'''Opt-in integration test: UTREEXOD=/path/to/utreexod pytest -q -s <this file>.'''

import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys

import aiohttp
import pytest

from electrumx.lib.coins import UtreexodRegtest
from electrumx.lib.hash import Base58, double_sha256, hash_to_hex_str, hex_str_to_hash
from electrumx.lib.tx import Tx, TxInput, TxOutput
from electrumx.server.daemon import UtreexodDaemon


UTREEXOD = os.environ.get('UTREEXOD')
pytestmark = pytest.mark.skipif(not UTREEXOD, reason='set UTREEXOD to run isolated regtest')


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@contextlib.contextmanager
def process(args, log_path, **kwargs):
    with log_path.open('a') as log:
        proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, **kwargs)
        try:
            yield proc
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


class ElectrumClient:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.request_id = 0
        self.notifications = []

    async def request(self, method, *params, error=False):
        self.request_id += 1
        self.writer.write(json.dumps({'jsonrpc': '2.0', 'id': self.request_id,
                                      'method': method, 'params': params}).encode() + b'\n')
        await self.writer.drain()
        while True:
            raw = await asyncio.wait_for(self.reader.readline(), 10)
            assert raw, 'Electrum connection closed'
            response = json.loads(raw)
            if 'id' not in response:
                self.notifications.append(response)
                continue
            assert response['id'] == self.request_id
            if error:
                return response['error']
            assert not response.get('error'), response
            return response['result']

    async def wait_height(self, height):
        for _ in range(200):
            header = await self.request('blockchain.headers.subscribe')
            if header['height'] == height:
                return header
            await asyncio.sleep(0.1)
        raise AssertionError(f'ElectrumX did not reach height {height}')


@contextlib.asynccontextmanager
async def connect(port, proc, log_path):
    for _ in range(200):
        assert proc.poll() is None, log_path.read_text()
        try:
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            break
        except OSError:
            await asyncio.sleep(0.1)
    else:
        raise AssertionError(log_path.read_text())
    try:
        yield ElectrumClient(reader, writer)
    finally:
        writer.close()
        await writer.wait_closed()


async def scenario(tmp_path, tls):
    rpc_port, electrum_port = free_port(), free_port()
    # Anyone-can-spend P2SH coinbases, only on this isolated regtest chain.
    redeem = b'\x51'
    script_hash = hashlib.new('ripemd160', hashlib.sha256(redeem).digest()).digest()
    address = Base58.encode_check(b'\xc4' + script_hash)
    mining_script = b'\xa9\x14' + script_hash + b'\x87'
    scripthash = hash_to_hex_str(hashlib.sha256(mining_script).digest())
    config = tmp_path / 'utreexod.conf'
    config.write_text('')
    node_args = [UTREEXOD, f'--configfile={config}',
                 f'--datadir={tmp_path / "node"}', f'--logdir={tmp_path / "logs"}',
                 '--regtest', '--flatutreexoproofindex', '--txindex', '--prune=0',
                 '--nobdkwallet', '--noassumeutreexo', '--nolisten', '--nodnsseed', '--maxpeers=0',
                 f'--rpclisten=127.0.0.1:{rpc_port}', '--rpcuser=test', '--rpcpass=test',
                 f'--miningaddr={address}']
    cert = tmp_path / 'rpc.cert'
    if tls:
        node_args += [f'--rpccert={cert}', f'--rpckey={tmp_path / "rpc.key"}']
    else:
        node_args += ['--notls']
    scheme = 'https' if tls else 'http'
    daemon = UtreexodDaemon(UtreexodRegtest, f'{scheme}://test:test@127.0.0.1:{rpc_port}', max_retry=0.1)
    db_dir = tmp_path / 'electrumx'
    db_dir.mkdir()
    banner = tmp_path / 'banner.txt'
    banner.write_text('$DAEMON_SUBVERSION / $SERVER_VERSION')
    env = dict(os.environ, COIN='Utreexod', NET='regtest', DB_DIRECTORY=str(db_dir),
               DAEMON_URL=daemon.current_url(), DB_ENGINE='leveldb',
               SERVICES=f'tcp://127.0.0.1:{electrum_port}', REPORT_SERVICES='',
               PEER_DISCOVERY='OFF', PEER_ANNOUNCE='', CACHE_MB='10', BANNER_FILE=str(banner),
               DAEMON_POLL_INTERVAL_BLOCKS='100', DAEMON_POLL_INTERVAL_MEMPOOL='100')
    server_args = [sys.executable, str(Path(__file__).resolve().parents[2] / 'electrumx_server')]
    server_log = tmp_path / 'electrumx.log'
    with process(node_args, tmp_path / 'utreexod.log'):
        if tls:
            for _ in range(200):
                if cert.exists():
                    break
                await asyncio.sleep(0.1)
            # The subprocess loads this at startup; this already-imported test
            # process needs an explicit equivalent SSL context.
            env['SSL_CERT_FILE'] = str(cert)
            daemon.connector = lambda: aiohttp.TCPConnector(ssl=ssl.create_default_context(cafile=cert))
        async with daemon:
            await asyncio.wait_for(daemon.height(), 20)
            await daemon.check_daemon_version()
            await daemon.check_daemon_indexes()  # fresh chain, no block 1 yet
            assert await daemon.estimatefee(1) == -1
            blocks = await daemon._send_single('generate', (101,))
            await daemon.check_daemon_indexes()
            first = await daemon.deserialised_block(blocks[0])
            coinbase = first['tx'][0]
            coinbase_hex = await daemon.getrawtransaction(coinbase)

            with process(server_args, server_log, env=env) as server:
                async with connect(electrum_port, server, server_log) as client:
                    _, protocol = await client.request('server.version', 'utreexod-regtest', ['1.4', '1.7'])
                    assert protocol == '1.4.3'
                    features = await client.request('server.features')
                    assert features['protocol_max'] == protocol
                    assert features['genesis_hash'] == UtreexodRegtest.GENESIS_HASH
                    assert (await client.request('server.banner')).startswith('Utreexod ')
                    await client.wait_height(101)
                    assert await client.request('blockchain.transaction.get', coinbase) == coinbase_hex
                    assert (await client.request('blockchain.transaction.get', coinbase, True))['txid'] == coinbase
                    utxos = await client.request('blockchain.scripthash.listunspent', scripthash)
                    assert len(utxos) == 101
                    assert (await client.request('blockchain.scripthash.get_balance', scripthash)) == {
                        'confirmed': 101 * 5_000_000_000, 'unconfirmed': 0}
                    assert len(await client.request('blockchain.scripthash.get_history', scripthash)) == 101
                    assert await client.request('blockchain.relayfee') == 0.00001
                    assert await client.request('blockchain.estimatefee', 26) == -1
                    assert isinstance(await client.request('blockchain.estimatefee', 2), (int, float))
                    for method in ['mempool.get_info', 'blockchain.transaction.broadcast_package',
                                   'blockchain.outpoint.get_status']:
                        assert (await client.request(method, error=True))['code'] == -32601

                    # Spend a mature coinbase to a script the node has never registered.
                    destination = b'\x00\x20' + hashlib.sha256(redeem).digest()  # P2WSH
                    destination_hash = hash_to_hex_str(hashlib.sha256(destination).digest())
                    assert await client.request('blockchain.scripthash.subscribe', destination_hash) is None
                    tx = Tx(version=2, inputs=[TxInput(prev_txid_rev=hex_str_to_hash(coinbase), prev_idx=0,
                                                       script=b'\x01\x51', sequence=0xffffffff)],
                            outputs=[TxOutput(value=4_999_990_000, pk_script=destination)], locktime=0,
                            txid_rev=b'', wtxid_rev=b'')
                    raw_tx = tx.serialize()
                    txid = hash_to_hex_str(double_sha256(raw_tx))
                    assert await client.request('blockchain.transaction.broadcast', raw_tx.hex()) == txid
                    for _ in range(200):
                        history = await client.request('blockchain.scripthash.get_mempool', destination_hash)
                        if history:
                            break
                        await asyncio.sleep(0.1)
                    assert history == [{'tx_hash': txid, 'height': 0, 'fee': 10000}]
                    assert any(n['method'] == 'blockchain.scripthash.subscribe' for n in client.notifications)
                    assert await client.request('blockchain.transaction.get', txid) == raw_tx.hex()

                    mined, = await daemon._send_single('generate', (1,))
                    header = await client.wait_height(102)
                    assert hash_to_hex_str(double_sha256(bytes.fromhex(header['hex']))) == mined
                    assert (await client.request('blockchain.scripthash.listunspent', destination_hash))[0]['height'] == 102
                    proof = await client.request('blockchain.transaction.get_merkle', txid, 102)
                    merkle_root = hex_str_to_hash(txid)
                    for level, sibling in enumerate(proof['merkle']):
                        sibling = hex_str_to_hash(sibling)
                        merkle_root = double_sha256(sibling + merkle_root if proof['pos'] >> level & 1
                                                   else merkle_root + sibling)
                    assert merkle_root == bytes.fromhex(header['hex'])[36:68]
                    # The bridge's proof service remains independently available.
                    roots = await daemon._send_single('getutreexoroots', (mined,))
                    assert roots

                    old_coinbase = (await daemon.deserialised_block(mined))['tx'][0]
                    await daemon._send_single('invalidateblock', (mined,))
                    # ElectrumX's prefetcher detects a fork when a block above
                    # its old height arrives, not on a rollback-only tip change.
                    fork = await daemon._send_single('generate', (2,))
                    assert fork[0] != mined
                    await client.wait_height(103)
                    assert await client.request('blockchain.block.header', 102) == (
                        await daemon._send_single('getblockheader', (fork[0], False)))
                    utxos = await client.request('blockchain.scripthash.listunspent', scripthash)
                    assert old_coinbase not in {utxo['tx_hash'] for utxo in utxos}
                    expected_history = []
                    for height, block_hash in enumerate(fork, 102):
                        if txid in (await daemon.deserialised_block(block_hash))['tx']:
                            expected_history.append({'tx_hash': txid, 'height': height})
                    assert await client.request('blockchain.scripthash.get_history', destination_hash) == expected_history

            # Restart the indexer and catch up blocks mined while it was offline.
            await daemon._send_single('generate', (2,))
            with process(server_args, server_log, env=env) as server:
                async with connect(electrum_port, server, server_log) as client:
                    await client.request('server.version', 'utreexod-regtest', '1.4.3')
                    await client.wait_height(105)
                    assert await client.request('blockchain.transaction.get', coinbase) == coinbase_hex
                    assert await client.request('blockchain.scripthash.get_history', destination_hash) == expected_history
    print(f'Isolated regtest passed; logs and databases: {tmp_path}')


@pytest.mark.asyncio
@pytest.mark.parametrize('tls', [False, True], ids=['http', 'https'])
async def test_utreexod_regtest(tmp_path, tls):
    pytest.importorskip('plyvel')
    await asyncio.wait_for(scenario(tmp_path, tls), 120)

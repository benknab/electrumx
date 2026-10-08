import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiorpcx import timeout_after

from electrumx.lib.coins import BitcoinMainnet, BitcoinRegtest, Coin, UtreexodRegtest
from electrumx.server.daemon import DaemonError, UtreexodDaemon
from electrumx.server.session import ElectrumX, UtreexodElectrumX


@pytest.fixture
def daemon():
    return UtreexodDaemon(UtreexodRegtest, 'user:password@127.0.0.1')


def test_profile():
    for network, port in [('mainnet', 8334), ('testnet', 18334), ('regtest', 18334), ('signet', 38332)]:
        coin = Coin.lookup_coin_class('Utreexod', network)
        bitcoin = Coin.lookup_coin_class('Bitcoin', network)
        assert coin.GENESIS_HASH == bitcoin.GENESIS_HASH
        assert coin.DESERIALIZER is bitcoin.DESERIALIZER
        assert coin.DAEMON is UtreexodDaemon
        assert coin.SESSIONCLS is UtreexodElectrumX
        assert coin.RPC_PORT == port
    assert BitcoinMainnet.SESSIONCLS is ElectrumX
    assert BitcoinMainnet.REQUIRED_DAEMON_INDEXES == ('txindex', 'txospenderindex')


@pytest.mark.asyncio
async def test_close_delimited_http(daemon):
    '''Exercise the real HTTP client against Utreexod-style framing and RPC types.'''
    requests = []

    async def handle(reader, writer):
        try:
            headers = await reader.readuntil(b'\r\n\r\n')
            length = next(int(line.split(b':', 1)[1]) for line in headers.split(b'\r\n')
                          if line.lower().startswith(b'content-length:'))
            body = json.loads(await reader.readexactly(length))
            requests.append((headers, body))

            def reply(request):
                assert request['jsonrpc'] == '1.0'
                assert isinstance(request['params'], list)
                method, params = request['method'], request.get('params', [])
                result, error = None, None
                if method == 'getinfo':
                    result = {'version': 60000, 'relayfee': 0.00001}
                elif method == 'getrawtransaction' and params[0] == 'missing':
                    error = {'code': -5, 'message': 'No information available'}
                elif method in ('getblock', 'getrawtransaction'):
                    if len(params) != 2 or type(params[1]) is not int:
                        error = {'code': -32602, 'message': 'integer verbosity required'}
                    else:
                        result = {'tx': ['txid']} if params[1] else '0102'
                elif method == 'estimatefee':
                    result = 0.00002
                else:
                    error = {'code': -32601, 'message': 'Method not found'}
                return dict(id=request['id'], **({'error': error} if error else {'result': result}))

            response = [reply(item) for item in body] if isinstance(body, list) else reply(body)
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n')
            writer.write(json.dumps(response).encode() + b'\n')
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, '127.0.0.1', 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        daemon.set_url(f'user:password@127.0.0.1:{port}')
        async with daemon, timeout_after(10):
            assert await daemon.raw_blocks(['a', 'b']) == [b'\x01\x02'] * 2
            assert await daemon.deserialised_block('a') == {'tx': ['txid']}
            assert await daemon.getrawtransaction('txid') == '0102'
            assert await daemon.getrawtransaction('txid', True) == {'tx': ['txid']}
            assert await daemon.getrawtransactions(['txid', 'missing']) == [b'\x01\x02', None]
            assert await daemon.relayfee() == 0.00001
            assert await daemon.relayfee() == 0.00001
            assert await daemon.estimatefee(2) == 0.00002
    assert len(requests) == 7  # getinfo cached
    assert all(b'Authorization: Basic dXNlcjpwYXNzd29yZA==' in headers for headers, _ in requests)


@pytest.mark.asyncio
async def test_network_check(daemon):
    daemon._send_single = AsyncMock(return_value={'version': 60000})
    daemon._send_vector = AsyncMock(return_value=[BitcoinRegtest.GENESIS_HASH])
    await daemon.check_daemon_version()
    daemon._send_vector.return_value = [BitcoinMainnet.GENESIS_HASH]
    with pytest.raises(RuntimeError, match='genesis'):
        await daemon.check_daemon_version()


@pytest.mark.asyncio
@pytest.mark.parametrize('height', [0, 100])
async def test_txindex_check(daemon, height):
    unknown = '00' * 32
    daemon._send_single = AsyncMock(return_value={'pruned': False, 'blocks': height})
    daemon.block_hex_hashes = AsyncMock(return_value=['block1'])
    daemon.deserialised_block = AsyncMock(return_value={'tx': ['coinbase1']})
    daemon.getrawtransaction = AsyncMock(side_effect=[
        DaemonError({'code': -5, 'message': f'No information available about transaction {unknown}'}),
        'rawcoinbase',
    ])
    await daemon.check_daemon_indexes()
    assert daemon.getrawtransaction.await_args_list[0].args == (unknown,)
    if height:
        daemon.getrawtransaction.assert_awaited_with('coinbase1')
    else:
        daemon.block_hex_hashes.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_index_and_pruning(daemon):
    daemon._send_single = AsyncMock(return_value={'pruned': True})
    with pytest.raises(RuntimeError, match='archival'):
        await daemon.check_daemon_indexes()
    daemon._send_single.return_value = {'pruned': False, 'blocks': 0}
    daemon.getrawtransaction = AsyncMock(side_effect=DaemonError({
        'code': -5, 'message': 'The transaction index must be enabled (specify --txindex)',
    }))
    with pytest.raises(RuntimeError, match='enable --txindex'):
        await daemon.check_daemon_indexes()


@pytest.mark.asyncio
@pytest.mark.parametrize('message', ['Fee estimation disabled', 'not enough blocks have been observed'])
async def test_unavailable_estimate(daemon, message):
    daemon._send_single = AsyncMock(side_effect=DaemonError({'code': -32603, 'message': message}))
    assert await daemon.estimatefee(1) == -1
    daemon._send_single.side_effect = DaemonError({'code': -32603, 'message': 'unexpected failure'})
    with pytest.raises(DaemonError):
        await daemon.estimatefee(1)


@pytest.mark.asyncio
async def test_out_of_range_estimate(daemon):
    daemon._send_single = AsyncMock()
    for target in (0, 26, 1008):
        assert await daemon.estimatefee(target) == -1
    daemon._send_single.assert_not_awaited()


@pytest.mark.asyncio
async def test_utreexod_generic_estimate_error(daemon):
    daemon._send_single = AsyncMock(side_effect=DaemonError({'code': -32600, 'message': 'Invalid request: malformed'}))
    assert await daemon.estimatefee(1) == -1


@pytest.mark.asyncio
async def test_banner_and_protocol():
    # Only banner/handler selection needs a session; no socket or manager needed.
    session = object.__new__(UtreexodElectrumX)
    session.env = SimpleNamespace(donation_address='donation', coin=UtreexodRegtest, report_services=[])
    session.daemon_request = AsyncMock(return_value={'version': 1060200})
    assert await session.replaced_banner('$DAEMON_VERSION $DAEMON_SUBVERSION $DONATION_ADDRESS') == (
        '1.6.2 Utreexod 1.6.2 donation')
    session.daemon_request.assert_awaited_once_with('getinfo')
    assert session.server_features(session.env)['protocol_max'] == '1.4.3'
    for protocol in [session.PROTOCOL_MIN, session.PROTOCOL_MAX]:
        session.set_request_handlers(protocol)
        assert 'blockchain.scripthash.listunspent' in session.request_handlers
        assert 'blockchain.transaction.get_merkle' in session.request_handlers
        assert 'blockchain.relayfee' in session.request_handlers
        for method in ['blockchain.transaction.broadcast_package', 'mempool.get_info',
                       'blockchain.transaction.testmempoolaccept', 'blockchain.outpoint.get_status']:
            assert method not in session.request_handlers

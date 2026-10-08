Utreexod backend
================

This fork adds an opt-in Utreexod backend to ElectrumX 2.0.0. Select it with
``COIN=Utreexod`` and ``NET=mainnet``, ``testnet``, ``regtest``, or ``signet``.
These are Bitcoin networks, with the same genesis hashes and transaction format.
The default RPC ports are 8334, 18334, 18334, and 38332 respectively.

The client-facing interface is standard Electrum protocol 1.4 through 1.4.3,
including arbitrary-script history, balances, UTXOs, subscriptions, raw
transactions, Merkle proofs, fee estimates, and transaction broadcast.
Package relay, mempool policy information, and the newer outpoint methods are
not advertised or registered. Bitcoin Core configurations still use
``COIN=Bitcoin`` and retain the upstream protocol and index requirements.

Utreexo membership proofs and accumulator roots remain a separate node API.
Clients should configure their Electrum and proof endpoints independently and
verify that evidence refers to the intended block.

Node requirements
-----------------

Use an archival Utreexod node with ``--prune=0 --txindex``. A bridge can also
use ``--flatutreexoproofindex`` to serve Utreexo proofs. ElectrumX does not need
the built-in watch-only wallet or Electrum server, and does not require
Bitcoin Core's ``txospenderindex``.

Startup checks verify the network's genesis hash, reject pruning, and probe
the transaction index. Utreexod has no ``getindexinfo``: an unknown transaction
distinguishes a disabled index from a missing transaction. Once block 1 exists,
startup also retrieves its coinbase through the historical transaction index.
As with upstream, startup checks apply to the first configured daemon; every
failover endpoint must meet the same requirements.

The adapter handles these RPC differences:

* Explicit JSON-RPC version and parameter arrays, including empty arrays.
* Omitted null ``error`` and ``result`` response fields.
* Integer verbosity for ``getblock``. Upstream already uses integer verbosity
  for ``getrawtransaction`` and sends no block-hash argument.
* ``getinfo.relayfee`` instead of ``getnetworkinfo``.
* ``estimatefee`` instead of ``estimatesmartfee``. Targets outside 1..25 and
  unavailable estimates return -1; Core-specific estimate modes are rejected.
  Utreexod 0.6 masks ordinary estimator errors as ``Invalid request: malformed``;
  this error is treated as an unavailable estimate only for the valid fee query.
* Utreexod's version encoding for custom banner substitutions.

RPC authentication and TLS
--------------------------

Set ``DAEMON_URL`` to an authenticated HTTP or HTTPS URL. Utreexod enables
RPC TLS by default. For its self-signed certificate, set
``SSL_CERT_FILE=/absolute/path/to/rpc.cert`` before starting ElectrumX and use
a hostname/IP covered by that certificate. The standard Python/aiohttp trust
checks remain enabled. Backend TLS is independent of Electrum's client-facing
``ssl://`` service and its ``SSL_CERTFILE``/``SSL_KEYFILE`` settings.

For an isolated loopback test, Utreexod's ``--notls`` permits plain HTTP.
The HTTP client accepts its connection-close-delimited responses; no
compatibility proxy or Utreexod source changes are needed.

Isolated regtest
----------------

Install from this checkout with ``python -m pip install -e '.[leveldb]'``
inside a virtual environment; the upstream PyPI package does not include this
adapter. See :doc:`HOWTO` for the database backend's system dependencies.
Set ``REGTEST_DIR`` to an absolute path dedicated to this test in both
terminals. Create fresh directories, then run Utreexod in a separate terminal::

    mkdir -p "$REGTEST_DIR/node" "$REGTEST_DIR/index"
    touch "$REGTEST_DIR/utreexod.conf"
    utreexod --configfile="$REGTEST_DIR/utreexod.conf" \
      --datadir="$REGTEST_DIR/node" --logdir="$REGTEST_DIR/logs" \
      --regtest --prune=0 --txindex --flatutreexoproofindex \
      --noassumeutreexo --nobdkwallet --nolisten --nodnsseed --maxpeers=0 \
      --notls --rpclisten=127.0.0.1:19443 \
      --rpcuser=regtest --rpcpass=regtest

Start ElectrumX::

    COIN=Utreexod NET=regtest \
      DB_DIRECTORY="$REGTEST_DIR/index" DB_ENGINE=leveldb \
      DAEMON_URL=http://regtest:regtest@127.0.0.1:19443 \
      SERVICES=tcp://127.0.0.1:19444 \
      PEER_DISCOVERY=OFF PEER_ANNOUNCE= \
      electrumx_server

Automated validation
--------------------

The opt-in integration test creates its own node/index directories, chooses
loopback ports, mines regtest coins, and terminates both processes afterwards.
It requires a Utreexod binary, pytest-asyncio, and the ``plyvel`` module
(``.[leveldb]``; a ``plyvel-ci`` wheel also works on macOS)::

    UTREEXOD=/absolute/path/to/utreexod \
      python -m pytest -q -s tests/server/test_utreexod_regtest.py

It exercises HTTP and verified HTTPS, startup on a fresh chain, indexing,
protocol negotiation, unsupported-method rejection, arbitrary-script queries,
broadcast, mempool subscriptions, confirmation, Merkle proofs, bridge root
queries, a longer-chain reorg, and restart/catch-up. Unit/transport tests are
in ``tests/server/test_utreexod.py``.

Scope and remaining validation
------------------------------

The initial runtime validation uses Utreexod 0.6.0 (commit ``e99c32c3``) on isolated regtest with
LevelDB. Mainnet storage, throughput, other networks, and limited ZK client
interoperability with electrs/Fulcrum still need validation. Root retrieval
alone is not end-to-end Utreexo membership-proof verification.

The upstream ElectrumX prefetcher notices a reorg when it fetches a block above
its previous indexed height. A rollback-only or equal-height tip change is not
immediately reflected; the regtest test grows the replacement chain past the
old height. This remains a follow-up for tip-sensitive clients.

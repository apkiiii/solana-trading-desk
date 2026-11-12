"""Test de la surveillance avec un vrai serveur WebSocket local (simule le RPC Solana)."""
from __future__ import annotations

import asyncio
import json

from websockets.asyncio.server import serve

from data.wallet_monitor import WalletMonitor
from tests.conftest import OTHER_WALLET, WALLET


class FakeRpc:
    def __init__(self):
        self.calls = []

    async def get_signatures_for_address(self, address, *, limit, until, critical):
        self.calls.append((address, until))
        # plus récent en premier, comme le vrai RPC
        return [{"signature": "missed-2", "err": None, "slot": 12},
                {"signature": "missed-failed", "err": {"x": 1}, "slot": 11},
                {"signature": "missed-1", "err": None, "slot": 10}]


def _notification(sub_id: int, signature: str, err=None) -> str:
    return json.dumps({"jsonrpc": "2.0", "method": "logsNotification", "params": {
        "subscription": sub_id, "result": {"context": {"slot": 99}, "value": {
            "signature": signature, "err": err, "logs": []}}}})


def test_monitor_subscribes_dedupes_reconnects_and_backfills():
    connections = {"n": 0}

    async def handler(ws):
        connections["n"] += 1
        subs = {}
        for _ in range(2):
            request = json.loads(await ws.recv())
            assert request["method"] == "logsSubscribe"
            wallet = request["params"][0]["mentions"][0]
            sub_id = 100 + request["id"]
            subs[wallet] = sub_id
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": sub_id}))
        if connections["n"] == 1:
            await ws.send(_notification(subs[WALLET], "sig-1"))
            await ws.send(_notification(subs[WALLET], "sig-1"))            # doublon
            await ws.send(_notification(subs[OTHER_WALLET], "sig-fail", err={"InstructionError": 1}))
            await ws.send(_notification(subs[OTHER_WALLET], "sig-2"))
            await asyncio.sleep(0.05)
            await ws.close()                                               # coupure
        else:
            await ws.send(_notification(subs[WALLET], "sig-3"))
            await asyncio.sleep(5)

    async def go():
        received = []
        stop = asyncio.Event()

        async def on_notice(notice):
            received.append((notice.wallet, notice.signature, notice.backfill))
            if notice.signature == "sig-3":
                stop.set()

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            rpc = FakeRpc()
            monitor = WalletMonitor(f"ws://127.0.0.1:{port}", [WALLET, OTHER_WALLET], on_notice, rpc=rpc,
                                    max_backoff=0.1)
            await asyncio.wait_for(monitor.run(stop), timeout=10)
            return received, rpc, monitor

    received, rpc, monitor = asyncio.run(go())
    signatures = [s for _, s, _ in received]
    assert signatures.count("sig-1") == 1                    # pas de doublon
    assert "sig-fail" not in signatures                      # transaction en échec ignorée
    assert ("missed-1" in signatures) and ("missed-2" in signatures) and ("missed-failed" not in signatures)
    assert signatures.index("missed-1") < signatures.index("missed-2")   # ordre chronologique
    assert all(b for _, s, b in received if s.startswith("missed"))
    assert signatures[-1] == "sig-3"
    assert monitor.connections == 2
    assert {call[0] for call in rpc.calls} == {WALLET, OTHER_WALLET}
    assert monitor.connected is False

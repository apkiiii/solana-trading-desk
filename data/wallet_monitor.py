"""Surveillance en temps réel des wallets suivis (WebSocket Solana ``logsSubscribe``).

- Un abonnement par wallet (filtre ``mentions``) : chaque transaction confirmée qui
  mentionne le wallet produit un ``SignatureNotice``.
- Les transactions en échec sont ignorées ; chaque signature n'est signalée qu'une fois.
- Coupure : reconnexion automatique avec délais croissants (1 s, 2 s, 4 s… jusqu'à 60 s),
  puis rattrapage des signatures manquées via ``getSignaturesForAddress`` (``backfill=True``).
- Les pings WebSocket détectent les connexions mortes.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI

from core.logs import redact
from data.models import SignatureNotice

log = logging.getLogger(__name__)

NoticeHandler = Callable[[SignatureNotice], Awaitable[None]]


class WalletMonitor:
    def __init__(self, ws_url: str, wallets: Iterable[str], on_notice: NoticeHandler, *, rpc: Any = None,
                 commitment: str = "confirmed", ping_interval: float = 20.0, max_backoff: float = 60.0,
                 connect: Callable[..., Any] = ws_connect, seen_capacity: int = 10_000):
        self._url = ws_url
        self._wallets: list[str] = list(dict.fromkeys(wallets))
        self._on_notice = on_notice
        self._rpc = rpc
        self._commitment = commitment
        self._ping_interval = ping_interval
        self._max_backoff = max_backoff
        self._connect = connect
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._seen_capacity = seen_capacity
        self._last_signature: dict[str, str] = {}
        self._resubscribe = asyncio.Event()
        self._subs: dict[int, str] = {}
        self.connected = False
        self.connections = 0
        self.last_message_at: datetime | None = None
        self.last_error: str | None = None

    # --------------------------------------------------------------- public
    @property
    def wallets(self) -> list[str]:
        return list(self._wallets)

    def set_wallets(self, wallets: Iterable[str]) -> None:
        """Change la liste des wallets suivis (reconnexion immédiate)."""
        new = list(dict.fromkeys(wallets))
        if new != self._wallets:
            self._wallets = new
            self._resubscribe.set()

    def seed_last_signatures(self, last: dict[str, str]) -> None:
        """Dernière signature connue par wallet (pour rattraper après un redémarrage)."""
        self._last_signature.update(last)

    def status(self) -> dict[str, Any]:
        return {"connected": self.connected, "connections": self.connections, "wallets": len(self._wallets),
                "last_message_at": self.last_message_at.isoformat() if self.last_message_at else None,
                "last_error": self.last_error}

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            if not self._wallets:
                self.connected = False
                await self._wait_any(stop, self._resubscribe, timeout=5.0)
                self._resubscribe.clear()
                continue
            try:
                async with self._connect(self._url, ping_interval=self._ping_interval, ping_timeout=20,
                                         open_timeout=10, max_size=4 * 1024 * 1024) as ws:
                    self.connections += 1
                    await self._subscribe(ws)
                    self.connected = True
                    self.last_error = None
                    backoff = 1.0
                    log.info("Surveillance active sur %d wallet(s).", len(self._wallets))
                    if self.connections > 1 or self._last_signature:
                        await self._backfill()
                    await self._consume(ws, stop)
            except (ConnectionClosed, OSError, InvalidHandshake, InvalidURI, asyncio.TimeoutError,
                    RuntimeError, ValueError) as exc:
                self.last_error = redact(f"{type(exc).__name__}: {exc}")
                log.warning("WebSocket interrompu (%s). Reconnexion dans %.0f s.", self.last_error, backoff)
            finally:
                self.connected = False
            if stop.is_set():
                break
            if self._resubscribe.is_set():
                self._resubscribe.clear()
                continue
            await self._wait_any(stop, timeout=backoff)
            backoff = min(self._max_backoff, backoff * 2)

    # -------------------------------------------------------------- interne
    @staticmethod
    async def _wait_any(*events: asyncio.Event, timeout: float) -> None:
        tasks = [asyncio.ensure_future(e.wait()) for e in events]
        try:
            await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()

    async def _subscribe(self, ws: Any) -> None:
        self._subs: dict[int, str] = {}
        pending: dict[int, str] = {}
        for request_id, wallet in enumerate(self._wallets, start=1):
            pending[request_id] = wallet
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "logsSubscribe",
                                      "params": [{"mentions": [wallet]}, {"commitment": self._commitment}]}))
        while pending:
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            message = json.loads(raw)
            if message.get("id") in pending:
                wallet = pending.pop(message["id"])
                if "error" in message:
                    raise RuntimeError(f"abonnement refusé pour {wallet} : {message['error']}")
                self._subs[int(message["result"])] = wallet
            elif message.get("method") == "logsNotification":
                await self._handle(message)

    async def _consume(self, ws: Any, stop: asyncio.Event) -> None:
        stop_task = asyncio.ensure_future(stop.wait())
        resub_task = asyncio.ensure_future(self._resubscribe.wait())
        try:
            while True:
                recv = asyncio.ensure_future(ws.recv())
                done, _ = await asyncio.wait({recv, stop_task, resub_task}, return_when=asyncio.FIRST_COMPLETED)
                if recv not in done:
                    recv.cancel()
                    return
                await self._handle(json.loads(recv.result()))
        finally:
            stop_task.cancel()
            resub_task.cancel()

    async def _handle(self, message: dict[str, Any]) -> None:
        self.last_message_at = datetime.now(UTC)
        if message.get("method") != "logsNotification":
            return
        params = message.get("params") or {}
        wallet = self._subs.get(params.get("subscription"))
        result = params.get("result") or {}
        value = result.get("value") or {}
        signature = value.get("signature")
        if not wallet or not signature or value.get("err") is not None:
            return
        slot = (result.get("context") or {}).get("slot")
        await self._emit(wallet, signature, slot, backfill=False)

    def _mark_seen(self, signature: str) -> bool:
        if signature in self._seen:
            return False
        self._seen[signature] = None
        while len(self._seen) > self._seen_capacity:
            self._seen.popitem(last=False)
        return True

    async def _emit(self, wallet: str, signature: str, slot: int | None, *, backfill: bool) -> None:
        if not self._mark_seen(signature):
            return
        self._last_signature[wallet] = signature
        notice = SignatureNotice(wallet=wallet, signature=signature, slot=slot,
                                 received_at=datetime.now(UTC), backfill=backfill)
        try:
            await self._on_notice(notice)
        except Exception:  # noqa: BLE001 - un traitement en erreur ne doit pas couper la surveillance
            log.exception("Erreur lors du traitement de %s", signature)

    async def _backfill(self) -> None:
        if self._rpc is None:
            return
        for wallet in list(self._wallets):
            until = self._last_signature.get(wallet)
            if not until:
                continue
            try:
                items = await self._rpc.get_signatures_for_address(wallet, limit=50, until=until,
                                                                   critical=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("Rattrapage impossible pour %s : %s", wallet, redact(str(exc)))
                continue
            for item in reversed(items):  # du plus ancien au plus récent
                if item.get("err") is None and item.get("signature"):
                    await self._emit(wallet, item["signature"], item.get("slot"), backfill=True)

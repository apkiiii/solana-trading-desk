"""Exécution des ordres APPROUVÉS par le JUDGE. Aucun autre module n'envoie d'ordre.

- ``PaperExecutor`` (compte démo) : argent fictif, mais chaque achat et chaque vente sont chiffrés
  avec un vrai devis Jupiter du moment (prix, frais de la route, impact prix), plus les frais réseau.
- ``LiveExecutor`` (compte réel) : devis Jupiter pour le wallet du bot, signature LOCALE avec la
  clé du compte Phantom dédié, envoi par Jupiter /execute. Les trades apparaissent dans Phantom.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from core.logs import redact

log = logging.getLogger(__name__)

LAMPORTS = 1_000_000_000
BASE_FEE_LAMPORTS = 5_000


@dataclass
class TradeResult:
    ok: bool
    mode: str
    side: str
    in_amount_raw: int = 0
    out_amount_raw: int = 0
    network_fee_lamports: int = 0
    tx_signature: str | None = None
    request_id: str | None = None
    error: str | None = None

    def fee_eur(self, eur_per_sol: float) -> float:
        return self.network_fee_lamports / LAMPORTS * eur_per_sol


class ExecutionError(RuntimeError):
    pass


class PaperExecutor:
    """Compte démo : simule l'ordre avec le devis réel du moment."""

    mode = "PAPER"

    def __init__(self, jupiter: Any, settings: Any):
        self.jupiter = jupiter
        self.sol_mint = settings["tokens"]["sol_mint"]
        self.slippage_bps = int(settings["execution"]["slippage_bps"])
        self.max_priority = int(settings["execution"]["max_priority_fee_lamports"])

    async def _swap(self, side: str, in_mint: str, out_mint: str, amount_raw: int) -> TradeResult:
        try:
            quote = await self.jupiter.get_order(in_mint, out_mint, amount_raw, slippage_bps=self.slippage_bps)
        except Exception as exc:  # noqa: BLE001
            return TradeResult(False, self.mode, side, amount_raw, error=redact(f"devis indisponible : {exc}"))
        if not quote.ok or quote.out_amount_raw <= 0:
            return TradeResult(False, self.mode, side, amount_raw,
                               error=f"aucune route ({quote.error_message or 'devis vide'})")
        priority = min(int(quote.prioritization_fee_lamports or 0), self.max_priority)
        return TradeResult(True, self.mode, side, amount_raw, quote.out_amount_raw, BASE_FEE_LAMPORTS + priority,
                           tx_signature=None, request_id=quote.request_id)

    async def buy(self, mint: str, lamports: int) -> TradeResult:
        return await self._swap("BUY", self.sol_mint, mint, lamports)

    async def sell(self, mint: str, amount_raw: int) -> TradeResult:
        return await self._swap("SELL", mint, self.sol_mint, amount_raw)


class LiveExecutor:
    """Compte réel : signature locale par le wallet du bot, envoi par Jupiter."""

    mode = "LIVE"

    def __init__(self, jupiter: Any, rpc: Any, wallet: Any, settings: Any):
        self.jupiter = jupiter
        self.rpc = rpc
        self.wallet = wallet
        self.sol_mint = settings["tokens"]["sol_mint"]
        self.slippage_bps = int(settings["execution"]["slippage_bps"])
        self.max_priority = int(settings["execution"]["max_priority_fee_lamports"])
        self.address = str(wallet.pubkey())

    async def sol_balance_lamports(self) -> int:
        return await self.rpc.get_balance(self.address)

    async def _swap(self, side: str, in_mint: str, out_mint: str, amount_raw: int) -> TradeResult:
        try:
            quote = await self.jupiter.get_order(in_mint, out_mint, amount_raw, taker=self.address,
                                                 slippage_bps=self.slippage_bps,
                                                 priority_fee_lamports=self.max_priority)
        except Exception as exc:  # noqa: BLE001
            return TradeResult(False, self.mode, side, amount_raw, error=redact(f"devis indisponible : {exc}"))
        if not quote.ok or not quote.transaction_b64 or not quote.request_id:
            return TradeResult(False, self.mode, side, amount_raw,
                               error=f"aucune transaction proposée ({quote.error_message or 'devis vide'})")
        try:
            signed = self.wallet.sign(quote.transaction_b64)
        except Exception as exc:  # noqa: BLE001
            return TradeResult(False, self.mode, side, amount_raw, error=redact(f"signature refusée : {exc}"))
        try:
            result = await self.jupiter.execute(signed, quote.request_id)
        except Exception as exc:  # noqa: BLE001
            # Statut inconnu : la transaction a peut-être été envoyée. Elle sera vérifiée par EXIT.
            return TradeResult(False, self.mode, side, amount_raw, request_id=quote.request_id,
                               error=redact(f"envoi sans réponse (statut à vérifier) : {exc}"))
        fee = int(quote.signature_fee_lamports or BASE_FEE_LAMPORTS) + min(
            int(quote.prioritization_fee_lamports or 0), self.max_priority)
        if result.get("status") != "Success":
            return TradeResult(False, self.mode, side, amount_raw, 0, fee, result.get("signature"), quote.request_id,
                               error=f"transaction échouée (code {result.get('code')}) : {result.get('error') or ''}")
        return TradeResult(True, self.mode, side, int(result.get("input_amount") or amount_raw),
                           int(result.get("output_amount") or quote.out_amount_raw), fee,
                           result.get("signature"), quote.request_id)

    async def buy(self, mint: str, lamports: int) -> TradeResult:
        return await self._swap("BUY", self.sol_mint, mint, lamports)

    async def sell(self, mint: str, amount_raw: int) -> TradeResult:
        # On revend ce que le wallet détient réellement (jamais plus que la position).
        try:
            held = await self.rpc.get_token_balance(self.address, mint)
        except Exception as exc:  # noqa: BLE001
            return TradeResult(False, self.mode, "SELL", amount_raw, error=redact(f"solde du token illisible : {exc}"))
        amount = min(held, amount_raw) if amount_raw > 0 else held
        if amount <= 0:
            return TradeResult(False, self.mode, "SELL", 0, error="le wallet ne détient plus ce token")
        return await self._swap("SELL", mint, self.sol_mint, amount)

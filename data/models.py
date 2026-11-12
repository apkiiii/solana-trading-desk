"""Structures de données partagées par le moteur et les agents."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

Side = Literal["BUY", "SELL", "OTHER"]


@dataclass(frozen=True)
class SwapEvent:
    """Un swap détecté sur un wallet suivi (calculé à partir des variations de soldes)."""

    signature: str
    wallet: str
    slot: int
    block_time: int | None          # horodatage Unix de la transaction (secondes)
    side: Side                      # BUY = SOL/USDC -> token ; SELL = token -> SOL/USDC
    token_mint: str | None          # le token acheté ou vendu
    token_amount_raw: int           # quantité (unités brutes, toujours positive)
    token_decimals: int | None
    quote_mint: str | None          # SOL (natif ou wSOL) ou USDC
    quote_amount_raw: int           # montant payé ou reçu (unités brutes, positif)
    quote_decimals: int | None
    fee_lamports: int
    fee_paid_by_wallet: bool        # False si une app/relais a payé les frais (cas "gasless")

    @property
    def token_amount(self) -> float | None:
        if self.token_decimals is None:
            return None
        return self.token_amount_raw / 10 ** self.token_decimals

    @property
    def quote_amount(self) -> float | None:
        if self.quote_decimals is None:
            return None
        return self.quote_amount_raw / 10 ** self.quote_decimals


@dataclass(frozen=True)
class SignatureNotice:
    """Signal du moniteur : le wallet ``wallet`` apparaît dans la transaction ``signature``."""

    wallet: str
    signature: str
    slot: int | None
    received_at: datetime
    backfill: bool = False          # True si rattrapée après une coupure


@dataclass(frozen=True)
class MintInfo:
    mint: str
    token_program: str
    is_token_2022: bool
    decimals: int
    supply_raw: int
    mint_authority: str | None      # None = autorité révoquée
    freeze_authority: str | None    # None = autorité révoquée
    extensions: tuple[str, ...] = ()
    transfer_fee_bps: int | None = None


@dataclass(frozen=True)
class PriceInfo:
    mint: str
    usd_price: float
    block_id: int | None
    decimals: int | None
    liquidity_usd: float | None
    price_change_24h: float | None


@dataclass(frozen=True)
class SwapQuote:
    """Devis Jupiter (/swap/v2/order). ``transaction`` est None pour un simple devis."""

    input_mint: str
    output_mint: str
    in_amount_raw: int
    out_amount_raw: int
    min_out_amount_raw: int | None
    price_impact_pct: float | None  # en pourcents (1.5 = 1,5 %)
    slippage_bps: int | None
    fee_bps: int | None             # frais totaux Jupiter
    platform_fee_bps: int | None
    router: str | None
    request_id: str | None
    transaction_b64: str | None
    signature_fee_lamports: int | None
    prioritization_fee_lamports: int | None
    rent_fee_lamports: int | None
    gasless: bool | None
    in_usd: float | None
    out_usd: float | None
    error_code: int | None = None
    error_message: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def ok(self) -> bool:
        return self.error_code is None and self.out_amount_raw > 0


@dataclass(frozen=True)
class PairInfo:
    pair_address: str
    dex_id: str
    base_mint: str
    quote_mint: str
    price_usd: float | None
    price_native: float | None
    liquidity_usd: float | None
    volume_h1: float | None
    volume_h24: float | None
    buys_m5: int | None
    sells_m5: int | None
    buys_h1: int | None
    sells_h1: int | None
    price_change_m5: float | None
    price_change_h1: float | None
    fdv: float | None
    market_cap: float | None
    pair_created_at: datetime | None


@dataclass(frozen=True)
class RiskItem:
    name: str
    level: str | None
    score: float | None
    description: str | None


@dataclass(frozen=True)
class RugReport:
    """Rapport RugCheck normalisé. ``None`` = information absente (donc à traiter comme un risque)."""

    mint: str
    score: float | None
    score_normalised: float | None
    rugged: bool | None
    mint_authority_revoked: bool | None
    freeze_authority_revoked: bool | None
    top10_holders_pct: float | None
    insider_detected: bool | None
    transfer_fee_pct: float | None
    total_market_liquidity_usd: float | None
    total_holders: int | None
    risks: tuple[RiskItem, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def danger_risks(self) -> tuple[RiskItem, ...]:
        return tuple(r for r in self.risks if (r.level or "").lower() in {"danger", "critical"})


@dataclass
class TokenSnapshot:
    """Tout ce que le moteur de données sait d'un token à un instant donné."""

    mint: str
    fetched_at: datetime
    price: PriceInfo | None = None
    best_pair: PairInfo | None = None
    mint_info: MintInfo | None = None
    rug: RugReport | None = None
    largest_accounts: list[tuple[str, int]] | None = None
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return not self.errors and None not in (self.price, self.best_pair, self.mint_info, self.rug)

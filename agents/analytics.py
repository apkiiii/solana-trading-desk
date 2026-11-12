"""Calcul des performances d'un trader à partir de ses swaps (fonctions pures, sans réseau).

Méthode :
- seuls les swaps payés/reçus en SOL sont comptés (unités homogènes) ;
- coût moyen par token : une vente « ferme » la part vendue de la position ;
- un trade clôturé est gagnant si le SOL reçu dépasse le coût de la part vendue ;
- drawdown = plus forte baisse du gain cumulé réalisé depuis son plus haut ;
- les ventes de tokens jamais achetés (reçus par transfert) sont comptées à part et signalées.

Limite : les tokens encore détenus (gains ou pertes latents) ne sont pas comptés.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from data.swap_parser import SOL_MINT

LAMPORTS = 1_000_000_000


@dataclass
class ClosedTrade:
    mint: str
    pnl_sol: float
    pnl_pct: float
    hold_seconds: float
    closed_at: int


@dataclass
class TraderStats:
    swaps: int = 0
    buys: int = 0
    sells: int = 0
    closed_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float | None = None
    realized_pnl_sol: float = 0.0
    gross_profit_sol: float = 0.0
    gross_loss_sol: float = 0.0
    profit_factor: float | None = None
    max_drawdown_sol: float = 0.0
    max_drawdown_pct: float | None = None
    avg_win_pct: float | None = None
    avg_loss_pct: float | None = None
    median_buy_sol: float | None = None
    avg_hold_seconds: float | None = None
    history_days: float = 0.0
    active_days: int = 0
    trades_per_day: float = 0.0
    positive_weeks_ratio: float | None = None
    unmatched_sells: int = 0
    top_win_share: float | None = None
    first_ts: int | None = None
    last_ts: int | None = None
    suspicious: list[str] = field(default_factory=list)


def _get(event: Any, key: str) -> Any:
    return event[key] if isinstance(event, dict) else getattr(event, key)


def compute_trader_stats(events: list[Any]) -> TraderStats:
    """``events`` : objets ``SwapEvent`` ou lignes de la table ``source_events``."""
    rows = []
    for e in events:
        if _get(e, "side") not in ("BUY", "SELL") or _get(e, "quote_mint") != SOL_MINT:
            continue
        if _get(e, "block_time") is None or not _get(e, "token_mint"):
            continue
        rows.append((int(_get(e, "block_time")), _get(e, "side"), _get(e, "token_mint"),
                     int(_get(e, "token_amount_raw")), int(_get(e, "quote_amount_raw")) / LAMPORTS))
    rows.sort(key=lambda r: r[0])
    stats = TraderStats(swaps=len(rows))
    if not rows:
        return stats

    positions: dict[str, dict[str, float]] = {}
    closed: list[ClosedTrade] = []
    buy_sizes: list[float] = []
    deployed = 0.0
    for ts, side, mint, amount, sol in rows:
        if amount <= 0:
            continue
        if side == "BUY":
            stats.buys += 1
            buy_sizes.append(sol)
            pos = positions.setdefault(mint, {"qty": 0.0, "cost": 0.0, "since": float(ts)})
            if pos["qty"] <= 0:
                pos["since"] = float(ts)
            pos["qty"] += amount
            pos["cost"] += sol
            continue
        stats.sells += 1
        pos = positions.get(mint)
        if not pos or pos["qty"] <= 0:
            stats.unmatched_sells += 1
            continue
        sold = min(float(amount), pos["qty"])
        basis = pos["cost"] * sold / pos["qty"]
        proceeds = sol * sold / amount
        pnl = proceeds - basis
        deployed += basis
        closed.append(ClosedTrade(mint=mint, pnl_sol=pnl, pnl_pct=(pnl / basis * 100) if basis > 0 else 0.0,
                                  hold_seconds=ts - pos["since"], closed_at=ts))
        pos["qty"] -= sold
        pos["cost"] -= basis
        if pos["qty"] <= 1e-9:
            positions.pop(mint)

    stats.first_ts, stats.last_ts = rows[0][0], rows[-1][0]
    stats.history_days = max((stats.last_ts - stats.first_ts) / 86400, 0.0)
    days = {datetime.fromtimestamp(r[0], UTC).date() for r in rows}
    stats.active_days = len(days)
    stats.trades_per_day = len(rows) / max(stats.history_days, 1.0)
    stats.median_buy_sol = statistics.median(buy_sizes) if buy_sizes else None

    stats.closed_trades = len(closed)
    if closed:
        wins = [c for c in closed if c.pnl_sol > 0]
        losses = [c for c in closed if c.pnl_sol <= 0]
        stats.wins, stats.losses = len(wins), len(losses)
        stats.win_rate = len(wins) / len(closed)
        stats.gross_profit_sol = sum(c.pnl_sol for c in wins)
        stats.gross_loss_sol = -sum(c.pnl_sol for c in losses)
        stats.realized_pnl_sol = stats.gross_profit_sol - stats.gross_loss_sol
        stats.profit_factor = (stats.gross_profit_sol / stats.gross_loss_sol) if stats.gross_loss_sol > 0 else None
        stats.avg_win_pct = statistics.mean(c.pnl_pct for c in wins) if wins else None
        stats.avg_loss_pct = statistics.mean(c.pnl_pct for c in losses) if losses else None
        stats.avg_hold_seconds = statistics.mean(c.hold_seconds for c in closed)
        if wins and stats.gross_profit_sol > 0:
            stats.top_win_share = max(c.pnl_sol for c in wins) / stats.gross_profit_sol

        cumulative = peak = max_dd = 0.0
        for trade in sorted(closed, key=lambda c: c.closed_at):
            cumulative += trade.pnl_sol
            peak = max(peak, cumulative)
            max_dd = max(max_dd, peak - cumulative)
        stats.max_drawdown_sol = max_dd
        stats.max_drawdown_pct = (max_dd / deployed * 100) if deployed > 0 else None

        weekly: dict[tuple[int, int], float] = defaultdict(float)
        for trade in closed:
            iso = datetime.fromtimestamp(trade.closed_at, UTC).isocalendar()
            weekly[(iso[0], iso[1])] += trade.pnl_sol
        stats.positive_weeks_ratio = sum(1 for v in weekly.values() if v > 0) / len(weekly)

    if stats.trades_per_day > 150:
        stats.suspicious.append("fréquence anormale (plus de 150 swaps par jour) : bot ou wash trading probable")
    if stats.sells >= 5 and stats.unmatched_sells / stats.sells > 0.5:
        stats.suspicious.append("plus de la moitié des ventes portent sur des tokens jamais achetés par ce wallet "
                                "(transferts, airdrops ou plusieurs wallets)")
    if stats.top_win_share is not None and stats.wins >= 3 and stats.top_win_share > 0.7:
        stats.suspicious.append("plus de 70 % des gains viennent d'un seul trade")
    if stats.avg_hold_seconds is not None and stats.closed_trades >= 10 and stats.avg_hold_seconds < 60:
        stats.suspicious.append("positions tenues moins d'une minute en moyenne (sniper/bot) : impossible à copier")
    return stats


def _scale(value: float, low: float, high: float) -> float:
    """0 à ``low``, 1 à ``high`` (bornes incluses), linéaire entre les deux."""
    if high == low:
        return 1.0 if value >= high else 0.0
    return max(0.0, min(1.0, (value - low) / (high - low)))


def score_trader(stats: TraderStats, *, min_closed_trades: int, min_history_days: float) -> tuple[float, list[str]]:
    """Score 0-100 sur des critères mesurables (jamais sur le seul gain total)."""
    notes: list[str] = []
    if stats.closed_trades == 0:
        return 0.0, ["aucun trade clôturé en SOL dans l'historique analysé"]

    win = _scale(stats.win_rate or 0.0, 0.30, 0.70) * 30
    pf_value = stats.profit_factor if stats.profit_factor is not None else (3.0 if stats.realized_pnl_sol > 0 else 0)
    profit = _scale(pf_value, 1.0, 3.0) * 25
    dd = (1 - _scale(stats.max_drawdown_pct if stats.max_drawdown_pct is not None else 100, 0, 60)) * 20
    regularity = (stats.positive_weeks_ratio or 0.0) * 10 + _scale(stats.active_days, 3, 14) * 5
    pnl = 10.0 if stats.realized_pnl_sol > 0 else 0.0
    score = win + profit + dd + regularity + pnl

    for flag in stats.suspicious:
        score -= 15
        notes.append(flag)
    if stats.closed_trades < min_closed_trades:
        score = min(score, 40.0)
        notes.append(f"historique insuffisant : {stats.closed_trades} trades clôturés (minimum {min_closed_trades})")
    if stats.history_days < min_history_days:
        score = min(score, 40.0)
        notes.append(f"historique trop court : {stats.history_days:.1f} jours (minimum {min_history_days})")
    return round(max(0.0, min(100.0, score)), 1), notes

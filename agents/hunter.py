"""HUNTER : cherche lui-même les meilleurs traders à copier et remplace ceux qui ne sont pas concluants.

Source : API officielle GMGN en lecture (forfait gratuit suffisant) —
- listes « smart money » (portefeuilles rentables repérés par GMGN) et « KOL » (traders connus) ;
- statistiques de chaque wallet sur 7 ou 30 jours (``wallet_stats``) ;
- ses derniers trades (``wallet_activity``) pour mesurer le gain moyen réel de ses ventes.

Chaque passage (toutes les ``interval_minutes``) :
1. relève les wallets actifs des listes GMGN ;
2. évalue quelques candidats (note 0-100, critères d'exclusion) ;
3. écarte les traders suivis « non concluants » (note trop basse, inactifs, copies perdantes) ;
4. complète la liste jusqu'à ``target_traders`` avec les meilleurs candidats, et remplace le plus
   faible si un candidat est nettement meilleur.

HUNTER ne fait que choisir QUI surveiller. Il n'achète rien : chaque achat reste soumis aux agents
et au JUDGE. Les traders ajoutés à la main ne sont jamais retirés automatiquement, et un trader
dont une copie est encore ouverte reste surveillé jusqu'à la vente (pour suivre sa sortie).
"""
from __future__ import annotations

import asyncio
import math
import statistics
from datetime import UTC, datetime, timedelta
from typing import Any

from agents.base import OK, UNAVAILABLE, WARN, AgentReport, unavailable
from core.logs import redact

SOURCE_AUTO = "auto"


def _f(value: Any) -> float | None:
    try:
        return float(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _unwrap(data: Any) -> Any:
    if isinstance(data, dict) and "data" in data and len(data) <= 3:
        return data["data"]
    return data


def _rows(data: Any, key: str) -> list[dict[str, Any]]:
    data = _unwrap(data)
    if isinstance(data, dict):
        data = data.get(key) or []
    return [r for r in (data or []) if isinstance(r, dict)]


def evaluate_wallet(stats: Any, activity: list[dict[str, Any]], cfg: Any, *, sol_usd: float | None,
                    now: datetime) -> tuple[float, str | None, dict[str, Any]]:
    """Note 0-100, motif d'exclusion éventuel, et statistiques au format attendu par HEAD/SNIPER."""
    s = _unwrap(stats)
    if isinstance(s, list):
        s = s[0] if s else {}
    s = s or {}
    pnl = s.get("pnl_stat") or {}
    common = s.get("common") or {}
    days = 30 if cfg["period"] == "30d" else 7
    buys = int(_f(s.get("buy", s.get("buy_count"))) or 0)
    sells = int(_f(s.get("sell", s.get("sell_count"))) or 0)
    winrate = _f(pnl.get("winrate", s.get("winrate")))
    tokens = int(_f(pnl.get("token_num")) or 0)
    realized = _f(s.get("realized_profit")) or 0.0
    roi = _f(s.get("realized_profit_pnl", s.get("pnl")))
    hold = _f(pnl.get("avg_holding_period"))
    big_losses = int(_f(pnl.get("pnl_lt_nd5_num")) or 0)
    created = int(_f(common.get("created_token_count")) or 0)
    trades_per_day = (buys + sells) / days

    # --- trades récents : gain moyen réel des ventes, taille habituelle des achats
    sells_rows = [r for r in activity if (r.get("event_type") or r.get("type")) == "sell"]
    buys_rows = [r for r in activity if (r.get("event_type") or r.get("type")) == "buy"]
    exits = []
    for row in sells_rows:
        cost, basis = _f(row.get("cost_usd")), _f(row.get("buy_cost_usd"))
        if cost is not None and basis and basis > 0:
            exits.append((cost / basis - 1) * 100)
    wins = [p for p in exits if p > 0]
    avg_win = round(statistics.fmean(wins), 2) if len(exits) >= 5 and wins else None
    buy_usd = [v for v in (_f(r.get("cost_usd")) for r in buys_rows) if v and v > 0]
    median_buy_sol = (statistics.median(buy_usd) / sol_usd) if buy_usd and sol_usd else None
    stamps = [int(_f(r.get("timestamp")) or 0) for r in activity]
    last_trade = max(stamps) if stamps and max(stamps) > 0 else None

    notes: list[str] = []
    reason = None
    if tokens < int(cfg["min_tokens"]):
        reason = f"trop peu de tokens tradés ({tokens} sur {days} j, minimum {cfg['min_tokens']})"
    elif trades_per_day > float(cfg["max_trades_per_day"]):
        reason = f"{trades_per_day:.0f} trades par jour : robot impossible à suivre"
    elif hold is not None and hold < float(cfg["min_avg_hold_seconds"]):
        reason = f"revend en {hold:.0f} s en moyenne : trop rapide pour être copié"
    elif realized <= 0:
        reason = f"pas gagnant sur {days} j ({realized:,.0f} $)"
    elif created >= 3:
        reason = f"a créé {created} tokens : profil de créateur, risque de manipulation"
    elif winrate is None:
        reason = "taux de réussite inconnu"
    elif last_trade is not None and now.timestamp() - last_trade > 86400:
        reason = "aucun trade depuis plus de 24 h"

    score = 0.0
    if winrate is not None:
        score += 35 * _clamp((winrate - 0.30) / 0.35)                         # réussite 30 % -> 65 %
    if roi is not None:
        score += 25 * _clamp(roi / 0.8)                                       # rendement jusqu'à +80 %
    score += 15 * _clamp(math.log10(max(realized, 1)) / math.log10(20_000))  # gain réalisé jusqu'à 20 000 $
    score += 10 * _clamp(tokens / 40)                                         # taille de l'échantillon
    if tokens:
        score += 15 * (1 - _clamp((big_losses / tokens) / 0.35))              # peu de pertes > 50 %
    score = round(score, 1)
    if avg_win is None:
        notes.append("gain moyen des ventes inconnu (trop peu de ventes récentes) : achats refusés par SNIPER")
    if reason:
        score = min(score, 39.0)

    payload = {
        "score": score, "source": "gmgn", "period": cfg["period"], "evaluated_at": now.isoformat(),
        "win_rate": winrate, "closed_trades": sells, "token_num": tokens, "buys": buys, "sells": sells,
        "realized_profit_usd": realized, "roi_pct": None if roi is None else round(roi * 100, 1),
        "realized_pnl_sol": round(realized / sol_usd, 3) if sol_usd else 0.0,
        "avg_win_pct": avg_win, "median_buy_sol": median_buy_sol, "avg_hold_s": hold,
        "trades_per_day": round(trades_per_day, 1), "big_loss_share": round(big_losses / tokens, 3) if tokens else None,
        "history_days": days, "max_drawdown_pct": None, "last_trade_at": last_trade,
        "tags": list(common.get("tags") or []), "name": common.get("twitter_username") or common.get("name"),
        "notes": ([reason] if reason else []) + notes, "exclusion": reason,
    }
    return score, reason, payload


class HunterAgent:
    name = "HUNTER"

    def __init__(self, gmgn: Any, db: Any, settings: Any, *, jupiter: Any = None):
        self.gmgn = gmgn
        self.db = db
        self.settings = settings
        self.cfg = settings["discovery"]
        self.jupiter = jupiter

    # --------------------------------------------------------------- passage
    async def cycle(self, now: datetime | None = None) -> AgentReport:
        now = now or datetime.now(UTC)
        if self.gmgn is None:
            return unavailable(self.name, None, "Clé API GMGN manquante : ajoute-la dans Connexions pour que le "
                                                "bot trouve lui-même les traders.")
        try:
            return await self._cycle(now)
        except Exception as exc:  # noqa: BLE001
            return unavailable(self.name, None, redact(f"Recherche impossible : {type(exc).__name__}: {exc}"))

    async def _sol_usd(self) -> float | None:
        if self.jupiter is None:
            return None
        sol = self.settings["tokens"]["sol_mint"]
        try:
            prices = await self.jupiter.get_prices([sol])
        except Exception:  # noqa: BLE001
            return None
        return prices[sol].usd_price if sol in prices else None

    async def discover(self, now: datetime) -> dict[str, dict[str, Any]]:
        """Wallets vus récemment dans les listes GMGN, avec leur nombre d'achats récents."""
        lists = await asyncio.gather(self.gmgn.smart_money(limit=100), self.gmgn.kol(limit=100),
                                     return_exceptions=True)
        if all(isinstance(r, BaseException) for r in lists):
            raise lists[0]  # type: ignore[misc]
        found: dict[str, dict[str, Any]] = {}
        for origin, result in zip(("smart_money", "kol"), lists, strict=True):
            if isinstance(result, BaseException):
                continue
            for row in _rows(result, "list"):
                maker = row.get("maker") or (row.get("maker_info") or {}).get("address")
                if not maker:
                    continue
                item = found.setdefault(maker, {"buys": 0, "trades": 0, "origins": set(), "last": 0})
                item["trades"] += 1
                item["origins"].add(origin)
                if row.get("side") == "buy":
                    item["buys"] += 1
                item["last"] = max(item["last"], int(_f(row.get("timestamp")) or 0))
        for maker in found:
            if self.db.get_candidate(maker) is None:
                self.db.touch_candidate(maker, now)
        return found

    async def evaluate(self, address: str, sol_usd: float | None, now: datetime) -> tuple[float, str | None]:
        stats = await self.gmgn.wallet_stats([address], period=self.cfg["period"])
        activity: list[dict[str, Any]] = []
        try:
            activity = await self.gmgn.wallet_activity(address, limit=50)
        except Exception:  # noqa: BLE001
            pass
        score, reason, payload = evaluate_wallet(stats, activity, self.cfg, sol_usd=sol_usd, now=now)
        self.db.upsert_candidate(address, score=score, status="excluded" if reason else "ok", reason=reason,
                                 stats=payload, evaluated_at=now)
        if any(t["address"] == address for t in self.db.list_traders()):
            self.db.update_trader_score(address, score, payload)
        return score, reason

    def _needs_eval(self, address: str, now: datetime) -> bool:
        candidate = self.db.get_candidate(address)
        if candidate is None or not candidate.get("evaluated_at"):
            return True
        age = (now - datetime.fromisoformat(candidate["evaluated_at"])).total_seconds() / 60
        return age >= float(self.cfg["stats_ttl_minutes"])

    async def _cycle(self, now: datetime) -> AgentReport:
        self.db.release_dropped_candidates(now - timedelta(hours=float(self.cfg["cooldown_hours"])))
        found = await self.discover(now)
        sol_usd = await self._sol_usd()

        # Traders suivis d'abord (pour juger s'ils restent concluants), puis les candidats les plus actifs.
        followed = [t["address"] for t in self.db.list_traders(active_only=True) if t.get("source") == SOURCE_AUTO]
        dropped = {c["address"] for c in self.db.list_candidates(500) if c["status"] == "dropped"}
        ranked = sorted((a for a in found if a not in followed and a not in dropped),
                        key=lambda a: (found[a]["buys"], found[a]["trades"], found[a]["last"]), reverse=True)
        queue = [a for a in followed if self._needs_eval(a, now)] + [a for a in ranked if self._needs_eval(a, now)]
        evaluated = []
        for address in queue[: int(self.cfg["candidates_per_cycle"])]:
            try:
                score, reason = await self.evaluate(address, sol_usd, now)
                evaluated.append((address, score, reason))
            except Exception as exc:  # noqa: BLE001
                evaluated.append((address, None, redact(str(exc))))
                if "RATE_LIMIT" in str(exc) or "429" in str(exc):
                    break

        changes = self.rotate(now)
        roster = self.db.list_traders(active_only=True)
        good = [e for e in evaluated if e[1] is not None and e[2] is None]
        summary = (f"{len(found)} wallets repérés sur GMGN, {len(evaluated)} évalués ({len(good)} retenus) — "
                   f"{len(roster)}/{self.target} traders copiés")
        if changes:
            summary += " — " + "; ".join(changes[:3])
        status = OK if len(roster) >= self.target else WARN
        reasons = changes + ([] if status == OK else [
            f"seulement {len(roster)} trader(s) au niveau requis pour l'instant : la recherche continue"])
        return AgentReport(self.name, None, status, summary, reasons,
                           {"discovered": len(found), "evaluated": [{"address": a, "score": s, "reason": r}
                                                                    for a, s, r in evaluated],
                            "roster": [t["address"] for t in roster], "changes": changes}, 1.0)

    # -------------------------------------------------------------- rotation
    @property
    def target(self) -> int:
        return min(int(self.cfg["target_traders"]), int(self.settings["capital"]["max_traders"]))

    def inconclusive(self, trader: dict[str, Any], now: datetime) -> str | None:
        """Motif pour lequel un trader suivi automatiquement n'est pas (ou plus) concluant."""
        min_score = float(self.settings["judge"]["min_trader_score"])
        score = trader.get("score")
        if score is None or score < min_score:
            return f"note {score if score is not None else '?'} < {min_score:.0f}"
        added = datetime.fromisoformat(trader["added_at"])
        rows = self.db.query_all("SELECT realized_pnl_eur FROM positions WHERE source_wallet = ? AND status = 'CLOSED' "
                                 "AND opened_at >= ?", (trader["address"], trader["added_at"]))
        pnl = sum(float(r["realized_pnl_eur"] or 0) for r in rows)
        losses = sum(1 for r in rows if (r["realized_pnl_eur"] or 0) < 0)
        if rows and (pnl <= -float(self.cfg["max_copied_loss_eur"]) or (losses >= 2 and losses == len(rows))):
            return f"copies perdantes ({pnl:+.2f} € sur {len(rows)} trade(s))"
        idle_limit = float(self.cfg["max_idle_minutes"]) * 60
        last = self.db.query_one("SELECT MAX(block_time) AS t FROM source_events WHERE wallet = ?",
                                 (trader["address"],))["t"]
        last = max(int(last or 0), int((trader.get("stats") or {}).get("last_trade_at") or 0))
        reference = max(last, int(added.timestamp()))
        if idle_limit and now.timestamp() - reference > idle_limit:
            return f"inactif depuis {(now.timestamp() - reference) / 60:.0f} min"
        return None

    def rotate(self, now: datetime) -> list[str]:
        """Retire les traders non concluants et complète la liste avec les meilleurs candidats."""
        changes: list[str] = []
        min_score = float(self.settings["judge"]["min_trader_score"])
        max_traders = int(self.settings["capital"]["max_traders"])
        open_sources = {p["source_wallet"] for p in self.db.open_positions() if p.get("source_wallet")}
        active = self.db.list_traders(active_only=True)

        for trader in [t for t in active if t.get("source") == SOURCE_AUTO]:
            reason = self.inconclusive(trader, now)
            if reason is None:
                continue
            if trader["address"] in open_sources:
                continue  # sa copie est encore ouverte : on garde l'œil sur sa vente
            self.db.delete_trader(trader["address"])
            self.db.drop_candidate(trader["address"], f"retiré : {reason}", now)
            changes.append(f"retiré {trader['address'][:4]}… ({reason})")

        active = self.db.list_traders(active_only=True)
        followed = {t["address"] for t in self.db.list_traders()}
        pool = [c for c in self.db.list_candidates(500)
                if c["status"] == "ok" and c["address"] not in followed and (c["score"] or 0) >= min_score
                and (c["stats"] or {}).get("avg_win_pct") is not None]
        pool.sort(key=lambda c: c["score"], reverse=True)

        while pool and len(active) < min(self.target, max_traders):
            best = pool.pop(0)
            self._add(best, now)
            changes.append(f"ajouté {best['address'][:4]}… (note {best['score']:.0f})")
            active = self.db.list_traders(active_only=True)

        # Remplacement du plus faible si un candidat est nettement meilleur (un seul par passage).
        autos = [t for t in active if t.get("source") == SOURCE_AUTO and t["address"] not in open_sources]
        if pool and autos:
            weakest = min(autos, key=lambda t: t.get("score") or 0)
            if pool[0]["score"] >= (weakest.get("score") or 0) + float(self.cfg["swap_margin"]):
                best = pool.pop(0)
                self.db.delete_trader(weakest["address"])
                self.db.drop_candidate(weakest["address"], f"remplacé par un trader mieux noté ({best['score']:.0f})",
                                       now)
                self._add(best, now)
                changes.append(f"remplacé {weakest['address'][:4]}… ({weakest.get('score') or 0:.0f}) par "
                               f"{best['address'][:4]}… ({best['score']:.0f})")
        return changes

    def _add(self, candidate: dict[str, Any], now: datetime) -> None:
        stats = dict(candidate["stats"] or {})
        label = stats.get("name") or "Auto GMGN"
        self.db.upsert_trader(candidate["address"], f"{label} (auto)", source=SOURCE_AUTO)
        self.db.update_trader_score(candidate["address"], candidate["score"], stats)


__all__ = ["HunterAgent", "evaluate_wallet", "SOURCE_AUTO", "UNAVAILABLE"]

"""HUNTER : cherche lui-même les meilleurs traders à copier et remplace ceux qui ne sont pas concluants.

Deux sources possibles (réglage ``discovery.source``, « auto » par défaut) :

A. **Blockchain, sans aucune clé** (utilisée si aucune clé GMGN n'est enregistrée) —
   1. Jupiter Tokens API (gratuite, sans clé) : tokens les plus actifs (« organic score ») ;
   2. RPC public gratuit : dernières transactions du token -> wallets qui ont acheté en payant eux-mêmes ;
   3. pour chaque wallet : tri rapide sur la liste de ses transactions (trop peu, trop actif, inactif),
      puis historique complet noté par HEAD (mêmes règles que pour un trader ajouté à la main).

B. **API officielle GMGN en lecture** (forfait gratuit suffisant, clé API nécessaire) —
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
from agents.head import HeadAgent
from core.logs import redact
from data.swap_parser import SOL_MINT, parse_swap

SOURCE_AUTO = "auto"
# Catégories Jupiter sans intérêt pour le copy trading de memecoins (stables, grosses cryptos, actions tokenisées…)
EXCLUDED_TOKEN_TAGS = {"stable", "major", "stocks", "xstocks", "prestocks", "rwa", "equities", "lst"}


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

    def __init__(self, gmgn: Any, db: Any, settings: Any, *, jupiter: Any = None, rpc: Any = None, dex: Any = None):
        self.gmgn = gmgn
        self.db = db
        self.settings = settings
        self.cfg = settings["discovery"]
        self.jupiter = jupiter
        self.rpc = rpc
        self.dex = dex

    @property
    def source(self) -> str | None:
        """Source utilisée pour ce passage : « gmgn », « onchain » ou None (rien de disponible)."""
        wanted = self.cfg.get("source", "auto")
        onchain_ok = self.rpc is not None and self.jupiter is not None and self.dex is not None
        if wanted == "gmgn":
            return "gmgn" if self.gmgn is not None else None
        if wanted == "onchain":
            return "onchain" if onchain_ok else None
        if self.gmgn is not None:
            return "gmgn"
        return "onchain" if onchain_ok else None

    # --------------------------------------------------------------- passage
    async def cycle(self, now: datetime | None = None) -> AgentReport:
        now = now or datetime.now(UTC)
        source = self.source
        if source is None:
            return unavailable(self.name, None, "Aucune source disponible pour chercher des traders "
                                                "(clé GMGN absente et blockchain inaccessible).")
        try:
            return await (self._cycle(now) if source == "gmgn" else self._cycle_onchain(now))
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

    def _needs_eval(self, address: str, now: datetime, ttl_minutes: float | None = None) -> bool:
        candidate = self.db.get_candidate(address)
        if candidate is None or candidate.get("status") == "new" or not candidate.get("evaluated_at"):
            return True
        age = (now - datetime.fromisoformat(candidate["evaluated_at"])).total_seconds() / 60
        return age >= (ttl_minutes if ttl_minutes is not None else float(self.cfg["stats_ttl_minutes"]))

    # ------------------------------------------------------- source blockchain
    async def scan_onchain(self, now: datetime) -> int:
        """Acheteurs récents (et sérieux) des tokens les plus actifs selon Jupiter."""
        tokens = await self.jupiter.top_tokens(self.cfg["onchain_category"], self.cfg["onchain_interval"], limit=20)
        skip = {self.settings["tokens"]["sol_mint"], self.settings["tokens"]["usdc_mint"],
                self.settings["tokens"]["eurc_mint"]}
        min_liquidity = float(self.settings["judge"]["min_liquidity_usd"])
        mints = [t["id"] for t in tokens if t["id"] not in skip and (_f(t.get("liquidity")) or 0) >= min_liquidity
                 and not (set(t.get("tags") or []) & EXCLUDED_TOKEN_TAGS)]
        usdc = self.settings["tokens"]["usdc_mint"]
        min_buy = float(self.cfg["min_buy_sol"])
        sol_usd = await self._sol_usd()
        buyers: dict[str, set[str]] = {}
        for mint in mints[: int(self.cfg["onchain_tokens"])]:
            # Transactions qui touchent le token (tous pools et routes confondus). Mesuré en réel : la plupart
            # sont des robots d'arbitrage (souvent en échec) ou des achats payés par le relais d'une application.
            try:
                signatures = await self.rpc.get_signatures_for_address(mint, limit=int(self.cfg["onchain_pool_sample"]))
            except Exception:  # noqa: BLE001
                continue
            read = 0
            for item in signatures:
                if item.get("err") is not None:  # les robots d'arbitrage échouent souvent : ignorés
                    continue
                if read >= int(self.cfg.get("onchain_tx_per_token", 40)):
                    break
                read += 1
                try:
                    tx = await self.rpc.get_transaction(item["signature"])
                except Exception:  # noqa: BLE001
                    continue
                keys = ((tx or {}).get("transaction") or {}).get("message", {}).get("accountKeys") or []
                if not keys:
                    continue
                payer = keys[0]["pubkey"] if isinstance(keys[0], dict) else str(keys[0])
                # L'acheteur n'est pas toujours celui qui paie les frais : les applications (FOMO, Phantom
                # Terminal…) font payer un relais. On examine donc chaque propriétaire du token dans la transaction.
                owners = {payer} | {b.get("owner") for b in ((tx.get("meta") or {}).get("postTokenBalances") or [])
                                    if b.get("mint") == mint and b.get("owner")}
                for owner in owners:
                    event = parse_swap(tx, owner, usdc_mint=usdc)
                    if not event or event.side != "BUY" or event.token_mint != mint:
                        continue
                    value = event.quote_amount or 0
                    if event.quote_mint == usdc:
                        value = value / sol_usd if sol_usd else 0
                    elif event.quote_mint != SOL_MINT:
                        continue
                    if value >= min_buy:
                        buyers.setdefault(owner, set()).add(mint)
        followed = {t["address"] for t in self.db.list_traders()}
        for wallet, seen in buyers.items():
            if wallet in followed:
                continue
            candidate = self.db.get_candidate(wallet)
            if candidate is None or candidate["status"] == "new":
                previous = (candidate or {}).get("stats") or {}
                self.db.upsert_candidate(wallet, score=None, status="new", reason=None, stats={
                    "origin": "onchain", "seen": int(previous.get("seen") or 0) + len(seen),
                    "seen_tokens": sorted(set(previous.get("seen_tokens") or []) | seen)[:10]}, evaluated_at=now)
        self.db.heartbeat("hunter_scan", "OK", f"{len(buyers)} acheteurs sur {len(mints[: int(self.cfg['onchain_tokens'])])} tokens")
        return len(buyers)

    async def evaluate_onchain(self, address: str, now: datetime) -> tuple[float | None, str | None]:
        reason, base = await self.prefilter(address, now)
        if reason:
            return 0.0, reason
        return await self.full_evaluation(address, base, now)

    async def prefilter(self, address: str, now: datetime) -> tuple[str | None, dict[str, Any]]:
        """Tri rapide (1 requête) sur la liste des transactions du wallet. Motif d'exclusion ou None."""
        agents = self.settings["agents"]
        judge = self.settings["judge"]
        limit = int(agents["head_max_transactions"])
        signatures = [s for s in await self.rpc.get_signatures_for_address(address, limit=limit)
                      if s.get("err") is None]
        times = [int(s["blockTime"]) for s in signatures if s.get("blockTime")]
        last = max(times) if times else None
        span_days = (max(times) - min(times)) / 86400 if len(times) > 1 else 0.0
        reason = None
        if len(signatures) < 2 * int(judge["min_trader_closed_trades"]):
            reason = f"pas assez de transactions ({len(signatures)})"
        elif len(signatures) >= limit and span_days < float(judge["min_trader_history_days"]):
            reason = (f"trop actif : {len(signatures)} transactions en {span_days:.1f} j "
                      f"(impossible de vérifier {judge['min_trader_history_days']} j d'historique)")
        elif last is not None and now.timestamp() - last > 86400:
            reason = "aucun trade depuis 24 h"
        previous = (self.db.get_candidate(address) or {}).get("stats") or {}
        base = {"origin": "onchain", "seen": previous.get("seen"), "seen_tokens": previous.get("seen_tokens"),
                "last_trade_at": last, "transactions": len(signatures), "span_days": round(span_days, 1)}
        if reason:
            self.db.upsert_candidate(address, score=0.0, status="excluded", reason=reason,
                                     stats={**base, "evaluated_at": now.isoformat()}, evaluated_at=now)
        return reason, base

    async def full_evaluation(self, address: str, base: dict[str, Any], now: datetime) -> tuple[float | None, str | None]:
        """Historique complet noté par HEAD (environ 300 transactions)."""
        judge = self.settings["judge"]
        report = await HeadAgent(self.rpc, self.db, self.settings, usdc_mint=self.settings["tokens"]["usdc_mint"],
                                 jupiter=self.jupiter).evaluate(address)
        payload = {**dict(report.data or {}), **base, "evaluated_at": now.isoformat()}
        score = _f(payload.get("score"))
        reason = None
        if report.status == UNAVAILABLE or score is None:
            reason = f"évaluation impossible : {report.summary}"
        elif score < float(judge["min_trader_score"]):
            reason = (report.reasons or [f"note {score:.0f}"])[0]
        elif payload.get("avg_win_pct") is None:
            reason = "gain moyen inconnu"
        self.db.upsert_candidate(address, score=score, status="excluded" if reason else "ok", reason=reason,
                                 stats=payload, evaluated_at=now)
        if any(t["address"] == address for t in self.db.list_traders()):
            self.db.update_trader_score(address, score, payload)
        return score, reason

    async def _cycle_onchain(self, now: datetime) -> AgentReport:
        self.db.release_dropped_candidates(now - timedelta(hours=float(self.cfg["cooldown_hours"])))
        row = self.db.query_one("SELECT at FROM heartbeats WHERE component = 'hunter_scan'")
        found = None
        if row is None or (now - datetime.fromisoformat(row["at"])).total_seconds() >= float(
                self.cfg["scan_minutes"]) * 60:
            found = await self.scan_onchain(now)
        ttl = float(self.settings["agents"]["head_cache_hours"]) * 60
        followed = [t["address"] for t in self.db.list_traders(active_only=True) if t.get("source") == SOURCE_AUTO]
        fresh = [c for c in self.db.list_candidates(500) if c["status"] == "new"]
        fresh.sort(key=lambda c: int((c["stats"] or {}).get("seen") or 0), reverse=True)
        queue = [a for a in followed if self._needs_eval(a, now, ttl)] + [c["address"] for c in fresh]
        evaluated = []
        full_done = 0
        for address in queue[: int(self.cfg.get("onchain_prefilter_per_cycle", 8))]:
            try:
                reason, base = await self.prefilter(address, now)
                if reason:
                    evaluated.append((address, 0.0, reason))
                    continue
                if full_done >= int(self.cfg["onchain_evals_per_cycle"]):
                    break  # vérification complète au prochain passage
                full_done += 1
                score, reason = await self.full_evaluation(address, base, now)
            except Exception as exc:  # noqa: BLE001
                score, reason = None, redact(f"{type(exc).__name__}: {exc}")
            evaluated.append((address, score, reason))
        changes = self.rotate(now)
        roster = self.db.list_traders(active_only=True)
        waiting = len([c for c in self.db.list_candidates(500) if c["status"] == "new"])
        summary = (f"Blockchain : {len(roster)}/{self.target} traders copiés, {waiting} candidat(s) en attente "
                   f"de vérification")
        if found is not None:
            summary += f", {found} acheteurs repérés sur les tokens actifs"
        if changes:
            summary += " — " + "; ".join(changes[:3])
        status = OK if len(roster) >= self.target else WARN
        reasons = changes + ([] if status == OK else [
            "la recherche continue : chaque candidat est vérifié sur son historique complet (1 à 2 min chacun)"])
        return AgentReport(self.name, None, status, summary, reasons,
                           {"source": "onchain", "discovered": found,
                            "evaluated": [{"address": a, "score": s, "reason": r} for a, s, r in evaluated],
                            "roster": [t["address"] for t in roster], "changes": changes}, 1.0)

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

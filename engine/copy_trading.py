"""Moteur de copy trading automatique.

Chaîne pour chaque transaction d'un trader suivi :
    SCOUT (détection) -> HEAD (qualité du trader) -> RISK + WHALE (token) -> SNIPER (proposition)
    -> JUDGE (décision déterministe, dernier mot) -> exécution (démo ou réel) si et seulement si APPROVED.
En parallèle, EXIT surveille les positions ouvertes ; chaque vente passe aussi par le JUDGE.

Règles de sécurité :
- seul le JUDGE peut autoriser un ordre ; aucun agent n'a accès à l'exécution ;
- le compte réel n'est utilisable que s'il a été armé dans CETTE session (``live_armed``),
  jamais après un redémarrage ;
- STOPPER bloque les nouveaux achats mais EXIT continue de protéger les positions ;
- l'ARRÊT D'URGENCE bloque tout, y compris les ventes.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from agents.base import UNAVAILABLE, AgentReport, save_report
from agents.exit_agent import ExitAgent
from agents.head import HeadAgent
from agents.hunter import SOURCE_AUTO, HunterAgent
from agents.judge import APPROVED, REJECTED, WAIT, Judge, SystemState
from agents.risk import RiskAgent
from agents.scout import ScoutAgent
from agents.sniper import SniperAgent
from agents.whale import WhaleAgent
from config.settings import load_settings
from core.logs import redact
from core.paths import EMERGENCY_STOP_FILE
from data.market_data import MarketData
from data.models import SignatureNotice
from engine.execution import LAMPORTS, LiveExecutor, PaperExecutor, TradeResult
from engine.portfolio import Portfolio, book_mode
from engine.scheduler import entries_window_open

log = logging.getLogger(__name__)


@dataclass
class Outcome:
    """Résultat du traitement d'une transaction source (pour le journal et les tests)."""
    stage: str                      # IGNORED | SKIPPED | REJECTED | APPROVED | EXECUTED | FAILED
    detail: str
    decision: Any = None
    position_id: int | None = None


class CopyTradingEngine:
    def __init__(self, db: Any, providers: Any, *, wallet_store: Any = None,
                 monitor_factory: Callable[..., Any] | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 sleep: Callable[[float], Any] = asyncio.sleep,
                 exit_interval: float = 15.0, wait_retry_seconds: float = 4.0, max_parallel: int = 2):
        self.db = db
        self.providers = providers
        self.wallet_store = wallet_store
        self.monitor_factory = monitor_factory
        self.clock = clock
        self.sleep = sleep
        self.exit_interval = exit_interval
        self.wait_retry_seconds = wait_retry_seconds
        self.portfolio = Portfolio(db)
        self.live_armed = False
        self.monitor: Any = None
        self.started_at: datetime | None = None
        self.last_error: str | None = None
        self._semaphore = asyncio.Semaphore(max_parallel)
        self._tasks: set[asyncio.Task] = set()
        self._head_running: set[str] = set()
        self._sell_retry_at: dict[int, datetime] = {}
        self._last_exit_status: dict[str, str] = {}
        self._notice_times: dict[str, list[float]] = {}
        self.dropped_notices = 0
        self._warned_live_positions = False
        self._last_eur_per_sol: float | None = None

    # ------------------------------------------------------------------ outils
    def _settings(self) -> Any:
        return load_settings(self.db)

    def _market(self, settings: Any) -> MarketData:
        p = self.providers
        return MarketData(p.rpc, p.jupiter, p.dex, p.rug, settings["tokens"])

    async def _eur_per_sol(self, settings: Any) -> float | None:
        value, _reason = await self._market(settings).eur_per_sol()
        if value:
            self._last_eur_per_sol = value
        return value

    def _executor(self, mode: str, settings: Any) -> PaperExecutor | LiveExecutor | None:
        if mode == "PAPER":
            return PaperExecutor(self.providers.jupiter, settings)
        if not self.live_armed or self.wallet_store is None or not self.wallet_store.exists():
            return None
        return LiveExecutor(self.providers.jupiter, self.providers.rpc, self.wallet_store, settings)

    async def _fee_reserve_ok(self, mode: str, settings: Any, eur_per_sol: float | None) -> bool | None:
        cap = settings["capital"]
        needed = float(cap["max_position_eur"]) + float(cap["fee_reserve_eur"])
        if mode == "PAPER":
            # argent fictif encore libre (le capital démo inclut aussi la réserve de 27,50 € : jamais investie)
            free = self.portfolio.demo_cash_eur(settings) - float(cap["reserve_eur"])
            return free >= needed
        executor = self._executor(mode, settings)
        if executor is None or not eur_per_sol:
            return None
        try:
            lamports = await executor.sol_balance_lamports()
        except Exception:  # noqa: BLE001
            return None
        return lamports / LAMPORTS * eur_per_sol >= needed

    async def system_state(self, settings: Any, eur_per_sol: float | None = None) -> SystemState:
        control = self.db.get_control()
        now = self.clock()
        mode = book_mode(control["mode"])
        book = self.portfolio.snapshot(mode, settings, now)
        return SystemState(
            now=now, mode=control["mode"], bot_running=control["bot_state"] == "RUNNING",
            manual_stop=bool(control["manual_stop"]), emergency_stop=bool(control["emergency_stop"]),
            emergency_file=EMERGENCY_STOP_FILE.exists(),
            entries_window_open=entries_window_open(settings["schedule"], now),
            live_armed=self.live_armed, open_positions=book["count"], open_mints=book["open_mints"],
            invested_eur=book["invested_eur"], daily_loss_eur=book["daily_loss_eur"],
            fee_reserve_ok=await self._fee_reserve_ok(mode, settings, eur_per_sol))

    # ------------------------------------------------------------- achats
    async def handle_notice(self, notice: SignatureNotice) -> Outcome:
        """Traite une transaction d'un trader suivi, de la détection à l'éventuel ordre."""
        async with self._semaphore:
            return await self._handle_notice(notice)

    async def _handle_notice(self, notice: SignatureNotice) -> Outcome:
        settings = self._settings()
        timeout = float(settings["agents"]["agent_timeout_seconds"])
        scout_agent = ScoutAgent(self.providers.rpc, self.db, usdc_mint=settings["tokens"]["usdc_mint"],
                                 timeout=timeout)
        scout = await scout_agent.process_notice(notice, now=self.clock())
        if scout.status not in ("DUPLICATE",):
            save_report(self.db, scout)
        event = scout.data
        if scout.status != "OK":
            return Outcome("IGNORED", scout.summary)
        if event.get("side") == "SELL":
            # Le trader vend : EXIT vérifie tout de suite si une de nos positions doit suivre.
            await self.review_exits()
            return Outcome("IGNORED", "vente du trader transmise à EXIT")
        if event.get("side") != "BUY":
            return Outcome("IGNORED", "pas un achat")

        control = self.db.get_control()
        if control["bot_state"] != "RUNNING" or control["manual_stop"]:
            return Outcome("SKIPPED", "bot arrêté : achat non analysé")

        key = f"BUY:{event['signature']}"
        head_agent = HeadAgent(self.providers.rpc, self.db, settings, usdc_mint=settings["tokens"]["usdc_mint"])
        head = head_agent.cached_report(event["wallet"], now=self.clock())
        save_report(self.db, head)
        if head_agent.needs_refresh(event["wallet"], now=self.clock()) and not self._is_auto(event["wallet"]):
            self._spawn(self._refresh_trader(event["wallet"]))

        # Contrôles d'état d'abord : s'ils échouent, le JUDGE refuse sans dépenser de crédits d'API.
        judge = Judge(self.db, settings)
        state = await self.system_state(settings)
        early = judge.evaluate_buy(proposal_key=key, reports={"SCOUT": scout, "HEAD": head}, state=state)
        if early.decision == REJECTED:
            return Outcome("REJECTED", "; ".join(early.reasons[:2]), early)

        deadline = self.clock() + timedelta(seconds=float(settings["judge"]["wait_timeout_seconds"]) + 5)
        while True:
            reports, context = await self._analyze(settings, event, head, scout)
            state = await self.system_state(settings, context.get("eur_per_sol"))
            decision = judge.evaluate_buy(proposal_key=key, reports=reports, state=state)
            if decision.decision != WAIT or self.clock() >= deadline:
                break
            await self.sleep(self.wait_retry_seconds)
        if decision.decision != APPROVED:
            return Outcome("REJECTED", "; ".join(decision.reasons[:2]), decision)
        return await self._buy(settings, key, decision, reports, context, state)

    async def _analyze(self, settings: Any, event: dict[str, Any], head: AgentReport,
                       scout: AgentReport) -> tuple[dict[str, AgentReport | None], dict[str, Any]]:
        timeout = float(settings["agents"]["agent_timeout_seconds"])
        tokens = settings["tokens"]
        mint = event["token_mint"]
        market = self._market(settings)
        eur_per_sol, _ = await market.eur_per_sol()
        if eur_per_sol:
            self._last_eur_per_sol = eur_per_sol
        snapshot = await market.snapshot(mint, use_cache=False)
        context: dict[str, Any] = {"eur_per_sol": eur_per_sol, "snapshot": snapshot}
        try:
            prices = await self.providers.jupiter.get_prices([tokens["sol_mint"]])
            sol_usd = prices[tokens["sol_mint"]].usd_price if tokens["sol_mint"] in prices else None
        except Exception:  # noqa: BLE001
            sol_usd = None
        context["sol_usd"] = sol_usd
        amount_eur = float(settings["capital"]["max_position_eur"])
        lamports = int(amount_eur / eur_per_sol * LAMPORTS) if eur_per_sol else 0
        if lamports > 0:
            risk, whale = await asyncio.gather(
                RiskAgent(self.providers.jupiter, settings, timeout=timeout).analyze(mint, lamports, snapshot,
                                                                                     now=self.clock()),
                WhaleAgent(self.providers.rpc, self.db, settings, timeout=timeout).analyze(mint, snapshot, sol_usd,
                                                                                          now=self.clock()))
        else:
            risk = AgentReport("RISK", mint, UNAVAILABLE, "Prix du SOL en euros indisponible.", ["prix SOL/EUR"])
            whale = await WhaleAgent(self.providers.rpc, self.db, settings, timeout=timeout).analyze(
                mint, snapshot, sol_usd, now=self.clock())
        book = self.portfolio.snapshot(book_mode(self.db.get_control()["mode"]), settings, self.clock())
        sized_event = event
        if event.get("quote_mint") == tokens["usdc_mint"] and sol_usd and event.get("quote_amount"):
            # achat du trader en USDC : converti en SOL pour comparer à ses achats habituels (calculés en SOL)
            sized_event = {**event, "quote_amount": float(event["quote_amount"]) / sol_usd}
        sniper = SniperAgent(settings).propose(event=sized_event, head=head, risk=risk, eur_per_sol=eur_per_sol,
                                               open_positions=book["count"], invested_eur=book["invested_eur"])
        for report in (risk, whale, sniper):
            save_report(self.db, report)
        return {"SCOUT": scout, "HEAD": head, "RISK": risk, "WHALE": whale, "SNIPER": sniper}, context

    async def _buy(self, settings: Any, key: str, decision: Any, reports: dict[str, AgentReport | None],
                   context: dict[str, Any], state: SystemState) -> Outcome:
        proposal = reports["SNIPER"].data  # type: ignore[union-attr]
        event = reports["SCOUT"].data      # type: ignore[union-attr]
        mint = proposal["mint"]
        mode = book_mode(state.mode)
        executor = self._executor(mode, settings)
        if executor is None:
            self.db.add_alert("warning", "Achat approuvé mais compte réel non armé : aucun ordre envoyé.")
            return Outcome("FAILED", "compte réel non armé", decision)
        eur_per_sol = float(proposal["eur_per_sol"])
        result: TradeResult = await executor.buy(mint, int(proposal["amount_lamports"]))
        execution_id = self._record_execution(result, key, settings, sol_in=True, mint=mint)
        if not result.ok:
            self.db.add_alert("warning", f"Achat non exécuté ({mint[:4]}…) : {result.error}")
            return Outcome("FAILED", result.error or "échec", decision)
        snapshot = context.get("snapshot")
        decimals = snapshot.mint_info.decimals if snapshot is not None and snapshot.mint_info else None
        sol_usd = context.get("sol_usd")
        entry_usd = None
        if decimals is not None and sol_usd and result.out_amount_raw:
            entry_usd = (result.in_amount_raw / LAMPORTS * sol_usd) / (result.out_amount_raw / 10 ** decimals)
        fee_eur = result.fee_eur(eur_per_sol)
        cost_eur = result.in_amount_raw / LAMPORTS * eur_per_sol + fee_eur
        exit_cfg = settings["exit"]
        position_id = self.db.open_position(
            token_mint=mint, mode=mode, source_wallet=event.get("wallet"), source_signature=event.get("signature"),
            amount_raw=result.out_amount_raw, decimals=decimals, entry_price_usd=entry_usd, cost_eur=cost_eur,
            fees_eur=fee_eur, exit_strategy={"stop_loss_pct": exit_cfg["stop_loss_pct"],
                                             "take_profit_pct": exit_cfg["take_profit_pct"]},
            opened_at=self.clock())
        self.db.link_execution(execution_id, position_id)
        account = "démo" if mode == "PAPER" else "réel"
        self.db.add_alert("info", f"Achat ({account}) : {mint[:4]}…{mint[-4:]} pour {cost_eur:.2f} €")
        return Outcome("EXECUTED", f"position {position_id} ouverte ({cost_eur:.2f} €)", decision, position_id)

    def _record_execution(self, result: TradeResult, key: str, settings: Any, *, sol_in: bool, mint: str,
                          position_id: int | None = None) -> int:
        sol = settings["tokens"]["sol_mint"]
        pending = (not result.ok) and result.request_id and result.error and "à vérifier" in result.error
        status = "CONFIRMED" if result.ok else ("SENT" if pending else "FAILED")
        return self.db.add_execution(
            side=result.side, mode=result.mode, status=status, position_id=position_id, decision_key=key,
            request_id=result.request_id, tx_signature=result.tx_signature,
            in_mint=sol if sol_in else mint, out_mint=mint if sol_in else sol,
            in_amount_raw=result.in_amount_raw, out_amount_raw=result.out_amount_raw,
            fee_lamports=result.network_fee_lamports, error=result.error)

    def _is_auto(self, wallet: str) -> bool:
        return any(t["address"] == wallet and t.get("source") == SOURCE_AUTO for t in self.db.list_traders())

    async def hunt(self) -> AgentReport:
        """Un passage de HUNTER : recherche, évaluation et remplacement des traders."""
        settings = self._settings()
        report = await HunterAgent(getattr(self.providers, "gmgn", None), self.db, settings,
                                   jupiter=self.providers.jupiter,
                                   rpc=getattr(self.providers, "public_rpc", None) or self.providers.rpc,
                                   dex=self.providers.dex).cycle(now=self.clock())
        save_report(self.db, report)
        for change in (report.data or {}).get("changes", []):
            self.db.add_alert("info", f"HUNTER : {change}")
        if self.monitor is not None:
            self.monitor.set_wallets([t["address"] for t in self.db.list_traders(active_only=True)])
        return report

    async def _hunter_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            settings = self._settings()
            interval = float(settings["discovery"]["interval_minutes"]) * 60
            if settings["discovery"]["enabled"]:
                try:
                    await self.hunt()
                except Exception as exc:  # noqa: BLE001
                    self.last_error = redact(f"HUNTER : {type(exc).__name__}: {exc}")
                    log.exception("Erreur de HUNTER")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass

    async def _refresh_trader(self, wallet: str) -> None:
        if wallet in self._head_running:
            return
        self._head_running.add(wallet)
        try:
            settings = self._settings()
            report = await HeadAgent(self.providers.rpc, self.db, settings, usdc_mint=settings["tokens"]["usdc_mint"],
                                     jupiter=self.providers.jupiter).evaluate(wallet)
            save_report(self.db, report)
        finally:
            self._head_running.discard(wallet)

    # -------------------------------------------------------------- ventes
    async def review_exits(self) -> list[dict[str, Any]]:
        """Passage d'EXIT sur toutes les positions ; ventes soumises au JUDGE puis exécutées."""
        if not self.db.open_positions() and not self.db.query_one(
                "SELECT 1 AS x FROM executions WHERE status IN ('PENDING','SENT')"):
            return []
        settings = self._settings()
        exit_agent = ExitAgent(self.providers.jupiter, self.providers.dex, self.db, settings,
                               timeout=float(settings["agents"]["agent_timeout_seconds"]))
        reports = await exit_agent.review(now=self.clock())
        positions = {str(p["id"]): p for p in self.db.open_positions()}
        results = []
        for report in reports:
            position = positions.get(str(report.data.get("position_id")))
            if position is not None and report.data.get("unrealized_pct") is not None:
                self.portfolio.unrealized_eur[int(position["id"])] = (
                    float(position["cost_eur"] or 0) * float(report.data["unrealized_pct"]) / 100)
            signature = f"{report.status}|{report.data.get('action')}|{report.summary[:40]}"
            if self._last_exit_status.get(report.ref or "") != signature:
                self._last_exit_status[report.ref or ""] = signature
                save_report(self.db, report)
            if position is not None and report.data.get("action") == "SELL":
                results.append(await self._sell(settings, position, report))
        return results

    async def _sell(self, settings: Any, position: dict[str, Any], report: AgentReport) -> dict[str, Any]:
        pid = int(position["id"])
        now = self.clock()
        retry_at = self._sell_retry_at.get(pid)
        if retry_at and now < retry_at:
            return {"position_id": pid, "stage": "RETRY_LATER"}
        executor = self._executor(position["mode"], settings)
        if executor is None:
            if not self._warned_live_positions:
                self._warned_live_positions = True
                self.db.add_alert("warning", "Une position du compte réel doit être vendue mais le compte réel "
                                             "n'est pas armé dans cette session : arme-le ou vends dans Phantom.")
            return {"position_id": pid, "stage": "NOT_ARMED"}
        judge = Judge(self.db, settings)
        state = await self.system_state(settings)
        decision = judge.evaluate_sell(exit_report=report, state=state)
        if decision.decision != APPROVED:
            return {"position_id": pid, "stage": decision.decision, "reasons": decision.reasons}
        eur_per_sol = await self._eur_per_sol(settings) or self._last_eur_per_sol
        if not eur_per_sol:
            self._sell_retry_at[pid] = now + timedelta(seconds=30)
            return {"position_id": pid, "stage": "NO_PRICE"}
        result = await executor.sell(position["token_mint"], int(position["amount_raw"] or 0))
        self._record_execution(result, f"SELL:{pid}", settings, sol_in=False, mint=position["token_mint"],
                               position_id=pid)
        if not result.ok:
            self._sell_retry_at[pid] = now + timedelta(seconds=30)
            self.db.add_alert("warning", f"Vente non exécutée (position {pid}) : {result.error} — nouvel essai "
                                         "dans 30 s.")
            return {"position_id": pid, "stage": "FAILED", "error": result.error}
        fee_eur = result.fee_eur(eur_per_sol)
        proceeds = result.out_amount_raw / LAMPORTS * eur_per_sol - fee_eur
        closed = self.db.close_position(pid, proceeds_eur=proceeds, fees_eur=fee_eur, closed_at=now)
        self.portfolio.unrealized_eur.pop(pid, None)
        self._sell_retry_at.pop(pid, None)
        account = "démo" if position["mode"] == "PAPER" else "réel"
        self.db.add_alert("info", f"Vente ({account}) position {pid} : {closed['realized_pnl_eur']:+.2f} € "
                                  f"({report.reasons[0] if report.reasons else 'sortie'})")
        return {"position_id": pid, "stage": "SOLD", "pnl_eur": closed["realized_pnl_eur"]}

    # ---------------------------------------------------------- boucles
    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.last_error = redact(f"{type(task.exception()).__name__}: {task.exception()}")
            log.error("Tâche du moteur en erreur : %s", self.last_error)

    MAX_PENDING = 40              # au-delà, les transactions arrivent trop tard pour être copiées
    MAX_NOTICES_PER_MINUTE = 60   # un vrai trader ne fait pas une transaction par seconde

    async def _on_notice(self, notice: SignatureNotice) -> None:
        now = asyncio.get_running_loop().time()
        times = [t for t in self._notice_times.get(notice.wallet, []) if now - t < 60] + [now]
        self._notice_times[notice.wallet] = times
        if len(times) > self.MAX_NOTICES_PER_MINUTE:
            self._disable_flooding(notice.wallet, len(times))
            return
        if len(self._tasks) >= self.MAX_PENDING:
            self.dropped_notices += 1  # trop tard de toute façon (le JUDGE refuse au-delà de 20 s)
            return
        self._spawn(self.handle_notice(notice))

    def _disable_flooding(self, wallet: str, count: int) -> None:
        trader = next((t for t in self.db.list_traders(active_only=True) if t["address"] == wallet), None)
        if trader is None:
            return
        self.db.set_trader_active(wallet, False)
        if self.monitor is not None:
            self.monitor.set_wallets([t["address"] for t in self.db.list_traders(active_only=True)])
        self.db.add_alert("warning", f"{wallet[:4]}…{wallet[-4:]} désactivé : {count} transactions en 1 minute. "
                                     "Ce n'est pas un trader copiable (adresse d'un token, d'un pool ou d'un robot).")

    async def _remove_token_addresses(self) -> None:
        """Un « trader » qui est en réalité l'adresse d'un token est désactivé (sinon chaque achat du token
        réveillerait le bot)."""
        for trader in self.db.list_traders(active_only=True):
            try:
                info = await self.providers.rpc.get_mint_info(trader["address"])
            except Exception:  # noqa: BLE001 - compte qui n'est pas un token, ou RPC indisponible
                continue
            if info is not None:
                self.db.set_trader_active(trader["address"], False)
                self.db.add_alert("warning", f"{trader['address'][:4]}…{trader['address'][-4:]} est l'adresse d'un "
                                             "TOKEN, pas d'un trader : retiré de la surveillance.")

    async def _exit_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.review_exits()
                self.db.heartbeat("engine", "OK", f"{len(self.db.open_positions())} position(s) surveillée(s)")
            except Exception as exc:  # noqa: BLE001
                self.last_error = redact(f"EXIT : {type(exc).__name__}: {exc}")
                log.exception("Erreur de la boucle EXIT")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.exit_interval)
            except TimeoutError:
                pass

    async def _traders_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                wallets = [t["address"] for t in self.db.list_traders(active_only=True)]
                if self.monitor is not None:
                    self.monitor.set_wallets(wallets)
                settings = self._settings()
                head = HeadAgent(self.providers.rpc, self.db, settings, usdc_mint=settings["tokens"]["usdc_mint"])
                manual = [t["address"] for t in self.db.list_traders(active_only=True)
                          if t.get("source") != SOURCE_AUTO]  # les traders automatiques sont notés par HUNTER
                for wallet in manual:
                    if head.needs_refresh(wallet, now=self.clock()) and wallet not in self._head_running:
                        self._spawn(self._refresh_trader(wallet))
                        break  # une évaluation à la fois (300 crédits Helius chacune)
            except Exception as exc:  # noqa: BLE001
                self.last_error = redact(f"traders : {type(exc).__name__}: {exc}")
                log.exception("Erreur de la boucle des traders")
            try:
                await asyncio.wait_for(stop.wait(), timeout=60)
            except TimeoutError:
                pass

    async def run(self, stop: asyncio.Event) -> None:
        """Fait tourner le moteur jusqu'à ``stop`` (surveillance, achats, sorties)."""
        from data.wallet_monitor import PollingMonitor, WalletMonitor
        await self._remove_token_addresses()
        wallets = [t["address"] for t in self.db.list_traders(active_only=True)]
        if self.monitor_factory is not None:
            self.monitor = self.monitor_factory(self.providers.ws_url, wallets, self._on_notice, rpc=self.providers.rpc)
        elif getattr(self.providers, "keyless_rpc", False):
            interval = float(self._settings()["providers"]["public_rpc"].get("poll_seconds", 4.0))
            self.monitor = PollingMonitor(self.providers.ws_url, wallets, self._on_notice, rpc=self.providers.rpc,
                                          interval=interval)
        else:
            self.monitor = WalletMonitor(self.providers.ws_url, wallets, self._on_notice, rpc=self.providers.rpc)
        self.monitor.seed_last_signatures(self.db.last_signatures())
        self.started_at = self.clock()
        self.db.heartbeat("engine", "OK", "démarré")
        try:
            await asyncio.gather(self.monitor.run(stop), self._exit_loop(stop), self._traders_loop(stop),
                                 self._hunter_loop(stop))
        finally:
            for task in list(self._tasks):
                task.cancel()
            self.db.heartbeat("engine", "STOPPED", "arrêté")

    def status(self) -> dict[str, Any]:
        return {"started_at": self.started_at.isoformat() if self.started_at else None,
                "live_armed": self.live_armed, "last_error": self.last_error,
                "monitor": self.monitor.status() if self.monitor is not None else None,
                "tasks": len(self._tasks), "dropped_notices": self.dropped_notices}

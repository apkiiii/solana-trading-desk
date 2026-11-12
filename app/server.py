"""Serveur local de l'application : sert l'interface et une API JSON (127.0.0.1 uniquement).

Le même serveur pourra tourner sur un VPS (version 0.8) : l'interface sera alors ouverte à
distance par un tunnel sécurisé, sans rien changer à l'écran.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

from aiohttp import web

from agents.base import _jsonable
from app import services
from config.profiles import PROFILES, apply_profile, detect_profile
from config.settings import EDITABLE_SECTIONS, SettingsError, apply_user_change, load_settings
from core import updater
from core.paths import BUNDLE_DIR, DATA_ROOT, EMERGENCY_STOP_FILE, UPDATES_DIR
from core.validation import is_valid_address
from core.version import RELEASE_NAME, __version__
from data.database import Database, DatabaseError
from engine.scheduler import entries_window_open, next_change
from security.secrets import SecretsError, load_secrets, mask, save_api_keys

log = logging.getLogger(__name__)
WEB_DIR = BUNDLE_DIR / "web"
AGENT_NAMES = ("SCOUT", "HEAD", "WHALE", "RISK", "SNIPER", "EXIT", "JUDGE")
KEY_NAMES = ("HELIUS_API_KEY", "JUPITER_API_KEY", "GMGN_API_KEY", "RUGCHECK_API_KEY")


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(_jsonable(data), status=status, dumps=lambda d: json.dumps(d, ensure_ascii=False))


def _error(message: str, status: int = 400) -> web.Response:
    return _json({"error": message}, status)


class DeskServer:
    def __init__(self, db: Database, *, check_updates: bool = True):
        self.db = db
        self.check_updates = check_updates
        self.last_seen = time.monotonic()
        self.update_info: updater.UpdateInfo | None = None
        self.update_checked_at: str | None = None
        self.update_error: str | None = None
        self.quit_event = asyncio.Event()
        self.app = web.Application(middlewares=[self._middleware])
        self._routes()

    # ------------------------------------------------------------ infrastructure
    @web.middleware
    async def _middleware(self, request: web.Request, handler):
        self.last_seen = time.monotonic()
        try:
            response = await handler(request)
        except web.HTTPException:
            raise
        except (SettingsError, SecretsError, DatabaseError, ValueError, PermissionError) as exc:
            return _error(str(exc), 400)
        except Exception as exc:  # noqa: BLE001
            log.exception("Erreur API %s", request.path)
            return _error(f"Erreur interne : {type(exc).__name__}", 500)
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def _routes(self) -> None:
        r = self.app.router
        r.add_get("/", self.index)
        r.add_get("/api/ping", self.ping)
        r.add_get("/api/state", self.state)
        r.add_get("/api/agents", self.agents)
        r.add_get("/api/stream", self.stream)
        r.add_get("/api/opportunities", self.opportunities)
        r.add_get("/api/decisions", self.decisions)
        r.add_get("/api/traders", self.traders)
        r.add_post("/api/traders", self.add_trader)
        r.add_post("/api/traders/{address}/toggle", self.toggle_trader)
        r.add_post("/api/traders/{address}/evaluate", self.evaluate_trader)
        r.add_post("/api/analyze", self.analyze)
        r.add_get("/api/journal", self.journal)
        r.add_get("/api/settings", self.settings)
        r.add_post("/api/settings", self.change_setting)
        r.add_post("/api/profile", self.profile)
        r.add_post("/api/account", self.account)
        r.add_post("/api/emergency", self.emergency)
        r.add_get("/api/keys", self.keys)
        r.add_post("/api/keys", self.save_keys)
        r.add_post("/api/diagnose", self.diagnose)
        r.add_get("/api/update", self.update_status)
        r.add_post("/api/update/check", self.update_check)
        r.add_post("/api/update/install", self.update_install)
        r.add_post("/api/quit", self.quit)
        if WEB_DIR.exists():
            r.add_static("/static", WEB_DIR)
        self.app.on_startup.append(self._on_startup)

    async def _on_startup(self, _app: web.Application) -> None:
        if self.check_updates:
            asyncio.get_running_loop().create_task(self._update_loop())

    async def _update_loop(self) -> None:
        await asyncio.sleep(3)
        hours = float(load_settings(self.db)["updates"]["check_interval_hours"])
        while True:
            await self._check_update()
            await asyncio.sleep(hours * 3600)

    async def _check_update(self) -> None:
        cfg = load_settings(self.db)["updates"]
        try:
            self.update_info = await asyncio.to_thread(updater.check_for_update, cfg["repository"],
                                                       cfg["installer_prefix"])
            self.update_error = None
        except updater.UpdateError as exc:
            self.update_error = str(exc)
        self.update_checked_at = datetime.now(UTC).isoformat(timespec="seconds")

    # ------------------------------------------------------------------- pages
    async def index(self, _request: web.Request) -> web.StreamResponse:
        return web.FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})

    async def ping(self, _request: web.Request) -> web.Response:
        return _json({"app": "SolanaTradingDesk", "version": __version__})

    # ------------------------------------------------------------------- état
    def _state(self) -> dict[str, Any]:
        settings = load_settings(self.db)
        control = self.db.get_control()
        cap = settings["capital"]
        now = datetime.now(UTC)
        one = self.db.query_one
        demo_mode = control["mode"] != "LIVE"
        mode_filter = "LIVE" if not demo_mode else "PAPER"
        positions = one("SELECT COUNT(*) n, COALESCE(SUM(cost_eur),0) c FROM positions WHERE status='OPEN' AND mode=?",
                        (mode_filter,))
        realized = one("SELECT COALESCE(SUM(realized_pnl_eur),0) p, COALESCE(SUM(fees_eur),0) f FROM positions "
                       "WHERE mode=?", (mode_filter,))
        executed = one("SELECT COUNT(*) n FROM executions WHERE status='CONFIRMED' AND mode=?", (mode_filter,))["n"]
        approved = one("SELECT COUNT(*) n FROM decisions WHERE decision='APPROVED' AND side='BUY'")["n"]
        rejected = one("SELECT COUNT(*) n FROM decisions WHERE decision='REJECTED'")["n"]
        secrets_ok = True
        try:
            secrets = load_secrets()
        except SecretsError:
            secrets_ok, secrets = False, None
        traders = self.db.list_traders(active_only=True)
        schedule = settings["schedule"]
        change = next_change(schedule, now, horizon_hours=48)
        demo_balance = float(settings["demo"]["start_balance_eur"]) + float(realized["p"])
        return {
            "version": __version__, "release_name": RELEASE_NAME, "data_root": str(DATA_ROOT),
            "account": "demo" if demo_mode else "real", "mode": control["mode"],
            "bot_state": control["bot_state"], "manual_stop": control["manual_stop"],
            "emergency": control["emergency_stop"] or EMERGENCY_STOP_FILE.exists(),
            "engine_available": False,
            "schedule": {"enabled": schedule["enabled"], "open": entries_window_open(schedule, now),
                         "start": schedule["start"], "stop": schedule["stop"],
                         "next_change": change.isoformat() if change else None},
            "capital": {"total": cap["total_eur"], "reserve": cap["reserve_eur"],
                        "budget": cap["trading_budget_eur"], "fee_reserve": cap["fee_reserve_eur"],
                        "max_position": cap["max_position_eur"], "max_open": cap["max_open_positions"],
                        "max_daily_loss": cap["max_daily_loss_eur"], "invested": positions["c"],
                        "available": cap["trading_budget_eur"] - positions["c"],
                        "realized_pnl": realized["p"], "fees": realized["f"], "demo_balance": demo_balance},
            "counts": {"open_positions": positions["n"], "executed": executed, "approved": approved,
                       "rejected": rejected, "traders": len(traders), "max_traders": cap["max_traders"],
                       "events": one("SELECT COUNT(*) n FROM source_events")["n"]},
            "credits": {"used": self.db.month_credits("helius"),
                        "budget": settings["providers"]["helius"]["monthly_credit_budget"]},
            "venue": settings["execution"]["venue"], "profile": detect_profile(settings),
            "keys": {"ok": secrets_ok, "helius": bool(secrets and (secrets.helius_api_key or secrets.rpc_url_override)),
                     "jupiter": bool(secrets and secrets.jupiter_api_key),
                     "gmgn": bool(secrets and secrets.gmgn_api_key)},
            "update": {"available": self.update_info.version if self.update_info else None},
            "first_run": not traders and not (secrets and secrets.helius_api_key),
        }

    async def state(self, _request: web.Request) -> web.Response:
        return _json(self._state())

    # ----------------------------------------------------------------- agents
    def _agents(self) -> list[dict[str, Any]]:
        result = []
        for name in AGENT_NAMES:
            if name == "JUDGE":
                rows = self.db.recent_decisions(5)
                history = [{"status": d["decision"], "summary": "; ".join(d["reasons"][:2]),
                            "created_at": d["created_at"], "ref": d["token_mint"]} for d in rows]
            else:
                history = [{"status": r["status"], "summary": r["summary"], "created_at": r["created_at"],
                            "confidence": r["confidence"], "ref": r["ref"],
                            "reasons": r["payload"].get("reasons", [])} for r in self.db.latest_agent_reports(name, 6)]
            result.append({"name": name, "history": history, "last": history[0] if history else None})
        return result

    async def agents(self, _request: web.Request) -> web.Response:
        return _json({"agents": self._agents()})

    async def stream(self, request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store"})
        await response.prepare(request)
        row = self.db.query_one("SELECT COALESCE(MAX(id), 0) AS m FROM agent_reports")
        last_id = row["m"] if row else 0
        last_decision = self.db.query_one("SELECT COALESCE(MAX(id), 0) AS m FROM decisions")["m"]
        try:
            while True:
                rows = self.db.query_all("SELECT id, agent, status, summary, created_at FROM agent_reports "
                                         "WHERE id > ? ORDER BY id LIMIT 50", (last_id,))
                for item in rows:
                    last_id = item["id"]
                    await response.write(f"data: {json.dumps({'type': 'agent', **item}, ensure_ascii=False)}\n\n"
                                         .encode())
                decisions = self.db.query_all("SELECT id, decision, token_mint, created_at FROM decisions "
                                              "WHERE id > ? ORDER BY id LIMIT 20", (last_decision,))
                for item in decisions:
                    last_decision = item["id"]
                    payload = {"type": "agent", "agent": "JUDGE", "status": item["decision"],
                               "summary": f"Décision {item['decision']} — {item['token_mint'] or ''}",
                               "created_at": item["created_at"]}
                    await response.write(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode())
                await response.write(b": ping\n\n")
                self.last_seen = time.monotonic()
                await asyncio.sleep(1.0)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return response

    async def opportunities(self, _request: web.Request) -> web.Response:
        return _json({"items": services.top_opportunities(self.db)})

    async def decisions(self, _request: web.Request) -> web.Response:
        rows = self.db.recent_decisions(100)
        for row in rows:
            evidence = row.pop("evidence", {})
            row["checks"] = evidence.get("checks", [])
        return _json({"items": rows})

    # ---------------------------------------------------------------- traders
    async def traders(self, _request: web.Request) -> web.Response:
        return _json({"items": self.db.list_traders()})

    async def add_trader(self, request: web.Request) -> web.Response:
        body = await request.json()
        address = str(body.get("address", "")).strip()
        if not is_valid_address(address):
            return _error("Adresse Solana invalide.")
        max_traders = int(load_settings(self.db)["capital"]["max_traders"])
        self.db.upsert_trader(address, (body.get("label") or "").strip() or None, max_traders=max_traders)
        return _json({"ok": True})

    async def toggle_trader(self, request: web.Request) -> web.Response:
        address = request.match_info["address"]
        trader = next((t for t in self.db.list_traders() if t["address"] == address), None)
        if trader is None:
            return _error("Trader inconnu.", 404)
        if not trader["active"]:
            active = len(self.db.list_traders(active_only=True))
            if active >= int(load_settings(self.db)["capital"]["max_traders"]):
                return _error("Nombre maximal de traders actifs atteint.")
        self.db.set_trader_active(address, not trader["active"])
        return _json({"ok": True})

    async def evaluate_trader(self, request: web.Request) -> web.Response:
        report = await services.evaluate_trader(self.db, request.match_info["address"])
        return _json({"status": report.status, "summary": report.summary, "reasons": report.reasons})

    async def analyze(self, request: web.Request) -> web.Response:
        mint = str((await request.json()).get("mint", "")).strip()
        if not is_valid_address(mint):
            return _error("Adresse de token invalide.")
        result = await services.analyze_token(self.db, mint)
        result["reports"] = [{"agent": r.agent, "status": r.status, "summary": r.summary, "reasons": r.reasons,
                              "confidence": r.confidence, "checks": r.data.get("checks", []),
                              "data": {k: v for k, v in r.data.items() if k != "checks"}}
                             for r in result["reports"]]
        return _json(result)

    async def journal(self, _request: web.Request) -> web.Response:
        positions = self.db.query_all("SELECT * FROM positions ORDER BY id DESC LIMIT 100")
        events = self.db.recent_source_events(100)
        return _json({"positions": positions, "events": events})

    # --------------------------------------------------------------- réglages
    async def settings(self, _request: web.Request) -> web.Response:
        settings = load_settings(self.db)
        sections = {name: dict(settings[name]) for name in sorted(EDITABLE_SECTIONS)}
        sections["schedule"]["days"] = list(sections["schedule"]["days"])
        return _json({"sections": sections, "profile": detect_profile(settings),
                      "profiles": {k: {"title": v["title"], "description": v["description"]}
                                   for k, v in PROFILES.items()}})

    async def change_setting(self, request: web.Request) -> web.Response:
        body = await request.json()
        apply_user_change(self.db, str(body["section"]), str(body["key"]), body["value"], actor="dashboard")
        return await self.settings(request)

    async def profile(self, request: web.Request) -> web.Response:
        apply_profile(self.db, str((await request.json())["name"]))
        return await self.settings(request)

    async def account(self, request: web.Request) -> web.Response:
        account = str((await request.json()).get("account"))
        if account == "demo":
            self.db.set_control(actor="dashboard", mode="PAPER")
            return _json({"ok": True, "account": "demo"})
        if account == "real":
            return _error("Le compte réel s'activera avec l'exécution des ordres (version 0.7), après vérification "
                          "du wallet, des limites et une confirmation de ta part.", 409)
        return _error("Compte inconnu.")

    async def emergency(self, request: web.Request) -> web.Response:
        active = bool((await request.json()).get("active"))
        self.db.set_control(actor="dashboard", emergency_stop=active)
        if active:
            EMERGENCY_STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
            EMERGENCY_STOP_FILE.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
        else:
            EMERGENCY_STOP_FILE.unlink(missing_ok=True)
        return _json({"ok": True, "emergency": active})

    # ------------------------------------------------------------- clés et tests
    async def keys(self, _request: web.Request) -> web.Response:
        try:
            secrets = load_secrets()
        except SecretsError as exc:
            return _error(str(exc))
        values = {"HELIUS_API_KEY": secrets.helius_api_key, "JUPITER_API_KEY": secrets.jupiter_api_key,
                  "GMGN_API_KEY": secrets.gmgn_api_key, "RUGCHECK_API_KEY": secrets.rugcheck_api_key}
        return _json({"keys": {name: {"set": bool(values[name]), "masked": mask(values[name])} for name in KEY_NAMES}})

    async def save_keys(self, request: web.Request) -> web.Response:
        body = await request.json()
        values = {k: str(v).strip() for k, v in body.items() if k in KEY_NAMES and str(v).strip()}
        if values:
            save_api_keys(values)
        return await self.keys(request)

    async def diagnose(self, request: web.Request) -> web.Response:
        wallet = (await request.json()).get("wallet") or None
        if wallet and not is_valid_address(wallet):
            return _error("Adresse invalide.")
        return _json({"output": await services.run_diagnostic(wallet)})

    # ----------------------------------------------------------- mises à jour
    def _update_payload(self) -> dict[str, Any]:
        info = self.update_info
        return {"current": __version__, "checked_at": self.update_checked_at, "error": self.update_error,
                "available": None if info is None else {"version": info.version, "title": info.title,
                                                        "notes": info.notes, "size": info.installer_size}}

    async def update_status(self, _request: web.Request) -> web.Response:
        return _json(self._update_payload())

    async def update_check(self, _request: web.Request) -> web.Response:
        await self._check_update()
        return _json(self._update_payload())

    async def update_install(self, _request: web.Request) -> web.Response:
        if self.update_info is None:
            return _error("Aucune mise à jour disponible.")
        path = await asyncio.to_thread(updater.download_update, self.update_info, UPDATES_DIR)
        updater.launch_installer(path)
        asyncio.get_running_loop().call_later(1.0, self.quit_event.set)
        return _json({"ok": True, "message": "Installation en cours : l'application va redémarrer."})

    async def quit(self, _request: web.Request) -> web.Response:
        asyncio.get_running_loop().call_later(0.3, self.quit_event.set)
        return _json({"ok": True})


async def serve(db: Database, host: str = "127.0.0.1", port: int = 8766, *, check_updates: bool = True,
                idle_exit_seconds: float | None = 180.0, on_ready=None) -> None:
    """Lance le serveur et attend : arrêt par /api/quit, ou après une longue inactivité de l'interface."""
    desk = DeskServer(db, check_updates=check_updates)
    runner = web.AppRunner(desk.app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("Interface disponible sur http://%s:%d/", host, port)
    if on_ready:
        on_ready()
    try:
        while not desk.quit_event.is_set():
            try:
                await asyncio.wait_for(desk.quit_event.wait(), timeout=5)
            except TimeoutError:
                pass
            if idle_exit_seconds and time.monotonic() - desk.last_seen > idle_exit_seconds:
                log.info("Interface fermée depuis %.0f s : arrêt.", idle_exit_seconds)
                break
    finally:
        await runner.cleanup()

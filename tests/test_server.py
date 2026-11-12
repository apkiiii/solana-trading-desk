"""Tests de l'API locale de l'interface (serveur réel en mémoire, aucun accès Internet)."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

import app.server as server_module
import security.secrets as secrets_module
from app.server import DeskServer
from security.trading_wallet import TradingWalletStore
from tests.conftest import WALLET


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(server_module, "EMERGENCY_STOP_FILE", tmp_path / "EMERGENCY_STOP")
    monkeypatch.setattr(secrets_module, "ENV_FILE", tmp_path / ".env")
    for name in ("HELIUS_API_KEY", "JUPITER_API_KEY", "GMGN_API_KEY", "RUGCHECK_API_KEY", "SOLANA_RPC_URL"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def run_with_client(db, scenario, keys=None, **kwargs):
    async def go():
        if "wallet" not in kwargs:
            kwargs["wallet"] = TradingWalletStore(Path(db.path).parent / "wallet", use_keyring=False)
        desk = DeskServer(db, check_updates=False, gmgn_keys=keys, **kwargs)
        async with TestClient(TestServer(desk.app)) as client:
            return await scenario(client, desk)
    return asyncio.run(go())


def test_index_state_and_first_run(db, isolated):
    async def scenario(client, _desk):
        page = await client.get("/")
        assert page.status == 200 and "Trading Desk" in await page.text()
        css = await client.get("/static/app.css")
        assert css.status == 200
        state = await (await client.get("/api/state")).json()
        assert state["account"] == "demo" and state["first_run"] is True and state["venue"] == "jupiter"
        assert state["profile"] == "equilibre" and state["capital"]["demo_balance"] == 50.0
        assert state["engine_available"] is True and state["engine_running"] is False
        assert state["wallet"] is None and state["live_armed"] is False
    run_with_client(db, scenario)


def test_traders_profiles_settings_and_validation(db, isolated):
    async def scenario(client, _desk):
        bad = await client.post("/api/traders", json={"address": "nope"})
        assert bad.status == 400
        ok = await client.post("/api/traders", json={"address": WALLET, "label": "GMGN smart money"})
        assert ok.status == 200
        items = (await (await client.get("/api/traders")).json())["items"]
        assert items[0]["label"] == "GMGN smart money"
        settings = await (await client.post("/api/profile", json={"name": "prudent"})).json()
        assert settings["profile"] == "prudent" and settings["sections"]["capital"]["max_position_eur"] == 1.5
        changed = await (await client.post("/api/settings", json={"section": "execution", "key": "venue",
                                                                   "value": "jupiter"})).json()
        assert changed["sections"]["execution"]["venue"] == "jupiter"
        refused = await client.post("/api/settings", json={"section": "capital", "key": "max_position_eur",
                                                            "value": 500})
        assert refused.status == 400 and "budget" in (await refused.json())["error"]
        refused_venue = await client.post("/api/settings", json={"section": "execution", "key": "venue",
                                                                  "value": "pocketoption"})
        assert refused_venue.status == 400
    run_with_client(db, scenario)


def test_real_account_is_locked_and_emergency_stop_works(db, isolated):
    async def scenario(client, _desk):
        real = await client.post("/api/account", json={"account": "real"})
        assert real.status == 409
        assert db.get_control()["mode"] != "LIVE"
        demo = await client.post("/api/account", json={"account": "demo"})
        assert demo.status == 200 and db.get_control()["mode"] == "PAPER"
        await client.post("/api/emergency", json={"active": True})
        assert (isolated / "EMERGENCY_STOP").exists() and db.get_control()["emergency_stop"] is True
        assert (await (await client.get("/api/state")).json())["emergency"] is True
        await client.post("/api/emergency", json={"active": False})
        assert not (isolated / "EMERGENCY_STOP").exists()
    run_with_client(db, scenario)


def test_keys_are_saved_masked_and_never_returned(db, isolated):
    async def scenario(client, _desk):
        saved = await (await client.post("/api/keys", json={"HELIUS_API_KEY": "helius-secret-123456",
                                                            "WALLET_PRIVATE_KEY": "nope"})).json()
        assert saved["keys"]["HELIUS_API_KEY"]["set"] is True
        assert "helius-secret-123456" not in str(saved)
        assert "WALLET_PRIVATE_KEY" not in (isolated / ".env").read_text(encoding="utf-8")
        state = await (await client.get("/api/state")).json()
        assert state["keys"]["helius"] is True and state["first_run"] is False
    run_with_client(db, scenario)


def test_agents_decisions_journal_and_quit(db, isolated):
    from agents.base import AgentReport, save_report
    save_report(db, AgentReport("RISK", "Tok", "BLOCK", "Token refusé", ["liquidité"], {"checks": []}, 0.9))

    async def scenario(client, desk):
        agents = (await (await client.get("/api/agents")).json())["agents"]
        names = [a["name"] for a in agents]
        assert names == ["HUNTER", "SCOUT", "HEAD", "WHALE", "RISK", "SNIPER", "EXIT", "JUDGE"]
        risk = next(a for a in agents if a["name"] == "RISK")
        assert risk["last"]["status"] == "BLOCK"
        assert (await client.get("/api/decisions")).status == 200
        assert (await client.get("/api/journal")).status == 200
        assert (await client.get("/api/opportunities")).status == 200
        await client.post("/api/quit", json={})
        await asyncio.sleep(0.5)
        assert desk.quit_event.is_set()
    run_with_client(db, scenario)


def test_gmgn_signing_key_flow(db, isolated):
    from security.gmgn_keys import GmgnKeyStore
    store = GmgnKeyStore(isolated, use_keyring=False)

    async def scenario(client, _desk):
        assert (await (await client.get("/api/gmgn/key")).json())["exists"] is False
        created = await (await client.post("/api/gmgn/key", json={})).json()
        assert created["public_pem"].startswith("-----BEGIN PUBLIC KEY-----")
        assert "PRIVATE" not in str(created)
        again = await client.post("/api/gmgn/key", json={})
        assert again.status == 400
        shown = await (await client.get("/api/gmgn/key")).json()
        assert shown["public_pem"] == created["public_pem"]
    run_with_client(db, scenario, keys=store)


# ----------------------------------------------------------- moteur, wallet, compte réel
class FakeEngine:
    def __init__(self):
        self.live_armed = False
        self.ran = asyncio.Event()

    async def run(self, stop):
        self.ran.set()
        await stop.wait()

    def status(self):
        return {"live_armed": self.live_armed}


class FakeProviders:
    class rpc:  # noqa: N801
        @staticmethod
        async def get_balance(address):
            return 250_000_000

    async def aclose(self):
        pass


def test_start_stop_wallet_and_real_account(db, isolated):
    from solders.keypair import Keypair

    from tests.test_trading_wallet import b58encode

    engines = []

    def engine_factory(_providers):
        engines.append(FakeEngine())
        return engines[-1]

    async def scenario(client, desk):
        # sans trader : DÉMARRER refusé avec une explication
        r = await client.post("/api/bot", json={"action": "start"})
        assert r.status == 409 and "trader" in (await r.json())["error"]
        await client.post("/api/traders", json={"address": WALLET})
        r = await client.post("/api/bot", json={"action": "start"})
        assert r.status == 200
        await asyncio.wait_for(engines[0].ran.wait(), 1)
        state = await (await client.get("/api/state")).json()
        assert state["bot_state"] == "RUNNING" and state["engine_running"] and state["mode"] == "PAPER"
        assert desk.keeps_running()                         # fenêtre fermée : le bot continue
        # compte réel : impossible sans wallet dédié, puis sans confirmation
        r = await client.post("/api/account", json={"account": "real", "confirm": True})
        assert r.status == 409 and "Phantom" in (await r.json())["error"]
        phrase = await client.post("/api/wallet", json={"private_key": " ".join(["word"] * 12)})
        assert phrase.status == 400 and "phrase de récupération" in (await phrase.json())["error"]
        kp = Keypair()
        imported = await (await client.post("/api/wallet", json={"private_key": b58encode(bytes(kp))})).json()
        assert imported["address"] == str(kp.pubkey())
        info = await (await client.get("/api/wallet")).json()
        assert info["address"] == str(kp.pubkey()) and info["balance"] == {"sol": 0.25}
        assert b58encode(bytes(kp)) not in str(info)          # la clé n'est jamais renvoyée
        r = await client.post("/api/account", json={"account": "real"})
        assert r.status == 409
        r = await client.post("/api/account", json={"account": "real", "confirm": True})
        assert r.status == 200 and engines[0].live_armed is True
        assert (await (await client.get("/api/state")).json())["mode"] == "LIVE"
        # ARRÊT D'URGENCE : tout s'arrête et le compte réel est désarmé
        await client.post("/api/emergency", json={"active": True})
        state = await (await client.get("/api/state")).json()
        assert state["bot_state"] == "STOPPED" and state["live_armed"] is False and engines[0].live_armed is False
        r = await client.post("/api/bot", json={"action": "start"})
        assert r.status == 409
        await client.post("/api/emergency", json={"active": False})
        await client.post("/api/account", json={"account": "demo"})
        # STOPPER sans position ouverte : le moteur s'arrête
        await client.post("/api/bot", json={"action": "start"})
        await client.post("/api/bot", json={"action": "stop"})
        assert not desk.engine_running and not desk.keeps_running()

    run_with_client(db, scenario, providers_factory=lambda: FakeProviders(), engine_factory=engine_factory)


def test_restart_never_resumes_trading_or_real_account(db, isolated):
    db.set_control(actor="dashboard", bot_state="RUNNING", manual_stop=False, mode="LIVE")

    async def scenario(client, desk):
        state = await (await client.get("/api/state")).json()
        assert state["bot_state"] == "STOPPED" and state["manual_stop"] is True
        assert state["mode"] == "PAPER" and state["live_armed"] is False

    run_with_client(db, scenario)


def test_start_without_helius_key_explains_what_to_do(db, isolated):
    async def scenario(client, _desk):
        await client.post("/api/traders", json={"address": WALLET})
        r = await client.post("/api/bot", json={"action": "start"})
        assert r.status == 409 and "Helius" in (await r.json())["error"]
        assert (await (await client.get("/api/state")).json())["bot_state"] == "STOPPED"

    run_with_client(db, scenario)


def test_hunter_lets_the_bot_start_without_manual_traders(db, isolated, monkeypatch):
    engines = []

    def engine_factory(_providers):
        engines.append(FakeEngine())
        return engines[-1]

    async def scenario(client, _desk):
        status = await (await client.get("/api/hunter")).json()
        assert status["gmgn_key"] is False and status["settings"]["target_traders"] == 5
        run = await (await client.post("/api/hunter/run", json={})).json()
        assert run["status"] == "UNAVAILABLE" and "GMGN" in run["summary"]
        r = await client.post("/api/bot", json={"action": "start"})
        assert r.status == 409 and "GMGN" in (await r.json())["error"]
        monkeypatch.setenv("GMGN_API_KEY", "cle-de-test")
        r = await client.post("/api/bot", json={"action": "start"})
        assert r.status == 200
        status = await (await client.get("/api/hunter")).json()
        assert status["gmgn_key"] is True and status["running"] is True

    run_with_client(db, scenario, providers_factory=lambda: FakeProviders(), engine_factory=engine_factory)

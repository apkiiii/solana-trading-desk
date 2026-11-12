"""Base de données SQLite locale (mode WAL : le moteur et l'application peuvent la lire en même temps).

Contient : état de contrôle (START/STOP/urgence/mode), réglages modifiés, traders suivis,
événements détectés, rapports d'agents, décisions du JUDGE, positions, exécutions,
consommation des API, battements de cœur des composants et alertes.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.validation import is_valid_address
from data.models import SwapEvent

MODES = ("SIMULATION", "PAPER", "LIVE")
BOT_STATES = ("STOPPED", "RUNNING")

_MIGRATIONS: list[str] = [
    # --- version 1 ---------------------------------------------------------
    """
    CREATE TABLE settings_overrides (
        section     TEXT NOT NULL,
        key         TEXT NOT NULL,
        value_json  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        updated_by  TEXT NOT NULL,
        PRIMARY KEY (section, key)
    );
    CREATE TABLE settings_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        section TEXT NOT NULL, key TEXT NOT NULL,
        old_json TEXT, new_json TEXT NOT NULL,
        actor TEXT NOT NULL, at TEXT NOT NULL
    );
    CREATE TABLE control (
        id              INTEGER PRIMARY KEY CHECK (id = 1),
        bot_state       TEXT NOT NULL DEFAULT 'STOPPED' CHECK (bot_state IN ('STOPPED','RUNNING')),
        manual_stop     INTEGER NOT NULL DEFAULT 1,
        emergency_stop  INTEGER NOT NULL DEFAULT 0,
        mode            TEXT NOT NULL DEFAULT 'SIMULATION' CHECK (mode IN ('SIMULATION','PAPER','LIVE')),
        updated_at      TEXT NOT NULL,
        updated_by      TEXT NOT NULL
    );
    CREATE TABLE control_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        changes_json TEXT NOT NULL, actor TEXT NOT NULL, at TEXT NOT NULL
    );
    CREATE TABLE traders (
        address     TEXT PRIMARY KEY,
        label       TEXT,
        active      INTEGER NOT NULL DEFAULT 1,
        score       REAL,
        stats_json  TEXT,
        added_at    TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );
    CREATE TABLE source_events (
        signature        TEXT PRIMARY KEY,
        wallet           TEXT NOT NULL,
        slot             INTEGER,
        block_time       INTEGER,
        side             TEXT NOT NULL,
        token_mint       TEXT,
        token_amount_raw TEXT,
        token_decimals   INTEGER,
        quote_mint       TEXT,
        quote_amount_raw TEXT,
        quote_decimals   INTEGER,
        fee_lamports     INTEGER,
        fee_paid_by_wallet INTEGER,
        backfill         INTEGER NOT NULL DEFAULT 0,
        status           TEXT NOT NULL DEFAULT 'NEW',
        detected_at      TEXT NOT NULL
    );
    CREATE INDEX idx_source_events_wallet ON source_events(wallet, block_time);
    CREATE INDEX idx_source_events_mint ON source_events(token_mint, block_time);
    CREATE TABLE agent_reports (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        agent       TEXT NOT NULL,
        ref         TEXT,
        status      TEXT NOT NULL,
        confidence  REAL,
        summary     TEXT,
        payload_json TEXT,
        created_at  TEXT NOT NULL
    );
    CREATE INDEX idx_agent_reports_agent ON agent_reports(agent, created_at);
    CREATE TABLE decisions (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        proposal_key     TEXT NOT NULL UNIQUE,
        side             TEXT NOT NULL,
        token_mint       TEXT,
        source_signature TEXT,
        decision         TEXT NOT NULL CHECK (decision IN ('APPROVED','REJECTED','WAIT')),
        reasons_json     TEXT NOT NULL,
        evidence_json    TEXT NOT NULL,
        mode             TEXT NOT NULL,
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL
    );
    CREATE TABLE positions (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        token_mint       TEXT NOT NULL,
        status           TEXT NOT NULL,
        mode             TEXT NOT NULL,
        source_wallet    TEXT,
        source_signature TEXT,
        opened_at        TEXT NOT NULL,
        closed_at        TEXT,
        amount_raw       TEXT,
        decimals         INTEGER,
        entry_price_usd  REAL,
        cost_eur         REAL,
        fees_eur         REAL,
        proceeds_eur     REAL,
        realized_pnl_eur REAL,
        exit_strategy_json TEXT
    );
    CREATE INDEX idx_positions_status ON positions(status);
    CREATE TABLE executions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        position_id     INTEGER REFERENCES positions(id),
        decision_id     INTEGER REFERENCES decisions(id),
        side            TEXT NOT NULL,
        mode            TEXT NOT NULL,
        request_id      TEXT,
        tx_signature    TEXT UNIQUE,
        status          TEXT NOT NULL,
        in_mint         TEXT, out_mint TEXT,
        in_amount_raw   TEXT, out_amount_raw TEXT,
        fee_lamports    INTEGER,
        error           TEXT,
        created_at      TEXT NOT NULL,
        confirmed_at    TEXT
    );
    CREATE TABLE api_usage (
        day TEXT NOT NULL, provider TEXT NOT NULL,
        requests INTEGER NOT NULL DEFAULT 0,
        credits INTEGER NOT NULL DEFAULT 0,
        errors INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (day, provider)
    );
    CREATE TABLE heartbeats (
        component TEXT PRIMARY KEY, status TEXT NOT NULL, detail TEXT, at TEXT NOT NULL
    );
    CREATE TABLE alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        level TEXT NOT NULL, message TEXT NOT NULL, at TEXT NOT NULL,
        acknowledged INTEGER NOT NULL DEFAULT 0
    );
    """,
]


def utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None = None) -> str:
    return (dt or utc_now()).isoformat(timespec="seconds")


class DatabaseError(RuntimeError):
    pass


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None,
                                     timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self.migrate()

    # ------------------------------------------------------------------ base
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query_one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def query_all(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    @property
    def schema_version(self) -> int:
        with self._lock:
            return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def migrate(self) -> None:
        with self._lock:
            version = self.schema_version
            for index, script in enumerate(_MIGRATIONS[version:], start=version + 1):
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in [s.strip() for s in script.split(";") if s.strip()]:
                        self._conn.execute(statement)
                    self._conn.execute(f"PRAGMA user_version = {index}")
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise
                self._conn.execute("COMMIT")
            if self.query_one("SELECT id FROM control WHERE id = 1") is None:
                self.execute("INSERT INTO control (id, updated_at, updated_by) VALUES (1, ?, 'system')",
                             (_iso(),))

    def journal_mode(self) -> str:
        return str(self.query_one("PRAGMA journal_mode")["journal_mode"])

    # ------------------------------------------------------------- sauvegarde
    def backup(self, dest_dir: Path, keep: int = 14) -> Path:
        """Copie cohérente de la base (API de sauvegarde SQLite), en gardant les ``keep`` plus récentes."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = utc_now().strftime("%Y%m%d-%H%M%S-%f")
        target = dest_dir / f"desk-{stamp}.db"
        with self._lock:
            dst = sqlite3.connect(str(target))
            try:
                self._conn.backup(dst)
            finally:
                dst.close()
        backups = sorted(dest_dir.glob("desk-*.db"))
        for old in backups[:-keep] if keep > 0 else []:
            old.unlink(missing_ok=True)
        return target

    # ---------------------------------------------------------------- contrôle
    def get_control(self) -> dict[str, Any]:
        row = self.query_one("SELECT * FROM control WHERE id = 1")
        assert row is not None
        row["manual_stop"] = bool(row["manual_stop"])
        row["emergency_stop"] = bool(row["emergency_stop"])
        return row

    def set_control(self, *, actor: str, **changes: Any) -> dict[str, Any]:
        allowed = {"bot_state", "manual_stop", "emergency_stop", "mode"}
        unknown = set(changes) - allowed
        if unknown:
            raise DatabaseError(f"Champs de contrôle inconnus : {sorted(unknown)}")
        if "mode" in changes and changes["mode"] not in MODES:
            raise DatabaseError(f"Mode invalide : {changes['mode']}")
        if "bot_state" in changes and changes["bot_state"] not in BOT_STATES:
            raise DatabaseError(f"État invalide : {changes['bot_state']}")
        values = {k: (int(v) if isinstance(v, bool) else v) for k, v in changes.items()}
        if values:
            assignments = ", ".join(f"{k} = :{k}" for k in values)
            with self.transaction() as conn:
                conn.execute(f"UPDATE control SET {assignments}, updated_at = :at, updated_by = :actor WHERE id = 1",
                             {**values, "at": _iso(), "actor": actor})
                conn.execute("INSERT INTO control_audit (changes_json, actor, at) VALUES (?, ?, ?)",
                             (json.dumps(changes), actor, _iso()))
        return self.get_control()

    # ----------------------------------------------------------------- réglages
    def get_setting_overrides(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for row in self.query_all("SELECT section, key, value_json FROM settings_overrides"):
            result.setdefault(row["section"], {})[row["key"]] = json.loads(row["value_json"])
        return result

    def put_setting_override(self, section: str, key: str, value: Any, *, actor: str) -> None:
        new_json = json.dumps(value)
        with self.transaction() as conn:
            old = conn.execute("SELECT value_json FROM settings_overrides WHERE section = ? AND key = ?",
                               (section, key)).fetchone()
            conn.execute(
                "INSERT INTO settings_overrides (section, key, value_json, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(section, key) DO UPDATE SET "
                "value_json = excluded.value_json, updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                (section, key, new_json, _iso(), actor))
            conn.execute("INSERT INTO settings_audit (section, key, old_json, new_json, actor, at) "
                         "VALUES (?, ?, ?, ?, ?, ?)",
                         (section, key, old[0] if old else None, new_json, actor, _iso()))

    # ------------------------------------------------------------------ traders
    def upsert_trader(self, address: str, label: str | None = None, *, max_traders: int | None = None) -> None:
        if not is_valid_address(address):
            raise DatabaseError(f"Adresse Solana invalide : {address!r}")
        existing = self.query_one("SELECT address FROM traders WHERE address = ?", (address,))
        if existing is None and max_traders is not None:
            active = self.query_one("SELECT COUNT(*) AS n FROM traders WHERE active = 1")["n"]
            if active >= max_traders:
                raise DatabaseError(f"Nombre maximal de traders suivis atteint ({max_traders}).")
        now = _iso()
        self.execute(
            "INSERT INTO traders (address, label, active, added_at, updated_at) VALUES (?, ?, 1, ?, ?) "
            "ON CONFLICT(address) DO UPDATE SET label = COALESCE(excluded.label, traders.label), "
            "updated_at = excluded.updated_at", (address, label, now, now))

    def set_trader_active(self, address: str, active: bool) -> None:
        self.execute("UPDATE traders SET active = ?, updated_at = ? WHERE address = ?",
                     (int(active), _iso(), address))

    def update_trader_score(self, address: str, score: float | None, stats: dict[str, Any]) -> None:
        self.execute("UPDATE traders SET score = ?, stats_json = ?, updated_at = ? WHERE address = ?",
                     (score, json.dumps(stats), _iso(), address))

    def list_traders(self, active_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM traders" + (" WHERE active = 1" if active_only else "") + " ORDER BY added_at"
        rows = self.query_all(sql)
        for row in rows:
            row["active"] = bool(row["active"])
            row["stats"] = json.loads(row.pop("stats_json") or "{}")
        return rows

    # ------------------------------------------------------- événements source
    def insert_source_event(self, event: SwapEvent, *, backfill: bool = False) -> bool:
        """Enregistre un swap détecté. Retourne False si la signature est déjà connue (doublon)."""
        cursor = self.execute(
            "INSERT OR IGNORE INTO source_events (signature, wallet, slot, block_time, side, token_mint, "
            "token_amount_raw, token_decimals, quote_mint, quote_amount_raw, quote_decimals, fee_lamports, "
            "fee_paid_by_wallet, backfill, detected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event.signature, event.wallet, event.slot, event.block_time, event.side, event.token_mint,
             str(event.token_amount_raw), event.token_decimals, event.quote_mint, str(event.quote_amount_raw),
             event.quote_decimals, event.fee_lamports, int(event.fee_paid_by_wallet), int(backfill), _iso()))
        return cursor.rowcount == 1

    def has_source_event(self, signature: str) -> bool:
        return self.query_one("SELECT 1 AS x FROM source_events WHERE signature = ?", (signature,)) is not None

    def set_source_event_status(self, signature: str, status: str) -> None:
        self.execute("UPDATE source_events SET status = ? WHERE signature = ?", (status, signature))

    def recent_source_events(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.query_all("SELECT * FROM source_events ORDER BY detected_at DESC LIMIT ?", (limit,))

    def source_events_for_mint(self, mint: str, since_block_time: int, side: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM source_events WHERE token_mint = ? AND block_time >= ?"
        params: list[Any] = [mint, since_block_time]
        if side:
            sql += " AND side = ?"
            params.append(side)
        return self.query_all(sql + " ORDER BY block_time", tuple(params))

    def source_events_for_wallet(self, wallet: str, since_block_time: int = 0) -> list[dict[str, Any]]:
        return self.query_all("SELECT * FROM source_events WHERE wallet = ? AND block_time >= ? ORDER BY block_time",
                              (wallet, since_block_time))

    # ----------------------------------------------------------- rapports d'agents
    def add_agent_report(self, agent: str, ref: str | None, status: str, confidence: float | None, summary: str,
                         payload_json: str, created_at: datetime | None = None) -> int:
        cursor = self.execute(
            "INSERT INTO agent_reports (agent, ref, status, confidence, summary, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", (agent, ref, status, confidence, summary, payload_json, _iso(created_at)))
        return int(cursor.lastrowid)

    def latest_agent_reports(self, agent: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.query_all("SELECT * FROM agent_reports WHERE agent = ? ORDER BY id DESC LIMIT ?", (agent, limit))
        for row in rows:
            row["payload"] = json.loads(row.pop("payload_json") or "{}")
        return rows

    def open_positions(self) -> list[dict[str, Any]]:
        return self.query_all("SELECT * FROM positions WHERE status = 'OPEN' ORDER BY id")

    # ---------------------------------------------------------- consommation API
    def add_api_usage(self, provider: str, *, requests: int = 1, credits: int = 0, errors: int = 0) -> None:
        day = utc_now().strftime("%Y-%m-%d")
        self.execute(
            "INSERT INTO api_usage (day, provider, requests, credits, errors) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(day, provider) DO UPDATE SET requests = requests + excluded.requests, "
            "credits = credits + excluded.credits, errors = errors + excluded.errors",
            (day, provider, requests, credits, errors))

    def month_credits(self, provider: str) -> int:
        month = utc_now().strftime("%Y-%m")
        row = self.query_one("SELECT COALESCE(SUM(credits), 0) AS c FROM api_usage WHERE provider = ? "
                             "AND day LIKE ?", (provider, f"{month}-%"))
        return int(row["c"]) if row else 0

    # --------------------------------------------------- santé et alertes
    def heartbeat(self, component: str, status: str, detail: str = "") -> None:
        self.execute("INSERT INTO heartbeats (component, status, detail, at) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT(component) DO UPDATE SET status = excluded.status, detail = excluded.detail, "
                     "at = excluded.at", (component, status, detail, _iso()))

    def heartbeats(self) -> list[dict[str, Any]]:
        return self.query_all("SELECT * FROM heartbeats ORDER BY component")

    def add_alert(self, level: str, message: str) -> None:
        self.execute("INSERT INTO alerts (level, message, at) VALUES (?, ?, ?)", (level, message, _iso()))

    def list_alerts(self, unacknowledged_only: bool = True, limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM alerts" + (" WHERE acknowledged = 0" if unacknowledged_only else "")
        return self.query_all(sql + " ORDER BY id DESC LIMIT ?", (limit,))

"""Contrat commun des agents.

Chaque agent rend un ``AgentReport`` et ne lève jamais d'exception vers l'appelant :
une erreur ou un dépassement de délai devient un rapport ``UNAVAILABLE``, que le JUDGE
traitera comme une information manquante (donc un refus d'achat).

Statuts :
- ``OK``          analyse faite, rien de bloquant ;
- ``WARN``        analyse faite, points d'attention ;
- ``BLOCK``       analyse faite, au moins un motif de refus ;
- ``UNAVAILABLE`` données insuffisantes, erreur ou délai dépassé ;
- ``IGNORED``     rien à analyser (ex. transaction qui n'est pas un swap) ;
- ``DUPLICATE``   déjà traité.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from core.logs import redact

log = logging.getLogger(__name__)

OK, WARN, BLOCK, UNAVAILABLE, IGNORED, DUPLICATE = "OK", "WARN", "BLOCK", "UNAVAILABLE", "IGNORED", "DUPLICATE"
STATUSES = (OK, WARN, BLOCK, UNAVAILABLE, IGNORED, DUPLICATE)


@dataclass
class AgentReport:
    agent: str
    ref: str | None
    status: str
    summary: str
    reasons: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    confidence: float | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"statut inconnu : {self.status}")

    @property
    def blocking(self) -> bool:
        return self.status in (BLOCK, UNAVAILABLE)

    def age_seconds(self, now: datetime | None = None) -> float:
        return ((now or datetime.now(UTC)) - self.created_at).total_seconds()

    def is_fresh(self, max_age_seconds: float, now: datetime | None = None) -> bool:
        return self.age_seconds(now) <= max_age_seconds


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, float) and value != value:  # NaN
        return None
    return value


def report_to_json(report: AgentReport) -> str:
    return json.dumps(_jsonable({"reasons": report.reasons, "data": report.data}), ensure_ascii=False)


def unavailable(agent: str, ref: str | None, reason: str) -> AgentReport:
    return AgentReport(agent=agent, ref=ref, status=UNAVAILABLE, summary=reason, reasons=[reason], confidence=0.0)


async def guarded(agent: str, ref: str | None, coro: Awaitable[AgentReport], timeout: float) -> AgentReport:
    """Exécute l'analyse d'un agent avec délai maximal ; toute erreur devient UNAVAILABLE."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except TimeoutError:
        return unavailable(agent, ref, f"{agent} n'a pas répondu dans le délai ({timeout:.0f} s)")
    except Exception as exc:  # noqa: BLE001
        log.exception("Erreur de l'agent %s", agent)
        return unavailable(agent, ref, redact(f"{agent} en erreur : {type(exc).__name__}: {exc}"))


def save_report(db: Any, report: AgentReport) -> None:
    db.add_agent_report(report.agent, report.ref, report.status, report.confidence, report.summary,
                        report_to_json(report), report.created_at)

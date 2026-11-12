"""JUDGE : décideur final. Moteur de règles DÉTERMINISTE (aucune IA, aucun hasard).

- Il ne lit les réglages qu'en lecture seule ; aucun agent ne peut les modifier.
- Une décision finale (APPROVED ou REJECTED) est enregistrée avec ses preuves et n'est jamais
  remplacée : un refus est définitif pour cette proposition.
- WAIT : une information manque encore (ex. agent sans réponse) ; si elle n'arrive pas avant
  ``wait_timeout_seconds``, la proposition est REJECTED. WAIT n'autorise jamais un achat.
- Les ventes de protection (EXIT) ne sont bloquées que par l'ARRÊT D'URGENCE.

Les 14 contrôles d'un achat :
 1 qualité du trader            8 nombre de positions / doublon de token
 2 validité de la transaction   9 limite de perte du jour
 3 liquidité                   10 horaires, STOP manuel, bot démarré
 4 risque du token             11 signaux contradictoires (WHALE, vente du trader)
 5 slippage / impact prix      12 transaction déjà traitée
 6 frais                       13 rapports complets et récents
 7 budget et réserve de frais  14 mode LIVE armé, arrêt d'urgence
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agents.base import OK, WARN, AgentReport, _jsonable

APPROVED, REJECTED, WAIT = "APPROVED", "REJECTED", "WAIT"
REQUIRED_AGENTS = ("SCOUT", "HEAD", "RISK", "WHALE", "SNIPER")


@dataclass(frozen=True)
class SystemState:
    now: datetime
    mode: str                       # SIMULATION | PAPER | LIVE
    bot_running: bool
    manual_stop: bool
    emergency_stop: bool            # drapeau en base
    emergency_file: bool            # fichier témoin (vérifié indépendamment de la base)
    entries_window_open: bool       # planning
    live_armed: bool                # armement LIVE de la session (jamais enregistré)
    open_positions: int
    open_mints: frozenset[str]
    invested_eur: float
    daily_loss_eur: float           # pertes du jour (réalisées + latentes), nombre positif
    fee_reserve_ok: bool | None     # le wallet garde bien la réserve de frais (None = inconnu)


@dataclass
class Decision:
    proposal_key: str
    side: str
    decision: str
    reasons: list[str]
    checks: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    final: bool = True

    @property
    def approved(self) -> bool:
        return self.decision == APPROVED


class Judge:
    name = "JUDGE"

    def __init__(self, db: Any, settings: Any):
        self.db = db
        cap, judge = settings["capital"], settings["judge"]
        self.max_position = float(cap["max_position_eur"])
        self.trading_budget = float(cap["trading_budget_eur"])
        self.max_open = int(cap["max_open_positions"])
        self.max_daily_loss = float(cap["max_daily_loss_eur"])
        self.min_score = float(judge["min_trader_score"])
        self.max_source_age = float(judge["max_source_age_seconds"])
        self.max_fees = float(judge["max_total_fees_pct"])
        self.max_report_age = float(judge["max_report_age_seconds"])
        self.wait_timeout = float(judge["wait_timeout_seconds"])

    # ---------------------------------------------------------------- achats
    def evaluate_buy(self, *, proposal_key: str, reports: dict[str, AgentReport | None],
                     state: SystemState) -> Decision:
        existing = self.db.get_decision(proposal_key)
        if existing and existing["decision"] in (APPROVED, REJECTED):
            return Decision(proposal_key, "BUY", existing["decision"],
                            ["décision déjà rendue (définitive) : " + "; ".join(existing["reasons"][:3])],
                            evidence=existing["evidence"])

        checks: list[dict[str, Any]] = []

        def check(number: int, name: str, passed: bool | None, detail: str) -> None:
            checks.append({"n": number, "name": name, "passed": passed, "detail": detail})

        scout, head = reports.get("SCOUT"), reports.get("HEAD")
        risk, whale, sniper = reports.get("RISK"), reports.get("WHALE"), reports.get("SNIPER")
        event = scout.data if scout else {}
        proposal = sniper.data if sniper else {}
        mint = event.get("token_mint") or proposal.get("mint")

        # 14 — urgence et mode (contrôlés en premier)
        emergency = state.emergency_stop or state.emergency_file
        check(14, "arrêt d'urgence et mode", False if emergency else
              (False if state.mode == "LIVE" and not state.live_armed else True),
              "ARRÊT D'URGENCE actif" if emergency else
              ("mode LIVE non armé dans cette session" if state.mode == "LIVE" and not state.live_armed
               else f"mode {state.mode}"))

        # 13 — rapports complets et récents
        missing = [a for a in REQUIRED_AGENTS if reports.get(a) is None]
        stale = [a for a in ("SCOUT", "RISK", "WHALE", "SNIPER")
                 if reports.get(a) is not None and not reports[a].is_fresh(self.max_report_age, state.now)]
        unavailable = [a for a in REQUIRED_AGENTS if reports.get(a) is not None and reports[a].status == "UNAVAILABLE"]
        if missing or unavailable:
            check(13, "rapports des agents", None,
                  "sans réponse ou sans données : " + ", ".join(missing + unavailable))
        elif stale:
            check(13, "rapports des agents", False, "rapports trop anciens : " + ", ".join(stale))
        else:
            check(13, "rapports des agents", True, "5 rapports complets et récents")

        # 1 — qualité du trader
        if head is None or head.status == "UNAVAILABLE":
            check(1, "qualité du trader", None, "évaluation HEAD indisponible")
        else:
            score = head.data.get("score")
            ok = head.status in (OK, WARN) and score is not None and score >= self.min_score
            check(1, "qualité du trader", ok, f"score {score if score is not None else '?'} "
                                              f"(minimum {self.min_score:.0f})")

        # 2 — validité de la transaction source
        if scout is None or scout.status == "UNAVAILABLE":
            check(2, "transaction source", None, "transaction non confirmée ou introuvable")
        else:
            age = event.get("age_seconds")
            valid = scout.status == OK and event.get("side") == "BUY" and bool(event.get("confirmed"))
            fresh = age is not None and age <= self.max_source_age
            detail = (f"achat confirmé il y a {age:.0f} s (maximum {self.max_source_age:.0f} s)" if age is not None
                      else "âge inconnu")
            check(2, "transaction source", (valid and fresh) if age is not None else None,
                  detail if valid else f"transaction source invalide ({scout.status}, {event.get('side')})")

        # 3, 4, 5 — liquidité, risque, slippage (RISK)
        risk_checks = {c["name"]: c for c in (risk.data.get("checks", []) if risk else [])}
        liq = risk_checks.get("liquidité")
        check(3, "liquidité", liq["passed"] if liq else None, liq["detail"] if liq else "non vérifiée")
        if risk is None or risk.status == "UNAVAILABLE":
            check(4, "risque du token", None, "analyse RISK indisponible")
        else:
            check(4, "risque du token", risk.status == OK,
                  "aucun risque bloquant" if risk.status == OK else "; ".join(risk.reasons[:3]))
        impacts = [risk_checks.get(n) for n in ("impact prix à l'achat", "revente possible",
                                                "impact prix à la revente")]
        if any(c is None for c in impacts):
            check(5, "slippage et revente", None, "devis d'achat ou de revente manquant")
        else:
            check(5, "slippage et revente", all(c["passed"] for c in impacts),
                  "; ".join(c["detail"] for c in impacts))

        # 6, 7 — frais et budget (SNIPER)
        amount = proposal.get("amount_eur")
        fees = proposal.get("estimated_fees_pct")
        if sniper is None or sniper.status != OK or amount is None:
            check(6, "frais", None if sniper is None or sniper.status == "UNAVAILABLE" else False,
                  "pas de proposition chiffrée" if sniper is None else sniper.summary)
            check(7, "budget", None if sniper is None or sniper.status == "UNAVAILABLE" else False,
                  "montant inconnu")
        else:
            check(6, "frais", fees is not None and fees <= self.max_fees,
                  f"{fees:.2f} % (maximum {self.max_fees:.1f} %)" if fees is not None else "inconnus")
            within = amount <= self.max_position and state.invested_eur + amount <= self.trading_budget + 1e-9
            reserve = state.fee_reserve_ok
            check(7, "budget", (within and reserve) if reserve is not None else None,
                  f"{amount:.2f} € ; investi {state.invested_eur:.2f} € / {self.trading_budget:.2f} € ; réserve de "
                  f"frais {'OK' if reserve else 'insuffisante' if reserve is False else 'inconnue'}")

        # 8 — positions
        dup_mint = bool(mint) and mint in state.open_mints
        check(8, "positions ouvertes", state.open_positions < self.max_open and not dup_mint,
              "position déjà ouverte sur ce token" if dup_mint
              else f"{state.open_positions} / {self.max_open}")

        # 9 — pertes du jour
        check(9, "limite de perte du jour", state.daily_loss_eur < self.max_daily_loss,
              f"perte du jour {state.daily_loss_eur:.2f} € (maximum {self.max_daily_loss:.2f} €)")

        # 10 — horaires et état du bot
        if not state.bot_running or state.manual_stop:
            check(10, "horaires et état du bot", False, "bot arrêté (STOP manuel ou non démarré)")
        else:
            check(10, "horaires et état du bot", state.entries_window_open,
                  "fenêtre d'entrée ouverte" if state.entries_window_open else "hors des horaires programmés")

        # 11 — signaux contradictoires
        if whale is None or whale.status == "UNAVAILABLE":
            check(11, "signaux contradictoires", None, "analyse WHALE indisponible")
        else:
            trader_sold = False
            if event.get("wallet") and mint and event.get("block_time"):
                sells = self.db.source_events_for_mint(mint, int(event["block_time"]), side="SELL")
                trader_sold = any(s["wallet"] == event["wallet"] for s in sells)
            contradictions = []
            if whale.status == "BLOCK":
                contradictions.append(whale.summary)
            if trader_sold:
                contradictions.append("le trader a déjà revendu ce token")
            check(11, "signaux contradictoires", not contradictions,
                  "; ".join(contradictions) if contradictions else
                  ("aucun" if whale.status == OK else "points d'attention : " + "; ".join(whale.reasons[:2])))

        # 12 — doublon
        source = event.get("signature") or proposal.get("source_signature")
        duplicate_position = bool(source) and self.db.query_one(
            "SELECT 1 AS x FROM positions WHERE source_signature = ?", (source,)) is not None
        check(12, "transaction déjà traitée", not duplicate_position,
              "déjà copiée" if duplicate_position else "jamais traitée")

        checks.sort(key=lambda c: c["n"])
        failed = [c for c in checks if c["passed"] is False]
        unknown = [c for c in checks if c["passed"] is None]
        first_seen_text = (existing or {}).get("evidence", {}).get("first_seen") if existing else None
        evidence = {"checks": checks, "state": _jsonable(state.__dict__),
                    "first_seen": first_seen_text or state.now.isoformat(),
                    "reports": {name: _jsonable({"status": r.status, "summary": r.summary, "reasons": r.reasons,
                                                 "confidence": r.confidence, "created_at": r.created_at,
                                                 "data": r.data}) if r else None for name, r in reports.items()},
                    "proposal": proposal}

        if failed:
            decision = Decision(proposal_key, "BUY", REJECTED, [f"{c['name']} : {c['detail']}" for c in failed],
                                checks, evidence)
        elif unknown:
            first_seen = datetime.fromisoformat(evidence["first_seen"])
            waited = (state.now - first_seen).total_seconds()
            reasons = [f"{c['name']} : {c['detail']}" for c in unknown]
            if waited >= self.wait_timeout:
                decision = Decision(proposal_key, "BUY", REJECTED,
                                    [f"informations toujours manquantes après {waited:.0f} s"] + reasons,
                                    checks, evidence)
            else:
                decision = Decision(proposal_key, "BUY", WAIT, reasons, checks, evidence, final=False)
        else:
            decision = Decision(proposal_key, "BUY", APPROVED, ["les 14 contrôles sont validés"], checks, evidence)

        self.db.record_decision(proposal_key=proposal_key, side="BUY", token_mint=mint, source_signature=source,
                                decision=decision.decision, reasons=decision.reasons, evidence=evidence,
                                mode=state.mode)
        return decision

    # ----------------------------------------------------------------- ventes
    def evaluate_sell(self, *, exit_report: AgentReport, state: SystemState) -> Decision:
        """Sortie proposée par EXIT. Seul l'ARRÊT D'URGENCE peut la bloquer."""
        position_id = exit_report.data.get("position_id")
        key = f"SELL:{position_id}"
        existing = self.db.get_decision(key)
        if existing and existing["decision"] == APPROVED:
            return Decision(key, "SELL", APPROVED, ["vente déjà approuvée"], evidence=existing["evidence"])
        emergency = state.emergency_stop or state.emergency_file
        if exit_report.data.get("action") != "SELL":
            return Decision(key, "SELL", REJECTED, ["EXIT ne propose pas de vente"], final=False)
        evidence = {"exit": _jsonable({"summary": exit_report.summary, "reasons": exit_report.reasons,
                                       "data": exit_report.data}), "state": _jsonable(state.__dict__)}
        if emergency:
            # Pas d'enregistrement définitif : la vente pourra être approuvée après levée de l'urgence.
            return Decision(key, "SELL", WAIT, ["ARRÊT D'URGENCE actif : vente suspendue"], evidence=evidence,
                            final=False)
        decision = Decision(key, "SELL", APPROVED, exit_report.reasons or ["sortie proposée par EXIT"],
                            evidence=evidence)
        if existing is None:
            self.db.record_decision(proposal_key=key, side="SELL", token_mint=exit_report.data.get("token_mint"),
                                    source_signature=None, decision=APPROVED, reasons=decision.reasons,
                                    evidence=evidence, mode=state.mode)
        return decision


def now_utc() -> datetime:
    return datetime.now(UTC)

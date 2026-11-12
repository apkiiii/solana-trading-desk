"""Pages de l'application."""
from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QScrollArea, QTextBrowser, QVBoxLayout, QWidget)

from app import theme
from app.widgets import Card, SignalWriter, TaskRunner, banner, fill_table, make_table
from config.settings import load_settings
from core import updater
from core.paths import DATA_ROOT, ENV_FILE, UPDATES_DIR
from core.validation import is_valid_address
from core.version import __version__
from data.database import Database, DatabaseError
from security.secrets import SecretsError, load_secrets, mask, save_api_keys

AGENTS = [
    ("SCOUT", "Surveille les wallets suivis et détecte leurs achats et ventes.", "0.5"),
    ("RISK", "Liquidité, slippage, autorités du token, concentration, test de revente.", "0.5"),
    ("WHALE", "Gros mouvements, achats groupés, gros vendeurs pendant que les petits achètent.", "0.5"),
    ("HEAD", "Note les traders : réussite, gains/pertes, drawdown, régularité.", "0.5"),
    ("SNIPER", "Prépare la proposition d'achat chiffrée (montant, frais, slippage).", "0.5"),
    ("EXIT", "Surveille les positions : stop-loss, prises de profit, ventes du trader.", "0.5"),
    ("JUDGE", "Décideur final : APPROVED / REJECTED / WAIT selon des règles fixes.", "0.6"),
]


def _page(title: str, subtitle: str = "") -> tuple[QWidget, QVBoxLayout]:
    page = QWidget()
    outer = QVBoxLayout(page)
    outer.setContentsMargins(0, 0, 0, 0)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    inner = QWidget()
    inner.setObjectName("root")
    layout = QVBoxLayout(inner)
    layout.setContentsMargins(28, 22, 28, 22)
    layout.setSpacing(14)
    heading = QLabel(title)
    heading.setObjectName("pageTitle")
    layout.addWidget(heading)
    if subtitle:
        sub = QLabel(subtitle)
        sub.setObjectName("muted")
        sub.setWordWrap(True)
        layout.addWidget(sub)
    scroll.setWidget(inner)
    outer.addWidget(scroll)
    return page, layout


def _fmt_eur(value: float) -> str:
    return f"{value:,.2f} €".replace(",", " ").replace(".", ",")


# ------------------------------------------------------------------ tableau de bord
class DashboardPage(QWidget):
    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        page, layout = _page("Tableau de bord", "Vue d'ensemble du capital, des positions et du bot.")
        QVBoxLayout(self).addWidget(page)
        self.layout().setContentsMargins(0, 0, 0, 0)
        layout.addWidget(banner(
            f"Version {__version__} — les agents analysent les traders (HEAD) et les tokens (RISK, WHALE). "
            "Le JUDGE et l'exécution arrivent par mises à jour : rien n'est acheté ni vendu pour l'instant."))
        grid = QGridLayout()
        grid.setSpacing(12)
        self.cards = {
            "total": Card("Capital total"), "available": Card("Budget de trading"),
            "invested": Card("Capital investi"), "pnl": Card("Résultat net après frais"),
            "positions": Card("Positions ouvertes"), "executed": Card("Transactions exécutées"),
            "rejected": Card("Transactions refusées"), "state": Card("État du bot"),
            "traders": Card("Traders suivis"), "events": Card("Swaps détectés"),
            "credits": Card("Crédits Helius ce mois"), "mode": Card("Mode"),
        }
        for index, card in enumerate(self.cards.values()):
            grid.addWidget(card, index // 4, index % 4)
        layout.addLayout(grid)
        layout.addStretch(1)
        self.refresh()

    def refresh(self) -> None:
        settings = load_settings(self.db)
        cap = settings["capital"]
        control = self.db.get_control()
        one = self.db.query_one
        open_positions = one("SELECT COUNT(*) n, COALESCE(SUM(cost_eur),0) c FROM positions WHERE status='OPEN'")
        executed = one("SELECT COUNT(*) n FROM executions WHERE status='CONFIRMED'")["n"]
        rejected = one("SELECT COUNT(*) n FROM decisions WHERE decision='REJECTED'")["n"]
        pnl = one("SELECT COALESCE(SUM(realized_pnl_eur),0) p FROM positions")["p"]
        traders = self.db.list_traders(active_only=True)
        events = one("SELECT COUNT(*) n FROM source_events")["n"]
        credits = self.db.month_credits("helius")
        budget = settings["providers"]["helius"]["monthly_credit_budget"]

        self.cards["total"].set(_fmt_eur(cap["total_eur"]), f"Réserve intouchable : {_fmt_eur(cap['reserve_eur'])}")
        self.cards["available"].set(_fmt_eur(cap["trading_budget_eur"] - open_positions["c"]),
                                    f"sur {_fmt_eur(cap['trading_budget_eur'])} — frais : "
                                    f"{_fmt_eur(cap['fee_reserve_eur'])}")
        self.cards["invested"].set(_fmt_eur(open_positions["c"]), f"max {_fmt_eur(cap['max_position_eur'])} / position")
        self.cards["pnl"].set(_fmt_eur(pnl), "réalisé", theme.GREEN if pnl > 0 else theme.RED if pnl < 0 else None)
        self.cards["positions"].set(f"{open_positions['n']} / {cap['max_open_positions']}")
        self.cards["executed"].set(str(executed))
        self.cards["rejected"].set(str(rejected), "par le JUDGE")
        running = control["bot_state"] == "RUNNING"
        state = "URGENCE" if control["emergency_stop"] else ("EN MARCHE" if running else "ARRÊTÉ")
        color = theme.RED if control["emergency_stop"] else theme.GREEN if running else theme.MUTED
        self.cards["state"].set(state, "Moteur de trading : prochaines versions", color)
        self.cards["traders"].set(f"{len(traders)} / {cap['max_traders']}")
        self.cards["events"].set(str(events))
        self.cards["credits"].set(f"{credits:,}".replace(",", " "), f"sur {budget:,} prévus".replace(",", " "))
        mode_color = {"SIMULATION": theme.BLUE, "PAPER": theme.AMBER, "LIVE": theme.RED}[control["mode"]]
        self.cards["mode"].set(control["mode"], "Par défaut : SIMULATION", mode_color)


# ------------------------------------------------------------------------- agents
STATUS_COLORS = {"OK": theme.GREEN, "WARN": theme.AMBER, "BLOCK": theme.RED, "UNAVAILABLE": theme.RED,
                 "IGNORED": theme.MUTED, "DUPLICATE": theme.MUTED}
STATUS_TEXT = {"OK": "OK", "WARN": "Attention", "BLOCK": "Refus", "UNAVAILABLE": "Données manquantes",
               "IGNORED": "Ignoré", "DUPLICATE": "Doublon"}


class AgentsPage(QWidget):
    def __init__(self, db: Database, runner: TaskRunner):
        super().__init__()
        self.db = db
        self.runner = runner
        page, layout = _page("Agents", "Les sept agents spécialisés, leur dernière analyse et leur niveau de "
                                       "confiance. Aucun agent ne peut acheter : seul le JUDGE décidera.")
        QVBoxLayout(self).addWidget(page)
        self.layout().setContentsMargins(0, 0, 0, 0)
        grid = QGridLayout()
        grid.setSpacing(12)
        self.cards: dict[str, Card] = {}
        for index, (name, mission, version) in enumerate(AGENTS):
            card = Card(name, "—", mission)
            if name == "JUDGE":
                card.setStyleSheet(f"QFrame#card {{ border: 1px solid {theme.PURPLE}; }}")
                card.set("En préparation", f"{mission}\nDisponible à partir de la version {version}.", theme.MUTED)
            self.cards[name] = card
            grid.addWidget(card, index // 3, index % 3)
        layout.addLayout(grid)

        title = QLabel("Analyser un token (RISK + WHALE)")
        title.setStyleSheet("font-size: 12pt; font-weight: 700; margin-top: 8px;")
        layout.addWidget(title)
        hint = QLabel("Colle l'adresse d'un token : les agents vérifient s'il serait achetable et revendable pour "
                      "le montant maximal d'une position. Aucun achat n'est effectué.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        row = QHBoxLayout()
        self.mint = QLineEdit()
        self.mint.setPlaceholderText("Adresse du token (mint)")
        self.analyze_button = QPushButton("Analyser")
        self.analyze_button.setObjectName("primary")
        self.analyze_button.clicked.connect(self.analyze)
        row.addWidget(self.mint, 1)
        row.addWidget(self.analyze_button)
        layout.addLayout(row)
        self.result = QTextBrowser()
        self.result.setMinimumHeight(320)
        self.result.setPlaceholderText("Le résultat de l'analyse s'affichera ici.")
        layout.addWidget(self.result)
        self.refresh()

    def refresh(self) -> None:
        for name, card in self.cards.items():
            if name == "JUDGE":
                continue
            rows = self.db.latest_agent_reports(name, 1)
            if not rows:
                card.set("En attente", f"{dict((a, m) for a, m, _ in AGENTS)[name]}\nAucune analyse pour l'instant.",
                         theme.MUTED)
                continue
            r = rows[0]
            confidence = "" if r["confidence"] is None else f" — confiance {r['confidence'] * 100:.0f} %"
            card.set(STATUS_TEXT.get(r["status"], r["status"]), f"{r['summary']}\n{r['created_at']}{confidence}",
                     STATUS_COLORS.get(r["status"]))

    def analyze(self) -> None:
        mint = self.mint.text().strip()
        if not is_valid_address(mint):
            QMessageBox.warning(self, "Adresse invalide", "Cette adresse de token n'est pas valide.")
            return
        self.analyze_button.setEnabled(False)
        self.result.setHtml("<p>Analyse en cours (devis Jupiter, liquidité, RugCheck, flux du pool)…</p>")
        from app import services
        self.runner.start(lambda _s: services.analyze_token(self.db, mint), on_done=self._done, on_error=self._error)

    def _done(self, results: dict) -> None:
        from app.services import format_report_html
        self.analyze_button.setEnabled(True)
        if results.get("eur_per_sol") is None:
            self.result.setHtml(f"<p>Analyse impossible : {results.get('eur_reason')}</p>")
            return
        head = (f"<p>Montant testé : {results['amount_eur']:.2f} € "
                f"(1 SOL = {results['eur_per_sol']:.2f} €)</p>")
        self.result.setHtml(head + "".join(format_report_html(r, STATUS_COLORS) for r in results["reports"]))
        self.refresh()

    def _error(self, message: str) -> None:
        self.analyze_button.setEnabled(True)
        self.result.setHtml(f"<p style='color:{theme.RED}'>Erreur : {message}</p>")


# -------------------------------------------------------------------------- JUDGE
class JudgePage(QWidget):
    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        page, layout = _page("JUDGE", "Chaque décision avec ses raisons et les données utilisées. "
                                      "Un refus est définitif pour la proposition concernée.")
        QVBoxLayout(self).addWidget(page)
        self.layout().setContentsMargins(0, 0, 0, 0)
        self.table = make_table(["Date", "Décision", "Sens", "Token", "Raisons"])
        self.table.setMinimumHeight(380)
        layout.addWidget(self.table)
        self.refresh()

    def refresh(self) -> None:
        rows = []
        for r in self.db.query_all("SELECT * FROM decisions ORDER BY id DESC LIMIT 200"):
            reasons = "; ".join(json.loads(r["reasons_json"] or "[]"))
            rows.append([r["created_at"], r["decision"], r["side"], r["token_mint"], reasons])
        fill_table(self.table, rows, "Aucune décision pour l'instant (le JUDGE arrive en version 0.6).")


# ------------------------------------------------------------- positions + historique
class PositionsPage(QWidget):
    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        page, layout = _page("Positions et historique")
        QVBoxLayout(self).addWidget(page)
        self.layout().setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("Positions ouvertes"))
        self.positions = make_table(["Token", "Ouverte le", "Montant investi", "Frais", "Mode", "État"])
        self.positions.setMinimumHeight(180)
        layout.addWidget(self.positions)
        layout.addWidget(QLabel("Swaps détectés chez les traders suivis"))
        self.events = make_table(["Détecté le", "Wallet source", "Type", "Token", "Montant", "Statut"])
        self.events.setMinimumHeight(260)
        layout.addWidget(self.events)
        self.refresh()

    def refresh(self) -> None:
        pos = [[p["token_mint"], p["opened_at"], _fmt_eur(p["cost_eur"] or 0), _fmt_eur(p["fees_eur"] or 0),
                p["mode"], p["status"]]
               for p in self.db.query_all("SELECT * FROM positions WHERE status='OPEN' ORDER BY id DESC")]
        fill_table(self.positions, pos, "Aucune position ouverte.")
        rows = []
        for e in self.db.recent_source_events(200):
            amount = ""
            if e["quote_amount_raw"] and e["quote_decimals"] is not None:
                unit = "SOL" if e["quote_decimals"] == 9 else "USDC"
                amount = f"{int(e['quote_amount_raw']) / 10 ** e['quote_decimals']:.4f} {unit}"
            rows.append([e["detected_at"], e["wallet"][:6] + "…" + e["wallet"][-4:], e["side"], e["token_mint"],
                         amount, e["status"]])
        fill_table(self.events, rows, "Aucun swap détecté pour l'instant.")


# ------------------------------------------------------------------------ traders
class TradersPage(QWidget):
    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        page, layout = _page("Traders suivis", "Adresses publiques uniquement. Le bot notera chaque trader "
                                               "(agent HEAD) et refusera de copier ceux qui ont un mauvais score.")
        QVBoxLayout(self).addWidget(page)
        self.layout().setContentsMargins(0, 0, 0, 0)
        form = QHBoxLayout()
        self.address = QLineEdit()
        self.address.setPlaceholderText("Adresse publique Solana du trader")
        self.label = QLineEdit()
        self.label.setPlaceholderText("Nom (facultatif)")
        self.label.setMaximumWidth(220)
        add = QPushButton("Ajouter")
        add.setObjectName("primary")
        add.clicked.connect(self.add)
        form.addWidget(self.address, 3)
        form.addWidget(self.label, 1)
        form.addWidget(add)
        layout.addLayout(form)
        self.table = make_table(["Adresse", "Nom", "Statut", "Score", "Réussite", "Drawdown", "Ajouté le"])
        self.table.setMinimumHeight(260)
        layout.addWidget(self.table)
        buttons = QHBoxLayout()
        self.evaluate_button = QPushButton("Évaluer le trader sélectionné (HEAD)")
        self.evaluate_button.setObjectName("primary")
        self.evaluate_button.clicked.connect(self.evaluate)
        toggle = QPushButton("Activer / désactiver")
        toggle.clicked.connect(self.toggle)
        buttons.addWidget(self.evaluate_button)
        buttons.addWidget(toggle)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        note = QLabel("L'évaluation lit jusqu'à 300 transactions du wallet (1 à 3 minutes) et calcule : taux de "
                      "réussite, gains et pertes réalisés, drawdown, régularité, comportements suspects.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.details = QTextBrowser()
        self.details.setMinimumHeight(220)
        self.details.setPlaceholderText("Sélectionne un trader pour voir le détail de son évaluation.")
        layout.addWidget(self.details)
        self.table.itemSelectionChanged.connect(self.show_details)
        self.refresh()

    def refresh(self) -> None:
        self._rows = self.db.list_traders()
        def pct(value):
            return "—" if value is None else f"{value:.0f} %"

        fill_table(self.table, [[t["address"], t["label"] or "", "actif" if t["active"] else "désactivé",
                                 "—" if t["score"] is None else f"{t['score']:.0f}/100",
                                 pct((t["stats"].get("win_rate") or 0) * 100 if t["stats"].get("win_rate") is not None
                                     else None),
                                 pct(t["stats"].get("max_drawdown_pct")), t["added_at"]]
                                for t in self._rows], "Aucun trader : ajoute une adresse ci-dessus.")

    def add(self) -> None:
        address = self.address.text().strip()
        if not is_valid_address(address):
            QMessageBox.warning(self, "Adresse invalide", "Cette adresse Solana n'est pas valide.")
            return
        try:
            max_traders = int(load_settings(self.db)["capital"]["max_traders"])
            self.db.upsert_trader(address, self.label.text().strip() or None, max_traders=max_traders)
        except DatabaseError as exc:
            QMessageBox.warning(self, "Impossible d'ajouter", str(exc))
            return
        self.address.clear()
        self.label.clear()
        self.refresh()

    def _selected(self):
        row = self.table.currentRow()
        rows = getattr(self, "_rows", [])
        return rows[row] if 0 <= row < len(rows) else None

    def show_details(self) -> None:
        trader = self._selected()
        if trader is None:
            return
        s = trader["stats"]
        if not s:
            self.details.setHtml("<p>Pas encore évalué : clique sur « Évaluer le trader sélectionné ».</p>")
            return
        notes = "".join(f"<li>{n}</li>" for n in s.get("notes", []))
        pf = s.get("profit_factor")
        self.details.setHtml(
            f"<h3>Score {trader['score']:.0f}/100</h3>"
            f"<p>Trades clôturés : <b>{s.get('closed_trades', 0)}</b> ({s.get('wins', 0)} gagnants, "
            f"{s.get('losses', 0)} perdants) — réussite <b>{(s.get('win_rate') or 0) * 100:.0f} %</b><br>"
            f"Gain réalisé : <b>{s.get('realized_pnl_sol', 0):+.3f} SOL</b> — facteur de profit "
            f"{'—' if pf is None else f'{pf:.2f}'}<br>"
            f"Gain moyen d'un trade gagnant : {s.get('avg_win_pct') or 0:.1f} % — perte moyenne : "
            f"{s.get('avg_loss_pct') or 0:.1f} %<br>"
            f"Drawdown max : {s.get('max_drawdown_sol', 0):.3f} SOL ({s.get('max_drawdown_pct') or 0:.0f} %)<br>"
            f"Historique : {s.get('history_days', 0):.0f} jours, {s.get('active_days', 0)} jours actifs, "
            f"semaines positives {(s.get('positive_weeks_ratio') or 0) * 100:.0f} %<br>"
            f"Évalué le : {s.get('evaluated_at', '—')}</p>" + (f"<p>Points d'attention :</p><ul>{notes}</ul>"
                                                               if notes else ""))

    def evaluate(self) -> None:
        trader = self._selected()
        if trader is None:
            QMessageBox.information(self, "Évaluer", "Sélectionne d'abord un trader dans le tableau.")
            return
        self.evaluate_button.setEnabled(False)
        self.evaluate_button.setText("Évaluation en cours…")
        from app import services
        address = trader["address"]
        self.runner.start(lambda _s: services.evaluate_trader(self.db, address), on_done=self._evaluated,
                          on_error=self._evaluate_failed)

    def _evaluated(self, report) -> None:
        self.evaluate_button.setEnabled(True)
        self.evaluate_button.setText("Évaluer le trader sélectionné (HEAD)")
        self.refresh()
        if report.status == "UNAVAILABLE":
            QMessageBox.warning(self, "Évaluation impossible", report.summary)
        else:
            QMessageBox.information(self, "Évaluation terminée", report.summary)

    def _evaluate_failed(self, message: str) -> None:
        self.evaluate_button.setEnabled(True)
        self.evaluate_button.setText("Évaluer le trader sélectionné (HEAD)")
        QMessageBox.warning(self, "Évaluation impossible", message)

    def toggle(self) -> None:
        row = self.table.currentRow()
        if row < 0 or row >= len(getattr(self, "_rows", [])):
            return
        trader = self._rows[row]
        if not trader["active"]:
            active = sum(1 for t in self._rows if t["active"])
            if active >= int(load_settings(self.db)["capital"]["max_traders"]):
                QMessageBox.warning(self, "Limite atteinte", "Nombre maximal de traders actifs atteint.")
                return
        self.db.set_trader_active(trader["address"], not trader["active"])
        self.refresh()


# --------------------------------------------------------------------- diagnostic
class DiagnosticPage(QWidget):
    def __init__(self, runner: TaskRunner):
        super().__init__()
        self.runner = runner
        page, layout = _page("Diagnostic", "Teste les connexions (RPC, temps réel, prix, devis, liquidité, risque). "
                                           "Aucune transaction n'est envoyée et aucune clé n'est affichée.")
        QVBoxLayout(self).addWidget(page)
        self.layout().setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.wallet = QLineEdit()
        self.wallet.setPlaceholderText("Optionnel : adresse publique d'un trader à tester")
        self.button = QPushButton("Lancer le diagnostic")
        self.button.setObjectName("primary")
        self.button.clicked.connect(self.start)
        row.addWidget(self.wallet, 1)
        row.addWidget(self.button)
        layout.addLayout(row)
        self.output = QPlainTextEdit()
        self.output.setObjectName("console")
        self.output.setReadOnly(True)
        self.output.setMinimumHeight(420)
        layout.addWidget(self.output)

    def start(self) -> None:
        wallet = self.wallet.text().strip() or None
        if wallet and not is_valid_address(wallet):
            QMessageBox.warning(self, "Adresse invalide", "Cette adresse Solana n'est pas valide.")
            return
        self.output.clear()
        self.button.setEnabled(False)

        def job(signals: Any) -> int:
            from tools import diagnose
            with contextlib.redirect_stdout(SignalWriter(signals.text)):
                return asyncio.run(diagnose.run(wallet))

        self.runner.start(job, on_text=self._append, on_done=lambda _: self.button.setEnabled(True),
                          on_error=self._error)

    def _append(self, text: str) -> None:
        self.output.moveCursor(self.output.textCursor().MoveOperation.End)
        self.output.insertPlainText(text)
        self.output.ensureCursorVisible()

    def _error(self, message: str) -> None:
        self._append(f"\n[ECHEC] {message}\n")
        self.button.setEnabled(True)


# ----------------------------------------------------------------------- clés API
class KeysPage(QWidget):
    FIELDS = [("HELIUS_API_KEY", "Clé Helius (obligatoire)", "https://dashboard.helius.dev"),
              ("JUPITER_API_KEY", "Clé Jupiter (recommandée)", "https://developers.jup.ag"),
              ("RUGCHECK_API_KEY", "Clé RugCheck (optionnelle)", "https://rugcheck.xyz")]

    def __init__(self):
        super().__init__()
        page, layout = _page("Clés API", "Ces clés servent uniquement à lire des données et à obtenir des devis. "
                                         "Ne colle JAMAIS ici une clé privée ou une phrase de récupération.")
        QVBoxLayout(self).addWidget(page)
        self.layout().setContentsMargins(0, 0, 0, 0)
        self.inputs: dict[str, QLineEdit] = {}
        self.current: dict[str, QLabel] = {}
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        for row, (name, label, url) in enumerate(self.FIELDS):
            title = QLabel(f'{label} — <a style="color:{theme.BLUE}" href="{url}">obtenir une clé</a>')
            title.setOpenExternalLinks(True)
            field = QLineEdit()
            field.setEchoMode(QLineEdit.EchoMode.Password)
            field.setPlaceholderText("Coller la nouvelle clé (laisser vide pour ne pas changer)")
            current = QLabel()
            current.setObjectName("muted")
            grid.addWidget(title, row * 2, 0, 1, 2)
            grid.addWidget(field, row * 2 + 1, 0)
            grid.addWidget(current, row * 2 + 1, 1)
            self.inputs[name] = field
            self.current[name] = current
        layout.addLayout(grid)
        save = QPushButton("Enregistrer les clés")
        save.setObjectName("primary")
        save.clicked.connect(self.save)
        layout.addWidget(save, alignment=Qt.AlignmentFlag.AlignLeft)
        where = QLabel(f"Fichier : {ENV_FILE}")
        where.setObjectName("muted")
        where.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(where)
        layout.addStretch(1)
        self.refresh()

    def refresh(self) -> None:
        try:
            secrets = load_secrets()
        except SecretsError as exc:
            for label in self.current.values():
                label.setText("erreur")
            QMessageBox.warning(self, "Fichier de clés invalide", str(exc))
            return
        values = {"HELIUS_API_KEY": secrets.helius_api_key, "JUPITER_API_KEY": secrets.jupiter_api_key,
                  "RUGCHECK_API_KEY": secrets.rugcheck_api_key}
        for name, label in self.current.items():
            label.setText("Actuelle : " + mask(values[name]))

    def save(self) -> None:
        values = {name: field.text().strip() for name, field in self.inputs.items() if field.text().strip()}
        if not values:
            return
        try:
            save_api_keys(values)
        except SecretsError as exc:
            QMessageBox.warning(self, "Clé refusée", str(exc))
            return
        for field in self.inputs.values():
            field.clear()
        self.refresh()
        QMessageBox.information(self, "Enregistré", "Clés enregistrées. Lance le diagnostic pour les tester.")


# ------------------------------------------------------------------ mises à jour
class UpdatesPage(QWidget):
    def __init__(self, db: Database, runner: TaskRunner, on_update_found):
        super().__init__()
        self.db = db
        self.runner = runner
        self.on_update_found = on_update_found
        self.info: updater.UpdateInfo | None = None
        cfg = load_settings(db)["updates"]
        self.repository = cfg["repository"]
        self.prefix = cfg["installer_prefix"]
        page, layout = _page("Mises à jour", "Les nouvelles versions sont proposées ici automatiquement. "
                                             "Rien ne s'installe sans ton accord ; tes données sont conservées.")
        QVBoxLayout(self).addWidget(page)
        self.layout().setContentsMargins(0, 0, 0, 0)
        self.status = QLabel(f"Version installée : {__version__}")
        self.status.setStyleSheet("font-size: 11pt;")
        layout.addWidget(self.status)
        row = QHBoxLayout()
        self.check_button = QPushButton("Vérifier maintenant")
        self.check_button.clicked.connect(self.check)
        self.install_button = QPushButton("Télécharger et installer")
        self.install_button.setObjectName("primary")
        self.install_button.setEnabled(False)
        self.install_button.clicked.connect(self.install)
        row.addWidget(self.check_button)
        row.addWidget(self.install_button)
        row.addStretch(1)
        layout.addLayout(row)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        self.notes = QTextBrowser()
        self.notes.setOpenExternalLinks(True)
        self.notes.setMinimumHeight(300)
        self.notes.setPlaceholderText("Les nouveautés de la prochaine version s'afficheront ici.")
        layout.addWidget(self.notes)
        self.last_check = QLabel("")
        self.last_check.setObjectName("muted")
        layout.addWidget(self.last_check)

    def check(self, silent: bool = False) -> None:
        self.check_button.setEnabled(False)
        repo, prefix = self.repository, self.prefix
        self.runner.start(lambda _s: updater.check_for_update(repo, prefix),
                          on_done=lambda info: self._checked(info, silent),
                          on_error=lambda msg: self._check_failed(msg, silent))

    def _checked(self, info: updater.UpdateInfo | None, silent: bool) -> None:
        self.check_button.setEnabled(True)
        self.last_check.setText("Dernière vérification : " + datetime.now().strftime("%d/%m/%Y %H:%M"))
        self.info = info
        if info is None:
            self.status.setText(f"Version installée : {__version__} — le logiciel est à jour.")
            self.install_button.setEnabled(False)
            return
        self.status.setText(f"Version installée : {__version__} — nouvelle version disponible : "
                            f"<b style='color:{theme.GREEN}'>{info.version}</b>")
        self.notes.setMarkdown(f"## {info.title}\n\n{info.notes}")
        self.install_button.setEnabled(True)
        self.on_update_found(info)

    def _check_failed(self, message: str, silent: bool) -> None:
        self.check_button.setEnabled(True)
        self.last_check.setText(f"Vérification impossible : {message}")
        if not silent:
            QMessageBox.warning(self, "Mises à jour", message)

    def install(self) -> None:
        if self.info is None:
            return
        answer = QMessageBox.question(
            self, "Installer la mise à jour",
            f"Installer la version {self.info.version} ?\n\nL'application va se fermer quelques secondes "
            "puis redémarrer automatiquement. Tes données et réglages sont conservés.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.install_button.setEnabled(False)
        self.check_button.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setValue(0)
        info = self.info

        def job(signals: Any):
            return updater.download_update(info, UPDATES_DIR,
                                           progress=lambda done, total: signals.progress.emit(done, total or 0))

        self.runner.start(job, on_progress=self._progress, on_done=self._downloaded, on_error=self._install_failed)

    def _progress(self, done: int, total: int) -> None:
        if total:
            self.progress.setMaximum(total)
            self.progress.setValue(done)
        else:
            self.progress.setMaximum(0)

    def _downloaded(self, path) -> None:
        try:
            updater.launch_installer(path)
        except updater.UpdateError as exc:
            self._install_failed(str(exc))
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))
            return
        from PySide6.QtWidgets import QApplication
        QApplication.quit()

    def _install_failed(self, message: str) -> None:
        self.progress.setVisible(False)
        self.check_button.setEnabled(True)
        self.install_button.setEnabled(self.info is not None)
        QMessageBox.warning(self, "Mise à jour impossible", message)


DATA_FOLDER_TEXT = str(DATA_ROOT)

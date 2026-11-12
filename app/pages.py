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
            f"Version {__version__} — moteur de données installé. Le trading automatique (agents, JUDGE, "
            "exécution) arrivera par mises à jour successives : rien n'est acheté ni vendu pour l'instant."))
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
class AgentsPage(QWidget):
    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        page, layout = _page("Agents", "Les sept agents spécialisés et leur dernière analyse.")
        QVBoxLayout(self).addWidget(page)
        self.layout().setContentsMargins(0, 0, 0, 0)
        grid = QGridLayout()
        grid.setSpacing(12)
        for index, (name, mission, version) in enumerate(AGENTS):
            card = Card(name, "En préparation", f"{mission}\nDisponible à partir de la version {version}.")
            card.value.setStyleSheet(f"color: {theme.MUTED}; font-size: 12pt;")
            if name == "JUDGE":
                card.setStyleSheet(f"QFrame#card {{ border: 1px solid {theme.PURPLE}; }}")
            grid.addWidget(card, index // 3, index % 3)
        layout.addLayout(grid)
        layout.addStretch(1)


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
        self.table = make_table(["Adresse", "Nom", "Statut", "Score", "Ajouté le"])
        self.table.setMinimumHeight(260)
        layout.addWidget(self.table)
        toggle = QPushButton("Activer / désactiver le trader sélectionné")
        toggle.clicked.connect(self.toggle)
        layout.addWidget(toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        self.refresh()

    def refresh(self) -> None:
        self._rows = self.db.list_traders()
        fill_table(self.table, [[t["address"], t["label"] or "", "actif" if t["active"] else "désactivé",
                                 "—" if t["score"] is None else f"{t['score']:.0f}/100", t["added_at"]]
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

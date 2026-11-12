"""Fenêtre principale : barre latérale, barre de contrôle, pages."""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow, QPushButton,
                               QStackedWidget, QStatusBar, QVBoxLayout, QWidget)

from app import theme
from app.pages import (AgentsPage, DashboardPage, DiagnosticPage, JudgePage, KeysPage, PositionsPage, TradersPage,
                       UpdatesPage)
from app.widgets import TaskRunner, badge, set_badge
from config.settings import load_settings
from core.paths import ASSETS_DIR, DATA_ROOT
from core.version import __version__
from data.database import Database

NAV = ["Tableau de bord", "Agents", "JUDGE", "Positions et historique", "Traders suivis", "Diagnostic",
       "Clés API", "Mises à jour"]
SOON = "Disponible avec le moteur de trading (prochaines mises à jour)."


class MainWindow(QMainWindow):
    def __init__(self, db: Database, *, auto_check_updates: bool = True):
        super().__init__()
        self.db = db
        self.runner = TaskRunner()
        self.setWindowTitle(f"MY SOLANA TRADING DESK — v{__version__}")
        icon = ASSETS_DIR / "icon.ico"
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))
        self.resize(1320, 840)
        self.setMinimumSize(QSize(1080, 700))

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ---- barre latérale
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(240)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(0, 0, 0, 12)
        brand = QLabel(f'<span style="color:{theme.GREEN}">◆</span> MY SOLANA<br>TRADING DESK')
        brand.setObjectName("brand")
        sub = QLabel(f"Version {__version__}")
        sub.setObjectName("brandSub")
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        for name in NAV:
            self.nav.addItem(QListWidgetItem(name))
        side.addWidget(brand)
        side.addWidget(sub)
        side.addWidget(self.nav, 1)
        data = QLabel(f"Données : {DATA_ROOT}")
        data.setObjectName("muted")
        data.setWordWrap(True)
        data.setStyleSheet("padding: 0 16px; font-size: 8pt;")
        side.addWidget(data)
        layout.addWidget(sidebar)

        # ---- zone principale
        main = QWidget()
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        main_layout.addWidget(self._topbar())
        self.stack = QStackedWidget()
        self.dashboard = DashboardPage(db)
        self.judge = JudgePage(db)
        self.positions = PositionsPage(db)
        self.traders = TradersPage(db)
        self.updates = UpdatesPage(db, self.runner, self._update_found)
        for page in (self.dashboard, AgentsPage(db), self.judge, self.positions, self.traders,
                     DiagnosticPage(self.runner), KeysPage(), self.updates):
            self.stack.addWidget(page)
        main_layout.addWidget(self.stack, 1)
        layout.addWidget(main, 1)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)

        status = QStatusBar()
        self.setStatusBar(status)
        status.showMessage("Prêt. Mode SIMULATION par défaut — aucune transaction réelle.")

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(5000)
        self.refresh()

        if auto_check_updates:
            hours = float(load_settings(db)["updates"]["check_interval_hours"])
            QTimer.singleShot(3000, lambda: self.updates.check(silent=True))
            self.update_timer = QTimer(self)
            self.update_timer.timeout.connect(lambda: self.updates.check(silent=True))
            self.update_timer.start(int(hours * 3600 * 1000))

    def _topbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("topbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(24, 12, 24, 12)
        row.setSpacing(10)
        self.mode_badge = badge("SIMULATION", theme.BLUE)
        self.state_badge = badge("ARRÊTÉ", theme.MUTED)
        row.addWidget(QLabel("Mode"))
        row.addWidget(self.mode_badge)
        row.addSpacing(12)
        row.addWidget(QLabel("Bot"))
        row.addWidget(self.state_badge)
        row.addStretch(1)
        for text, name in (("▶  DÉMARRER", "start"), ("■  STOPPER", "stop"), ("⚠  ARRÊT D'URGENCE", "emergency")):
            button = QPushButton(text)
            button.setObjectName(name)
            button.setEnabled(False)
            button.setToolTip(SOON)
            button.setCursor(Qt.CursorShape.ForbiddenCursor)
            row.addWidget(button)
        return bar

    def refresh(self) -> None:
        control = self.db.get_control()
        colors = {"SIMULATION": theme.BLUE, "PAPER": theme.AMBER, "LIVE": theme.RED}
        set_badge(self.mode_badge, control["mode"], colors[control["mode"]])
        if control["emergency_stop"]:
            set_badge(self.state_badge, "URGENCE", theme.RED)
        elif control["bot_state"] == "RUNNING":
            set_badge(self.state_badge, "EN MARCHE", theme.GREEN)
        else:
            set_badge(self.state_badge, "ARRÊTÉ", theme.MUTED)
        current = self.stack.currentWidget()
        if hasattr(current, "refresh") and current is not self.updates:
            try:
                current.refresh()
            except Exception:  # noqa: BLE001 - un rafraîchissement raté ne doit pas fermer l'app
                pass

    def _update_found(self, info) -> None:
        item = self.nav.item(NAV.index("Mises à jour"))
        item.setText(f"Mises à jour  ●  {info.version}")
        item.setForeground(Qt.GlobalColor.green)
        self.statusBar().showMessage(f"Nouvelle version {info.version} disponible — menu « Mises à jour ».")

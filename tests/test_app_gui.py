"""Test de démarrage de l'application (affichage hors écran, sans réseau)."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from app.theme import STYLESHEET  # noqa: E402
from app.window import NAV, MainWindow  # noqa: E402
from tests.conftest import WALLET  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setStyleSheet(STYLESHEET)
    yield app


def test_window_opens_every_page_and_buttons_are_honest(qapp, db, tmp_path):
    db.upsert_trader(WALLET, "Trader test")
    window = MainWindow(db, auto_check_updates=False)
    window.show()
    for index in range(len(NAV)):
        window.nav.setCurrentRow(index)
        qapp.processEvents()
        window.refresh()
    # Les boutons de trading restent désactivés tant que le moteur n'existe pas.
    buttons = [b for b in window.findChildren(QtWidgets.QPushButton) if b.objectName() in {"start", "stop", "emergency"}]
    assert len(buttons) == 3 and not any(b.isEnabled() for b in buttons)
    assert window.dashboard.cards["mode"].value.text() == "SIMULATION"
    assert window.traders.table.item(0, 0).text() == WALLET
    shot = os.environ.get("DESK_SCREENSHOT")
    if shot:
        window.nav.setCurrentRow(0)
        qapp.processEvents()
        window.grab().save(shot)
    window.close()

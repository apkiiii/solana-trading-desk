"""Briques d'interface réutilisables + exécution de tâches en arrière-plan."""
from __future__ import annotations

import io
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QHeaderView, QLabel, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from app import theme


class Card(QFrame):
    """Carte d'indicateur : titre, valeur, sous-titre."""

    def __init__(self, label: str, value: str = "—", sub: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(4)
        self.label = QLabel(label)
        self.label.setObjectName("cardLabel")
        self.value = QLabel(value)
        self.value.setObjectName("cardValue")
        self.sub = QLabel(sub)
        self.sub.setObjectName("cardSub")
        self.sub.setWordWrap(True)
        layout.addWidget(self.label)
        layout.addWidget(self.value)
        layout.addWidget(self.sub)

    def set(self, value: str, sub: str | None = None, color: str | None = None) -> None:
        self.value.setText(value)
        self.value.setStyleSheet(f"color: {color};" if color else "")
        if sub is not None:
            self.sub.setText(sub)


def badge(text: str, color: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("badge")
    set_badge(label, text, color)
    return label


def _rgba(hex_color: str, alpha: float) -> str:
    value = hex_color.lstrip("#")
    r, g, b = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r}, {g}, {b}, {alpha})"


def set_badge(label: QLabel, text: str, color: str) -> None:
    label.setText(text)
    label.setStyleSheet(f"background: {_rgba(color, 0.15)}; color: {color}; border: 1px solid {color};")


def banner(text: str) -> QFrame:
    frame = QFrame()
    frame.setObjectName("banner")
    layout = QHBoxLayout(frame)
    layout.setContentsMargins(14, 10, 14, 10)
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(f"color: {theme.TEXT};")
    layout.addWidget(label)
    return frame


def make_table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setAlternatingRowColors(True)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    return table


def fill_table(table: QTableWidget, rows: list[list[Any]], empty_text: str = "Aucune donnée") -> None:
    table.setRowCount(0)
    if not rows:
        table.setRowCount(1)
        item = QTableWidgetItem(empty_text)
        item.setForeground(QBrush(QColor(theme.MUTED)))
        table.setItem(0, 0, item)
        table.setSpan(0, 0, 1, table.columnCount())
        return
    table.clearSpans()
    table.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            table.setItem(r, c, QTableWidgetItem("" if value is None else str(value)))


# ---------------------------------------------------------------- tâches de fond
class TaskSignals(QObject):
    done = Signal(object)
    error = Signal(str)
    progress = Signal(int, int)
    text = Signal(str)


class _Task(QRunnable):
    def __init__(self, fn: Callable[..., Any], signals: TaskSignals):
        super().__init__()
        self.setAutoDelete(False)
        self.fn = fn
        self.signals = signals

    def run(self) -> None:
        try:
            result = self.fn(self.signals)
        except Exception as exc:  # noqa: BLE001
            self.signals.error.emit(str(exc) or type(exc).__name__)
        else:
            self.signals.done.emit(result)


class TaskRunner:
    """Lance ``fn(signals)`` dans un fil séparé pour ne jamais bloquer la fenêtre."""

    def __init__(self) -> None:
        self._pool = QThreadPool.globalInstance()
        self._running: set[_Task] = set()

    def start(self, fn: Callable[[TaskSignals], Any], *, on_done: Callable[[Any], None] | None = None,
              on_error: Callable[[str], None] | None = None,
              on_progress: Callable[[int, int], None] | None = None,
              on_text: Callable[[str], None] | None = None) -> None:
        signals = TaskSignals()
        task = _Task(fn, signals)
        if on_done:
            signals.done.connect(on_done)
        if on_error:
            signals.error.connect(on_error)
        if on_progress:
            signals.progress.connect(on_progress)
        if on_text:
            signals.text.connect(on_text)
        signals.done.connect(lambda _=None: self._running.discard(task))
        signals.error.connect(lambda _=None: self._running.discard(task))
        self._running.add(task)
        self._pool.start(task)


class SignalWriter(io.TextIOBase):
    """Flux texte qui renvoie chaque écriture vers un signal Qt (pour afficher le diagnostic)."""

    def __init__(self, signal: Signal):
        super().__init__()
        self._signal = signal

    def write(self, text: str) -> int:
        if text:
            self._signal.emit(text)
        return len(text)

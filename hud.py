"""
hud.py — PrometheusHUD: always-on-top overlay showing live system state.

Compact dark panel (320px wide, top-right corner) with 5 rows of live data.
Starts in a daemon thread — does NOT block the main process.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from working_memory import WorkingMemory
from git_safety import GitSafety
from log_viewer import LogViewer
from utils import log_event


class PrometheusHUD:
    """
    Always-on-top PyQt6 overlay showing live Prometheus system state.

    Rows:
      1. PROMETHEUS label | status dot (green=ok, yellow=busy, red=error)
      2. Active task
      3. Session cost
      4. Last log event
      5. Git checkpoint SHA

    Click anywhere: toggle expand/collapse (collapsed = row 1 only).
    Right-click: context menu (Rollback, View full log).
    """

    def __init__(
        self,
        working_memory: WorkingMemory | None = None,
        git_safety: GitSafety | None = None,
        log_path: str | None = None,
    ) -> None:
        self._wm = working_memory or WorkingMemory()
        self._git = git_safety or GitSafety()
        self._log_viewer = LogViewer(log_path=log_path)
        self._app = None
        self._window = None
        self._thread: threading.Thread | None = None
        self._expanded = True

    def start(self) -> None:
        """
        Launch the Qt event loop in a daemon thread. Non-blocking.
        Silently skips if no display is available.
        """
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._qt_main,
            daemon=True,
            name="prometheus-hud",
        )
        self._thread.start()

    def _qt_main(self) -> None:
        """Qt event loop entry point (runs in daemon thread)."""
        try:
            import os
            if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
                log_event("hud_no_display", {"note": "skipping HUD — no display detected"})
                return

            from PyQt6.QtWidgets import (
                QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                QLabel, QMenu, QDialog, QDialogButtonBox,
                QPlainTextEdit, QSizePolicy,
            )
            from PyQt6.QtCore import Qt, QTimer
            from PyQt6.QtGui import QColor, QPalette, QFont, QCursor

            self._app = QApplication.instance() or QApplication([])

            # ── Window setup ────────────────────────────────────────────
            win = QWidget()
            win.setWindowTitle("Prometheus HUD")
            win.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.Tool
            )
            win.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
            win.setFixedWidth(320)
            win.setStyleSheet(
                "QWidget { background-color: #1a1a2e; color: #e0e0e0; }"
                "QLabel { font-family: monospace; font-size: 11px; padding: 2px 6px; }"
            )

            # ── Layout ──────────────────────────────────────────────────
            layout = QVBoxLayout(win)
            layout.setContentsMargins(4, 4, 4, 4)
            layout.setSpacing(2)

            # Row 1: title + status dot
            row1 = QHBoxLayout()
            lbl_title = QLabel("PROMETHEUS")
            lbl_title.setStyleSheet("font-weight: bold; color: #7eb8f7; font-size: 12px;")
            lbl_dot = QLabel("●")
            lbl_dot.setStyleSheet("color: #44ff44; font-size: 14px;")
            row1.addWidget(lbl_title)
            row1.addStretch()
            row1.addWidget(lbl_dot)
            layout.addLayout(row1)

            # Rows 2–5 (hidden in collapsed mode)
            lbl_task = QLabel("Idle")
            lbl_cost = QLabel("$0.00 session / $0.00 today")
            lbl_event = QLabel("—")
            lbl_checkpoint = QLabel("No checkpoint")
            for lbl in (lbl_task, lbl_cost, lbl_event, lbl_checkpoint):
                lbl.setWordWrap(True)
                layout.addWidget(lbl)

            self._window = win
            self._lbl_dot = lbl_dot
            self._lbl_task = lbl_task
            self._lbl_cost = lbl_cost
            self._lbl_event = lbl_event
            self._lbl_checkpoint = lbl_checkpoint
            self._detail_labels = [lbl_task, lbl_cost, lbl_event, lbl_checkpoint]

            # ── Position: top-right ──────────────────────────────────────
            screen = self._app.primaryScreen()
            if screen:
                geom = screen.availableGeometry()
                win.adjustSize()
                win.move(geom.right() - win.width() - 8, geom.top() + 8)

            # ── Refresh timer ────────────────────────────────────────────
            timer = QTimer()
            timer.timeout.connect(self._refresh)
            timer.start(2000)

            # ── Click handler ────────────────────────────────────────────
            win.mousePressEvent = self._on_mouse_press

            win.show()
            self._app.exec()

        except Exception as exc:
            log_event("hud_error", {"error": str(exc)[:200]})

    def _refresh(self) -> None:
        """Called every 2 seconds by QTimer. Reads WorkingMemory and updates labels."""
        try:
            wm = self._wm.read()
            dot_color, task_text = self._resolve_state(wm)
            self._lbl_dot.setStyleSheet(f"color: {dot_color}; font-size: 14px;")
            self._lbl_task.setText(task_text)

            # Cost
            cost_data = wm.get("session_cost") or {}
            s_cost = float(cost_data.get("session_total", 0.0))
            d_cost = float(cost_data.get("daily_total", 0.0))
            self._lbl_cost.setText(f"${s_cost:.2f} session / ${d_cost:.2f} today")

            # Last event
            last_entries = self._log_viewer.tail(1)
            if last_entries:
                e = last_entries[-1]
                kind = str(e.get("kind", ""))[:40]
                ts = str(e.get("ts", ""))[-8:]  # HH:MM:SS
                self._lbl_event.setText(f"{kind} {ts}")
            else:
                self._lbl_event.setText("—")

            # Git checkpoint
            sha = self._git.last_checkpoint_sha()
            self._lbl_checkpoint.setText(f"Checkpoint: {sha}" if sha else "No checkpoint")

        except Exception as exc:
            log_event("hud_refresh_error", {"error": str(exc)[:200]})

    def _resolve_state(self, wm: dict) -> tuple[str, str]:
        """Return (dot_color, task_label) from WorkingMemory state."""
        orch = wm.get("last_orchestration_result") or {}
        coding = wm.get("last_coding_result") or {}

        for task_dict, prefix in ((orch, "Building"), (coding, "Coding")):
            if isinstance(task_dict, dict) and task_dict.get("status") == "running":
                goal = str(task_dict.get("goal", ""))[:40]
                return "#ffcc00", f"{prefix}: {goal}"

        cost_hit = bool(wm.get("cost_limit_reached"))
        if cost_hit:
            return "#ff4444", "Cost limit reached"

        active_goal = str(wm.get("active_goal") or "").strip()
        if active_goal:
            return "#aa88ff", active_goal[:50]

        return "#44ff44", "Idle"

    def _on_mouse_press(self, event) -> None:
        """Left click = toggle expand/collapse. Right click = context menu."""
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QMenu
        from PyQt6.QtGui import QCursor

        if event.button() == Qt.MouseButton.RightButton:
            self._show_context_menu()
        else:
            self._expanded = not self._expanded
            for lbl in self._detail_labels:
                lbl.setVisible(self._expanded)
            if self._window:
                self._window.adjustSize()

    def _show_context_menu(self) -> None:
        """Show right-click context menu."""
        from PyQt6.QtWidgets import QMenu, QDialog, QDialogButtonBox, QVBoxLayout, QLabel
        from PyQt6.QtGui import QCursor

        menu = QMenu(self._window)
        rollback_action = menu.addAction("Rollback to last checkpoint")
        log_action = menu.addAction("View full log")
        action = menu.exec(QCursor.pos())

        if action == rollback_action:
            self._do_rollback()
        elif action == log_action:
            self._show_log_viewer()

    def _do_rollback(self) -> None:
        """Confirm and execute rollback to last checkpoint."""
        from PyQt6.QtWidgets import QMessageBox
        sha = self._git.last_checkpoint_sha()
        if not sha:
            QMessageBox.information(self._window, "Rollback", "No checkpoint found.")
            return
        reply = QMessageBox.question(
            self._window,
            "Rollback",
            f"Roll back to {sha}? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            ok = self._git.rollback(sha)
            log_event("hud_rollback_confirmed", {"sha": sha, "success": ok})
            status = "Rollback succeeded." if ok else "Rollback failed."
            QMessageBox.information(self._window, "Rollback", status)

    def _show_log_viewer(self) -> None:
        """Open a plain-text window showing the last 100 log entries."""
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QPlainTextEdit, QDialogButtonBox

        dlg = QDialog(self._window)
        dlg.setWindowTitle("Prometheus Log")
        dlg.resize(700, 500)
        layout = QVBoxLayout(dlg)
        text_edit = QPlainTextEdit()
        text_edit.setReadOnly(True)
        text_edit.setStyleSheet("font-family: monospace; font-size: 10px;")
        content = self._log_viewer.tail_formatted(100)
        text_edit.setPlainText(content)
        # Scroll to bottom
        cursor = text_edit.textCursor()
        from PyQt6.QtGui import QTextCursor
        cursor.movePosition(QTextCursor.MoveOperation.End)
        text_edit.setTextCursor(cursor)
        layout.addWidget(text_edit)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        dlg.exec()

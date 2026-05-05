from __future__ import annotations

import datetime
import json
import math
import os
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

from PyQt6.QtCore import QRectF, Qt, QTimer
from PyQt6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PyQt6.QtWidgets import QApplication, QLineEdit, QMessageBox, QToolTip, QWidget

try:
    import psutil
except Exception:
    psutil = None

# ── File paths ──────────────────────────────────────────────────────────────

STATE_FILE          = Path.home() / ".jarvis" / "visual_state.json"
AUDIO_FILE          = Path.home() / ".jarvis" / "audio_levels.json"
LOG_DIR             = Path.home() / ".jarvis" / "logs"
TASKS_FILE          = Path.home() / ".jarvis" / "background_tasks.json"
AGENTS_FILE         = Path.home() / ".jarvis" / "agents.json"
HEARTBEAT_FILE      = Path.home() / ".jarvis" / "heartbeat.json"
WORKING_MEMORY_FILE = Path.home() / ".jarvis" / "memory_v2" / "working_memory.json"
COST_LOG_FILE       = Path.home() / ".prometheus" / "cost_log.jsonl"

# ── Layout constants ─────────────────────────────────────────────────────────

_HEADER_H   = 44
_TAB_LABELS = ["MAIN", "OPS", "AGENTS"]

# ── Sidebar constants ─────────────────────────────────────────────────────────

_SIDEBAR_W = 48
_SIDEBAR_ICONS = [
    (0, "⌂", "HOME"),
    (1, "✉", "CHAT"),
    (2, "≡", "ACTV"),
    (3, "⚙", "AGNT"),
    (4, "♥", "DIAG"),
    (5, "◈", "SYS"),
    (6, "$", "COST"),
]

# ── Color palette ─────────────────────────────────────────────────────────────

_TEAL     = QColor(0, 255, 200)
_TEAL_DIM = QColor(0, 180, 140, 180)
_AMBER    = QColor(255, 210, 80, 220)
_RED      = QColor(255, 80, 80, 220)
_GREEN    = QColor(80, 230, 140, 220)
_VIOLET   = QColor(170, 160, 220, 190)
_ORANGE   = QColor(255, 170, 60, 215)
_DIM      = QColor(160, 185, 200, 160)

_STEP_STATUS: dict[str, tuple[str, QColor]] = {
    "complete": ("✓", _GREEN),
    "running":  ("→", _AMBER),
    "pending":  ("○", _DIM),
    "failed":   ("✗", _RED),
}

_SYMBOL_COLORS: dict[str, QColor] = {
    "◆": QColor(34, 224, 255, 230),
    "◇": QColor(200, 230, 255, 200),
    "→": QColor(255, 210, 80, 215),
    "✓": QColor(80, 230, 140, 215),
    "✗": QColor(255, 80, 80, 215),
    "⟳": QColor(170, 160, 220, 190),
    "●": QColor(255, 170, 60, 215),
}
_DEFAULT_LINE_COLOR = QColor(200, 220, 235, 145)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _elapsed(started_at: str) -> str:
    try:
        dt = datetime.datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%S")
        secs = max(0, int((datetime.datetime.now() - dt).total_seconds()))
        return f"{secs // 60}:{secs % 60:02d}"
    except Exception:
        return ""


def format_log_event(rec: dict) -> str | None:
    """
    Convert a raw log record to a one-line activity-stream entry, or None to drop.
    Format: "HH:MM:SS  <symbol>  human description"
    """
    kind    = str(rec.get("kind", ""))
    ts_full = str(rec.get("ts", ""))
    ts      = ts_full[11:19] if len(ts_full) >= 19 else ts_full[:8]

    # Listening
    if kind == "ptt_turn_started":
        return f"{ts}  ◆  Listening — PTT"
    if kind == "wakeword_turn_started":
        return f"{ts}  ◆  Listening — wake word"
    if kind == "barge_in":
        return f"{ts}  ◆  Interrupted"

    # Transcript
    if kind == "transcript":
        text = str(rec.get("transcript", "")).strip()
        return f'{ts}  ◇  "{text[:88]}"' if text else None

    # Executing
    if kind == "tool_call_received":
        args   = rec.get("args") or {}
        action = str(args.get("action", "?"))
        detail = args.get("app") or args.get("script_name") or args.get("query") or ""
        return f"{ts}  →  {action}{f': {detail}' if detail else ''}"
    if kind == "direct_tool_override":
        action = str((rec.get("payload") or {}).get("action", "?"))
        return f"{ts}  →  (direct) {action}"
    if kind == "tool_execute":
        payload = rec.get("payload") or {}
        action  = str(payload.get("action", "")).strip()
        if not action:
            acts = payload.get("actions") or []
            action = " + ".join(str(a.get("action", "?")) for a in acts[:3]) if isinstance(acts, list) and acts else ""
        if not action:
            return None
        detail = payload.get("app") or payload.get("script_name") or payload.get("query") or ""
        return f"{ts}  →  {action}{f': {detail}' if detail else ''}"
    if kind == "visual_state":
        state = str(rec.get("state", ""))
        if state == "processing":
            return f"{ts}  →  Processing"
        if state == "speaking":
            return f"{ts}  ✓  Speaking"
        return None

    # Success
    if kind == "realtime_connected":
        return f"{ts}  ✓  Realtime API connected"
    if kind == "prometheus_started":
        return f"{ts}  ✓  Prometheus online"
    if kind == "background_task_done":
        ok   = bool(rec.get("ok", False))
        desc = str(rec.get("description", ""))[:40]
        return f"{ts}  {'✓' if ok else '✗'}  Background {'done' if ok else 'failed'}: {desc}"
    if kind == "session_summary_written":
        proj = str(rec.get("project", ""))
        return f"{ts}  ●  Session summary saved — {proj}"

    # Errors
    if kind in {"realtime_connection_closed", "realtime_receiver_error"}:
        return f"{ts}  ✗  Connection lost: {str(rec.get('error', ''))[:55]}"
    if kind == "interrupt_error":
        return f"{ts}  ✗  Interrupt error: {str(rec.get('error', ''))[:55]}"
    if kind == "workspace_watcher_error":
        return f"{ts}  ✗  Workspace error: {str(rec.get('error', ''))[:55]}"
    if kind == "workspace_working_memory_error":
        return f"{ts}  ✗  Memory write error: {str(rec.get('error', ''))[:55]}"

    # Background / status
    if kind == "background_task_submitted":
        desc = str(rec.get("description", ""))[:45]
        return f"{ts}  ⟳  Background task queued: {desc}"
    if kind == "prometheus_stopped":
        return f"{ts}  ⟳  Shutting down"
    if kind in {"workspace_project_changed", "workspace_changed"}:
        name = str(rec.get("name") or rec.get("to") or "unknown")
        return f"{ts}  ⟳  Project: {name}"
    if kind == "workspace_watcher_started":
        return f"{ts}  ⟳  Workspace watcher ready"
    if kind == "ptt_started":
        return f"{ts}  ⟳  PTT armed — Ctrl+Win+Alt"
    if kind == "mic_started":
        return f"{ts}  ⟳  Mic ready"
    if kind == "realtime_closed":
        return f"{ts}  ⟳  Realtime connection closed"

    # Memory / vault
    if kind == "vault_context_loaded":
        return f"{ts}  ●  Vault: {rec.get('count', 0)} memories loaded for {rec.get('project', '')}"

    return None


# ── Data store ────────────────────────────────────────────────────────────────

class Store:
    MAX_ACTIVITY = 50

    def __init__(self) -> None:
        self.state:              str        = "armed"
        self.mic:                float      = 0.0
        self.spk:                float      = 0.0
        self.lines:              list[str]  = []
        self.active_tab:         int        = 0
        self.bg_tasks:           list[dict] = []
        self.agents:             list[dict] = []
        self.heartbeat_ok:       bool       = False
        self._log_mtime:         float      = 0.0
        self.chat_history:       list[dict] = []
        self.activity_filter:    str        = "ALL"
        self.diagnostic:         dict       = {}
        self.cost_log:           list[dict] = []
        self._last_chat_resp_ts: str        = ""

    def refresh(self) -> None:
        # Visual state + active tab
        try:
            data           = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            self.state     = str(data.get("state", "armed"))
            self.active_tab = max(0, min(6, int(data.get("active_hud_tab", 0))))
        except Exception:
            pass

        # Audio levels
        try:
            data     = json.loads(AUDIO_FILE.read_text(encoding="utf-8"))
            self.mic = float(data.get("mic_level", 0.0))
            self.spk = float(data.get("speaker_level", 0.0))
        except Exception:
            self.mic = self.spk = 0.0

        # Activity log — read only when file changes
        try:
            files = sorted(LOG_DIR.glob("*.jsonl"))
            if files:
                latest = files[-1]
                mtime  = latest.stat().st_mtime
                if mtime != self._log_mtime:
                    self._log_mtime = mtime
                    entries: list[str] = []
                    for raw in latest.read_text(encoding="utf-8", errors="ignore").splitlines():
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            rec = json.loads(raw)
                        except Exception:
                            continue
                        line = format_log_event(rec)
                        if line:
                            entries.append(line)
                    self.lines = entries[-self.MAX_ACTIVITY:]
        except Exception:
            pass

        # Background tasks
        try:
            data          = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
            self.bg_tasks = data.get("tasks") or []
        except Exception:
            self.bg_tasks = []

        # Agents
        try:
            data        = json.loads(AGENTS_FILE.read_text(encoding="utf-8"))
            self.agents = data.get("agents") or []
        except Exception:
            self.agents = []

        # Heartbeat
        try:
            data = json.loads(HEARTBEAT_FILE.read_text(encoding="utf-8"))
            dt   = datetime.datetime.strptime(str(data.get("ts", "")), "%Y-%m-%dT%H:%M:%S")
            self.heartbeat_ok = (datetime.datetime.now() - dt).total_seconds() <= 10.0
        except Exception:
            self.heartbeat_ok = False

        # Diagnostic — read from working memory
        try:
            if WORKING_MEMORY_FILE.exists():
                wm_data = json.loads(WORKING_MEMORY_FILE.read_text(encoding="utf-8"))
                diag = wm_data.get("last_diagnostic")
                if isinstance(diag, dict):
                    self.diagnostic = diag
                # Chat response polling
                chat_resp = wm_data.get("chat_response")
                if isinstance(chat_resp, dict):
                    ts = str(chat_resp.get("ts", ""))
                    if ts and ts != self._last_chat_resp_ts:
                        self._last_chat_resp_ts = ts
                        self.chat_history.append({
                            "role": "assistant",
                            "text": str(chat_resp.get("text", ""))[:500],
                            "ts": ts,
                        })
                        if len(self.chat_history) > 50:
                            self.chat_history = self.chat_history[-50:]
        except Exception:
            pass

        # Cost log — last 10 lines
        try:
            if COST_LOG_FILE.exists():
                raw_lines = COST_LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
                entries = []
                for raw in raw_lines[-10:]:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        entries.append(json.loads(raw))
                    except Exception:
                        pass
                self.cost_log = entries
        except Exception:
            self.cost_log = []

    @property
    def drive(self) -> float:
        return max(self.mic, self.spk)


# ── System stats ──────────────────────────────────────────────────────────────

class SystemStats:
    def __init__(self) -> None:
        self.cpu           = 0.0
        self.mem           = 0.0
        self.net_down_kbps = 0.0
        self.net_up_kbps   = 0.0
        self.cpu_hist      = deque([0.0] * 120, maxlen=120)
        self.mem_hist      = deque([0.0] * 120, maxlen=120)
        self.down_hist     = deque([0.0] * 120, maxlen=120)
        self.up_hist       = deque([0.0] * 120, maxlen=120)
        self._last_net     = None
        self._last_t       = time.time()
        if psutil:
            try:
                self._last_net = psutil.net_io_counters()
            except Exception:
                pass

    def refresh(self) -> None:
        now = time.time()
        if psutil:
            try:
                self.cpu = float(psutil.cpu_percent(interval=None))
                self.mem = float(psutil.virtual_memory().percent)
            except Exception:
                self.cpu = self.mem = 0.0
            try:
                cur = psutil.net_io_counters()
                if self._last_net is not None:
                    dt = max(0.001, now - self._last_t)
                    self.net_down_kbps = max(0.0, (cur.bytes_recv - self._last_net.bytes_recv) / 1024.0 / dt)
                    self.net_up_kbps   = max(0.0, (cur.bytes_sent - self._last_net.bytes_sent) / 1024.0 / dt)
                self._last_net = cur
                self._last_t   = now
            except Exception:
                self.net_down_kbps = self.net_up_kbps = 0.0
        else:
            t = now
            self.cpu           = 14 + abs(math.sin(t * 0.7)) * 32
            self.mem           = 32 + abs(math.sin(t * 0.21 + 0.8)) * 18
            self.net_down_kbps = 25 + abs(math.sin(t * 1.18)) * 420
            self.net_up_kbps   = 5  + abs(math.sin(t * 0.92 + 1.2)) * 120
        self.cpu_hist.append(self.cpu)
        self.mem_hist.append(self.mem)
        self.down_hist.append(min(100.0, self.net_down_kbps / 10.0))
        self.up_hist.append(min(100.0, self.net_up_kbps / 4.0))


# ── HUD window ────────────────────────────────────────────────────────────────

class HUDWindow(QWidget):
    def __init__(self, store: Store, stats: SystemStats):
        super().__init__()
        self.store = store
        self.stats = stats
        self.phase     = 0.0
        self.orbit_vel = 0.0

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowFlags(Qt.WindowType.Window)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)

        screen = QGuiApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            self.setGeometry(geo.x() + 20, geo.y() + 20,
                             max(980, geo.width() - 40),
                             max(720, geo.height() - 40))
        else:
            self.resize(1280, 900)

        self.setWindowTitle("PROMETHEUS HUD")

        self.anim = QTimer(self)
        self.anim.timeout.connect(self.tick)
        self.anim.start(16)

        # Chat input widget (shown only when tab 1 is active)
        self._chat_input = QLineEdit(self)
        self._chat_input.setPlaceholderText("Type a message...")
        self._chat_input.setStyleSheet(
            "QLineEdit { background: rgba(4,10,20,220); color: rgb(200,230,250); "
            "border: 1px solid rgba(0,200,160,120); border-radius: 6px; padding: 4px 8px; "
            "font-size: 11px; }"
        )
        self._chat_input.hide()
        self._chat_input.returnPressed.connect(self._send_chat)

    # ── Colors ────────────────────────────────────────────────────────────────

    def color(self) -> QColor:
        return QColor(150, 110, 255, 255) if self.store.state == "processing" else QColor(34, 224, 255, 255)

    def accent_color(self) -> QColor:
        if self.store.state == "speaking":
            return QColor(255, 188, 110, 255)
        if self.store.state == "processing":
            return QColor(150, 110, 255, 255)
        return QColor(34, 224, 255, 255)

    def target_orbit_speed(self) -> float:
        if self.store.state == "speaking":
            return 0.125 + self.store.drive * 0.100
        if self.store.state == "processing":
            return -0.100
        if self.store.state == "listening":
            return 0.075
        return 0.032

    def tick(self):
        target = self.target_orbit_speed()
        delta  = target - self.orbit_vel
        changing = (self.orbit_vel > 0 > target) or (self.orbit_vel < 0 < target)
        accel = 0.34 if changing else (0.24 if abs(delta) > 0.03 else 0.050)
        self.orbit_vel += delta * accel
        self.phase     += self.orbit_vel
        self.update()

    def is_compact_mode(self) -> bool:
        screen = QGuiApplication.primaryScreen()
        if not screen:
            return self.width() <= 640 or self.height() <= 500
        geo = screen.availableGeometry()
        return self.width() <= geo.width() * 0.5 and self.height() <= geo.height() * 0.5

    # ── Tab / dot geometry ────────────────────────────────────────────────────

    def _outer_rect(self) -> QRectF:
        return QRectF(10, 10, self.width() - 20, self.height() - 20)

    def _sidebar_rect(self) -> QRectF:
        outer = self._outer_rect()
        return QRectF(outer.left(), outer.top(), _SIDEBAR_W, outer.height())

    def _content_rect(self) -> QRectF:
        outer = self._outer_rect()
        return QRectF(outer.left() + _SIDEBAR_W + 1, outer.top(),
                      outer.width() - _SIDEBAR_W - 1, outer.height())

    def _sidebar_btn_rect(self, idx: int) -> QRectF:
        sidebar = self._sidebar_rect()
        btn_size = 40.0
        start_y = sidebar.top() + 60.0
        spacing = 52.0
        x = sidebar.left() + (_SIDEBAR_W - btn_size) / 2
        y = start_y + idx * spacing
        return QRectF(x, y, btn_size, btn_size)

    def _tab_btn_rect(self, idx: int) -> QRectF:
        r      = self._outer_rect()
        bw, bh = 50, 26
        gap    = 5
        x      = r.right() - 12 - (3 * bw + 2 * gap) + idx * (bw + gap)
        y      = r.top() + (_HEADER_H - bh) / 2
        return QRectF(x, y, bw, bh)

    def _restart_btn_rect(self) -> QRectF:
        sz    = 20 if self.is_compact_mode() else 24
        first = self._tab_btn_rect(0)
        x     = first.left() - 8 - sz
        y     = first.center().y() - sz / 2
        return QRectF(x, y, sz, sz)

    def _dot_btn_rect(self) -> QRectF:
        d       = 14
        restart = self._restart_btn_rect()
        return QRectF(restart.left() - 10 - d, restart.center().y() - d / 2, d, d)

    # ── Tab state ─────────────────────────────────────────────────────────────

    def _set_tab(self, tab: int) -> None:
        self.store.active_tab = max(0, min(6, tab))
        try:
            data: dict = {}
            if STATE_FILE.exists():
                try:
                    data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                except Exception:
                    pass
            data["active_hud_tab"] = self.store.active_tab
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, STATE_FILE)
        except Exception:
            pass
        # Show chat input only on tab 1
        if self.store.active_tab == 1 and not self.is_compact_mode():
            self._chat_input.show()
            self._position_chat_input()
        else:
            self._chat_input.hide()
        self.update()

    # ── Mouse handling ────────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        pos = event.position()
        x, y = pos.x(), pos.y()
        if self.is_compact_mode():
            # Compact mode: old 3-tab header buttons
            for i in range(3):
                if self._tab_btn_rect(i).contains(x, y):
                    self._set_tab(i)
                    return
        else:
            # Full mode: sidebar tab buttons (7 tabs)
            for idx, _icon, _label in _SIDEBAR_ICONS:
                if self._sidebar_btn_rect(idx).contains(x, y):
                    self._set_tab(idx)
                    return
        if self._restart_btn_rect().contains(x, y):
            self._handle_restart_click()
            return
        if self._dot_btn_rect().contains(x, y):
            self._handle_restart_click()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        pos  = event.position()
        x, y = pos.x(), pos.y()
        if self._restart_btn_rect().contains(x, y):
            QToolTip.showText(event.globalPosition().toPoint(), "Restart Prometheus core", self)
        else:
            QToolTip.hideText()
        super().mouseMoveEvent(event)

    def _handle_restart_click(self) -> None:
        reply = QMessageBox.question(
            self,
            "Prometheus",
            "Restart Prometheus core?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        subprocess.Popen(
            ["systemctl", "--user", "restart", "prometheus"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _send_chat(self) -> None:
        """Send the typed chat message to working_memory["chat_input"]."""
        text = self._chat_input.text().strip()
        if not text:
            return
        self._chat_input.clear()
        self.store.chat_history.append({
            "role": "user",
            "text": text,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        if len(self.store.chat_history) > 50:
            self.store.chat_history = self.store.chat_history[-50:]
        try:
            wm_file = WORKING_MEMORY_FILE
            data: dict = {}
            if wm_file.exists():
                try:
                    data = json.loads(wm_file.read_text(encoding="utf-8"))
                except Exception:
                    pass
            data["chat_input"] = {
                "text": text,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            wm_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = wm_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, wm_file)
        except Exception:
            pass
        self.update()

    def _position_chat_input(self) -> None:
        """Position the chat input widget at the bottom of the chat content area."""
        if self.is_compact_mode():
            self._chat_input.hide()
            return
        content = self._content_rect()
        input_h = 28
        margin = 12
        x = int(content.left() + margin)
        y = int(self.height() - 10 - _HEADER_H - input_h - margin)
        w = int(content.width() - margin * 2)
        self._chat_input.setGeometry(x, y, w, input_h)

    def closeEvent(self, event) -> None:
        running_tasks = False
        try:
            data = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
            running_tasks = any(
                isinstance(t, dict) and t.get("status") == "running"
                for t in (data.get("tasks") or [])
            )
        except Exception:
            pass

        if running_tasks:
            reply = QMessageBox.question(
                self,
                "Prometheus",
                "Background tasks are running. Stop Prometheus anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return

        try:
            subprocess.run(
                ["systemctl", "--user", "stop", "prometheus", "prometheus-hud"],
                timeout=3.0,
                start_new_session=True,
            )
        except subprocess.TimeoutExpired:
            subprocess.Popen(
                ["pkill", "-9", "-f", "python3 main.py"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
        event.accept()

    # ── Header bar ────────────────────────────────────────────────────────────

    def _draw_header_bar(self, p: QPainter, rect: QRectF) -> None:
        # Separator line
        p.setPen(QPen(QColor(80, 160, 200, 50), 1))
        sep_y = rect.top() + _HEADER_H
        p.drawLine(int(rect.left() + 16), int(sep_y), int(rect.right() - 16), int(sep_y))

        # Status dot
        dot = self._dot_btn_rect()
        dot_color = QColor(60, 220, 100) if self.store.heartbeat_ok else QColor(220, 60, 60)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(dot_color)
        p.drawEllipse(dot)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(dot_color.red(), dot_color.green(), dot_color.blue(), 90), 1))
        p.drawEllipse(dot.adjusted(-3, -3, 3, 3))

        # Restart button (↺)
        rbtn = self._restart_btn_rect()
        rpath = QPainterPath()
        rpath.addRoundedRect(rbtn, 5, 5)
        p.fillPath(rpath, QColor(0, 255, 200, 14))
        p.setPen(QPen(QColor(0, 255, 200, 60), 1))
        p.drawRoundedRect(rbtn, 5, 5)
        rf = QFont()
        rf.setPointSize(max(8, int(rbtn.height() * 0.55)))
        p.setFont(rf)
        p.setPen(QColor(0, 255, 200, 220))
        p.drawText(rbtn, Qt.AlignmentFlag.AlignCenter, "↺")

        # Tab buttons — only shown in compact mode (sidebar replaces in full mode)
        if self.is_compact_mode():
            btn_font = QFont("Monospace")
            btn_font.setPointSize(8)
            btn_font.setBold(True)
            p.setFont(btn_font)

            for i, label in enumerate(_TAB_LABELS):
                btn = self._tab_btn_rect(i)
                is_active = self.store.active_tab == i
                path = QPainterPath()
                path.addRoundedRect(btn, 5, 5)
                if is_active:
                    p.fillPath(path, QColor(0, 180, 140, 80))
                    p.setPen(QPen(QColor(0, 255, 200, 200), 1.2))
                    p.drawRoundedRect(btn, 5, 5)
                    p.setPen(QColor(0, 255, 200, 235))
                else:
                    p.fillPath(path, QColor(255, 255, 255, 12))
                    p.setPen(QColor(155, 185, 205, 155))
                p.drawText(btn, Qt.AlignmentFlag.AlignCenter, label)

    # ── Sidebar ───────────────────────────────────────────────────────────────

    def _draw_sidebar(self, p: QPainter) -> None:
        sidebar = self._sidebar_rect()
        # Sidebar background (darker than main)
        sb_path = QPainterPath()
        sb_path.addRoundedRect(
            QRectF(sidebar.left(), sidebar.top(), sidebar.width(), sidebar.height()),
            20, 20,
        )
        p.fillPath(sb_path, QColor(1, 3, 6, 250))
        # Right separator line
        p.setPen(QPen(QColor(40, 120, 180, 60), 1))
        p.drawLine(
            int(sidebar.right()), int(sidebar.top() + 20),
            int(sidebar.right()), int(sidebar.bottom() - 20),
        )

        icon_font = QFont()
        icon_font.setPointSize(14)
        label_font = QFont("Monospace")
        label_font.setPointSize(6)
        label_font.setBold(True)

        for idx, icon, label in _SIDEBAR_ICONS:
            btn = self._sidebar_btn_rect(idx)
            is_active = self.store.active_tab == idx
            if is_active:
                btn_path = QPainterPath()
                btn_path.addRoundedRect(btn, 8, 8)
                p.fillPath(btn_path, QColor(0, 180, 140, 60))
                p.setPen(QPen(QColor(0, 255, 200, 140), 1))
                p.drawRoundedRect(btn, 8, 8)
                icon_color = QColor(0, 255, 200, 235)
                label_color = QColor(0, 255, 200, 200)
            else:
                icon_color = QColor(160, 190, 210, 140)
                label_color = QColor(120, 160, 190, 100)

            p.setFont(icon_font)
            p.setPen(icon_color)
            icon_rect = QRectF(btn.left(), btn.top(), btn.width(), btn.height() * 0.65)
            p.drawText(icon_rect, Qt.AlignmentFlag.AlignCenter, icon)

            p.setFont(label_font)
            p.setPen(label_color)
            label_rect = QRectF(btn.left(), btn.top() + btn.height() * 0.60, btn.width(), btn.height() * 0.40)
            p.drawText(label_rect, Qt.AlignmentFlag.AlignCenter, label)

    # ── Panel background ──────────────────────────────────────────────────────

    def _panel(self, p: QPainter, rect: QRectF, title: str):
        path = QPainterPath()
        path.addRoundedRect(rect, 16, 16)
        grad = QLinearGradient(rect.left(), rect.top(), rect.left(), rect.bottom())
        grad.setColorAt(0.0, QColor(6, 10, 16, 236))
        grad.setColorAt(1.0, QColor(4, 7, 12, 224))
        p.fillPath(path, grad)
        p.setPen(QPen(QColor(100, 200, 255, 95), 1.5))
        p.drawRoundedRect(rect, 16, 16)
        title_font = QFont()
        title_font.setPointSize(9)
        title_font.setBold(True)
        p.setFont(title_font)
        p.setPen(QColor(220, 240, 250, 180))
        p.drawText(rect.adjusted(14, 10, -14, 0), title)

    # ── Graph panel ───────────────────────────────────────────────────────────

    def _draw_graph(self, p: QPainter, rect: QRectF, values, title: str, value_text: str):
        self._panel(p, rect, title)
        p.setPen(QColor(220, 240, 250, 180))
        p.drawText(rect.adjusted(14, 10, -14, 0), Qt.AlignmentFlag.AlignRight, value_text)
        plot = rect.adjusted(14, 30, -14, -12)
        pp = QPainterPath()
        pp.addRoundedRect(plot, 8, 8)
        p.fillPath(pp, QColor(3, 8, 14, 230))
        p.setPen(QPen(QColor(255, 255, 255, 18), 1))
        for i in range(1, 5):
            y = plot.top() + plot.height() * (i / 5.0)
            p.drawLine(int(plot.left()), int(y), int(plot.right()), int(y))
        vals = list(values)
        if len(vals) < 2:
            return
        line = QPainterPath()
        fill = QPainterPath()
        for i, v in enumerate(vals):
            x = plot.left() + (i / (len(vals) - 1)) * plot.width()
            y = plot.bottom() - (max(0.0, min(100.0, v)) / 100.0) * plot.height()
            if i == 0:
                line.moveTo(x, y)
                fill.moveTo(x, plot.bottom())
                fill.lineTo(x, y)
            else:
                line.lineTo(x, y)
                fill.lineTo(x, y)
        fill.lineTo(plot.right(), plot.bottom())
        fill.closeSubpath()
        c = self.color()
        fg = QLinearGradient(plot.left(), plot.top(), plot.left(), plot.bottom())
        fg.setColorAt(0.0, QColor(c.red(), c.green(), c.blue(), 120))
        fg.setColorAt(1.0, QColor(c.red(), c.green(), c.blue(), 10))
        p.fillPath(fill, fg)
        p.setPen(QPen(QColor(c.red(), c.green(), c.blue(), 220), 2))
        p.drawPath(line)

    # ── Corner gauge ──────────────────────────────────────────────────────────

    def _draw_corner_gauge(self, p: QPainter, rect: QRectF, label: str, value: float, value_text: str):
        c  = self.color()
        cx = rect.center().x()
        cy = rect.center().y()
        r  = min(rect.width(), rect.height()) * 0.44
        path = QPainterPath()
        path.addEllipse(rect)
        g = QLinearGradient(rect.left(), rect.top(), rect.left(), rect.bottom())
        g.setColorAt(0.0, QColor(8, 14, 24, 232))
        g.setColorAt(1.0, QColor(4, 8, 14, 220))
        p.fillPath(path, g)
        p.setPen(QPen(QColor(c.red(), c.green(), c.blue(), 70), 1.4))
        p.drawEllipse(rect)
        self._ring(p, cx, cy, r * 0.80, 70, 1)
        self._ring(p, cx, cy, r * 0.58, 55, 1)
        sd, fs = 220, 260
        sp = fs * max(0.0, min(1.0, value / 100.0))
        p.setPen(QPen(QColor(255, 255, 255, 20), 3))
        p.drawArc(QRectF(cx - r * 0.82, cy - r * 0.82, r * 1.64, r * 1.64), int(-sd * 16), int(-fs * 16))
        p.setPen(QPen(QColor(c.red(), c.green(), c.blue(), 230), 3))
        p.drawArc(QRectF(cx - r * 0.82, cy - r * 0.82, r * 1.64, r * 1.64), int(-sd * 16), int(-sp * 16))
        lf = QFont()
        lf.setPointSize(max(8, int(r * 0.13)))
        lf.setBold(True)
        p.setFont(lf)
        p.setPen(QColor(210, 232, 245, 185))
        p.drawText(QRectF(cx - r * 0.75, cy - r * 0.60, r * 1.5, 20), Qt.AlignmentFlag.AlignCenter, label)
        vf = QFont()
        vf.setPointSize(max(10, int(r * 0.21)))
        vf.setBold(True)
        p.setFont(vf)
        p.setPen(QColor(235, 246, 252, 230))
        p.drawText(QRectF(cx - r * 0.78, cy - 12, r * 1.56, 26), Qt.AlignmentFlag.AlignCenter, value_text)

    # ── Core animation helpers ─────────────────────────────────────────────────

    def _ring(self, p, cx, cy, r, alpha, width):
        c = self.color()
        p.setPen(QPen(QColor(c.red(), c.green(), c.blue(), alpha), width))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))

    def _arc(self, p, cx, cy, r, start_deg, span_deg, alpha, width, accent=False):
        c = self.accent_color() if accent else self.color()
        p.setPen(QPen(QColor(c.red(), c.green(), c.blue(), alpha), width))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(QRectF(cx - r, cy - r, r * 2, r * 2), int(-start_deg * 16), int(-span_deg * 16))

    def _segmented_band(self, p, cx, cy, r, segments, draw_every, span_deg, alpha, width, offset_deg=0.0, speed_mul=1.0):
        c = self.color()
        p.setPen(QPen(QColor(c.red(), c.green(), c.blue(), alpha), width))
        base = math.degrees(self.phase * speed_mul) + offset_deg
        step = 360 / segments
        for i in range(segments):
            if i % draw_every != 0:
                continue
            p.drawArc(QRectF(cx - r, cy - r, r * 2, r * 2), int(-(base + i * step) * 16), int(-span_deg * 16))

    def _tick_band(self, p, cx, cy, r, tick_count, inner, outer, alpha, width, speed_mul=0.0):
        c = self.color()
        p.setPen(QPen(QColor(c.red(), c.green(), c.blue(), alpha), width))
        for i in range(tick_count):
            if i % 2 != 0:
                continue
            ang = (i / tick_count) * math.tau + self.phase * speed_mul
            p.drawLine(int(cx + math.cos(ang) * (r - inner)), int(cy + math.sin(ang) * (r - inner)),
                       int(cx + math.cos(ang) * (r + outer)), int(cy + math.sin(ang) * (r + outer)))

    # ── Core visualization ────────────────────────────────────────────────────

    def _draw_core(self, p: QPainter, rect: QRectF):
        cx    = rect.center().x()
        cy    = rect.center().y() + 12
        s     = min(rect.width(), rect.height()) * 0.94
        drive = self.store.drive
        c     = self.color()
        a     = self.accent_color()

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(c.red(), c.green(), c.blue(), 7))
        p.drawEllipse(QRectF(cx - s * 0.36, cy - s * 0.36, s * 0.72, s * 0.72))

        self._ring(p, cx, cy, s * 0.095, 170, 4)
        self._ring(p, cx, cy, s * 0.148, 122, 3)
        self._ring(p, cx, cy, s * 0.205,  86, 2)
        self._ring(p, cx, cy, s * 0.286,  52, 2)
        self._ring(p, cx, cy, s * 0.344,  28, 1)

        self._segmented_band(p, cx, cy, s * 0.118, 48, 2, 5.0, 118, 2, 0.0,   1.10)
        self._segmented_band(p, cx, cy, s * 0.172, 62, 3, 4.0,  98, 2, 18.0, -0.86)
        self._segmented_band(p, cx, cy, s * 0.226, 84, 4, 3.0,  82, 2, 54.0,  0.66)

        base = math.degrees(self.phase) * 1.35
        self._arc(p, cx, cy, s * 0.124,  base + 12,         84, 250, 7, accent=True)
        self._arc(p, cx, cy, s * 0.182, -base * 0.78 + 34,  60, 210, 5)
        self._arc(p, cx, cy, s * 0.240,  base * 0.56 + 210, 96, 178, 5)
        self._arc(p, cx, cy, s * 0.306, -base * 0.36 + 302, 68, 138, 5)

        self._tick_band(p, cx, cy, s * 0.152, 48, s * 0.005, s * 0.010, 74, 2, 0.22)

        p.setPen(QPen(QColor(c.red(), c.green(), c.blue(), 24), 1))
        p.drawLine(int(cx - s * 0.37), int(cy), int(cx + s * 0.37), int(cy))
        p.drawLine(int(cx), int(cy - s * 0.29), int(cx), int(cy + s * 0.29))

        p.setPen(QPen(QColor(c.red(), c.green(), c.blue(), 112), 3))
        bw, bh = s * 0.11, s * 0.14
        for side in (-1, 1):
            bx = cx + side * s * 0.35
            p.drawLine(int(bx), int(cy - bh / 2), int(bx), int(cy + bh / 2))
            p.drawLine(int(bx), int(cy - bh / 2), int(bx - side * bw), int(cy - bh / 2))
            p.drawLine(int(bx), int(cy + bh / 2), int(bx - side * bw), int(cy + bh / 2))

        pulse   = 1.0 + drive * 0.18 + (0.05 * math.sin(self.phase * 2.2))
        core_r  = s * 0.050 * pulse
        ig      = QLinearGradient(cx, cy - core_r, cx, cy + core_r)
        ig.setColorAt(0.0, QColor(235, 248, 255, 245))
        ig.setColorAt(1.0, QColor(a.red(), a.green(), a.blue(), 180))
        cp = QPainterPath()
        cp.addEllipse(QRectF(cx - core_r, cy - core_r, core_r * 2, core_r * 2))
        p.fillPath(cp, ig)
        p.setPen(QPen(QColor(210, 240, 255, 220), 1.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QRectF(cx - s * 0.052, cy - s * 0.052, s * 0.104, s * 0.104))

        bw2    = s * 0.52
        bh2    = s * 0.095
        tb     = QRectF(cx - bw2 / 2, cy - bh2 / 2, bw2, bh2)
        bp     = QPainterPath()
        bp.addRoundedRect(tb, bh2 * 0.24, bh2 * 0.24)
        gg     = QLinearGradient(tb.left(), tb.top(), tb.left(), tb.bottom())
        gg.setColorAt(0.0, QColor(9, 14, 22, 232))
        gg.setColorAt(1.0, QColor(5, 9, 15, 224))
        p.fillPath(bp, gg)
        p.setPen(QPen(QColor(120, 210, 255, 62), 1.1))
        p.drawRoundedRect(tb, bh2 * 0.24, bh2 * 0.24)

        cf = QFont()
        cf.setPointSize(max(10, int(s * 0.026)))
        cf.setBold(True)
        cf.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, max(1.0, s * 0.0018))
        p.setFont(cf)
        p.setPen(QColor(235, 246, 252, 238))
        p.drawText(tb, Qt.AlignmentFlag.AlignCenter, "PROMETHEUS")

        tf = QFont()
        tf.setPointSize(max(7, int(s * 0.012)))
        tf.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
        p.setFont(tf)
        p.setPen(QColor(170, 220, 245, 170))
        tw, th = s * 0.10, s * 0.030
        for text, dx, dy in [("SYS", 0, -s * 0.255), ("LINK", s * 0.245, 0), ("CORE", 0, s * 0.225), ("AUX", -s * 0.245, 0)]:
            p.drawText(QRectF(cx + dx - tw / 2, cy + dy - th / 2, tw, th), Qt.AlignmentFlag.AlignCenter, text)

    # ── Tab 0: Activity stream ────────────────────────────────────────────────

    def _draw_logs(self, p: QPainter, rect: QRectF):
        self._panel(p, rect, "ACTIVITY")
        body = rect.adjusted(14, 34, -14, -12)
        clip = QPainterPath()
        clip.addRoundedRect(body, 8, 8)
        p.setClipPath(clip)
        p.fillRect(body, QColor(3, 8, 14, 235))

        font = QFont("Monospace")
        font.setPointSize(9)
        font.setStyleHint(QFont.StyleHint.Monospace)
        p.setFont(font)

        line_h   = 17
        max_vis  = max(1, int((body.height() - 8) / line_h))
        visible  = self.store.lines[-max_vis:]
        y        = body.top() + 16

        for line in visible:
            parts  = line.split("  ", 2)
            symbol = parts[1].strip() if len(parts) >= 2 else ""
            p.setPen(_SYMBOL_COLORS.get(symbol, _DEFAULT_LINE_COLOR))
            p.drawText(QRectF(body.left() + 8, y - 11, body.width() - 16, line_h + 3),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       line[:260])
            y += line_h

        p.setClipping(False)

    # ── Tab 1: Background ops ─────────────────────────────────────────────────

    def _draw_ops_panel(self, p: QPainter, rect: QRectF):
        self._panel(p, rect, "BACKGROUND OPS")
        body = rect.adjusted(14, 34, -14, -12)

        tasks = [t for t in self.store.bg_tasks if isinstance(t, dict)]

        if not tasks:
            p.setPen(_DIM)
            nf = QFont()
            nf.setPointSize(10)
            p.setFont(nf)
            p.drawText(body, Qt.AlignmentFlag.AlignCenter, "No active tasks")
            return

        mono     = QFont("Monospace")
        mono.setPointSize(9)
        step_h   = 16
        card_gap = 8
        y        = body.top() + 4

        for task in reversed(tasks):
            steps   = [s for s in (task.get("steps") or []) if isinstance(s, dict)]
            card_h  = 26 + max(1, len(steps)) * step_h + 14 + 10
            if y + card_h > body.bottom() - 2:
                break

            card = QRectF(body.left(), y, body.width(), card_h)
            cp   = QPainterPath()
            cp.addRoundedRect(card, 8, 8)
            p.fillPath(cp, QColor(4, 10, 18, 215))
            p.setPen(QPen(QColor(60, 120, 160, 75), 1))
            p.drawRoundedRect(card, 8, 8)

            # Title
            tf = QFont()
            tf.setPointSize(9)
            tf.setBold(True)
            p.setFont(tf)
            p.setPen(QColor(220, 235, 245, 218))
            p.drawText(QRectF(card.left() + 10, card.top() + 5, card.width() - 82, 20),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       str(task.get("intent", "Task"))[:55])

            # Elapsed / status badge (top-right)
            elapsed   = _elapsed(str(task.get("started_at", "")))
            t_status  = str(task.get("status", "running"))
            completed = bool(task.get("completed_at"))
            if completed and t_status == "complete":
                badge_color = _GREEN
                badge = f"✓ {elapsed}"
            elif t_status == "failed":
                badge_color = _RED
                badge = f"✗ {elapsed}"
            else:
                badge_color = _AMBER
                badge = elapsed
            p.setFont(mono)
            p.setPen(badge_color)
            p.drawText(QRectF(card.right() - 78, card.top() + 5, 70, 20),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, badge)

            # Steps
            sy          = card.top() + 26
            done_count  = 0
            for step in steps:
                s_status           = str(step.get("status", "pending"))
                symbol, step_color = _STEP_STATUS.get(s_status, ("○", _DIM))
                if s_status == "complete":
                    done_count += 1
                p.setFont(mono)
                p.setPen(step_color)
                name = str(step.get("name", step.get("action", "?")))[:42]
                p.drawText(QRectF(card.left() + 10, sy, card.width() - 20, step_h),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                           f"{symbol}  {name}")

                # Inline step progress bar for running step
                if s_status == "running" and "progress" in step:
                    prog      = float(step["progress"])
                    pb        = QRectF(card.right() - 80, sy + 4, 68, 8)
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(QColor(30, 50, 60, 150))
                    p.drawRoundedRect(pb, 4, 4)
                    fill = pb.adjusted(0, 0, -(pb.width() * (1 - prog)), 0)
                    if fill.width() > 0:
                        p.setBrush(_TEAL_DIM)
                        p.drawRoundedRect(fill, 4, 4)
                sy += step_h

            # Overall progress bar
            total    = len(steps)
            progress = done_count / total if total > 0 else 0.0
            pb_y     = card.bottom() - 12
            pb       = QRectF(card.left() + 10, pb_y, card.width() - 20, 5)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(20, 40, 55, 200))
            p.drawRoundedRect(pb, 2, 2)
            if progress > 0:
                fill = pb.adjusted(0, 0, -(pb.width() * (1 - progress)), 0)
                p.setBrush(_TEAL)
                p.drawRoundedRect(fill, 2, 2)

            y += card_h + card_gap

    # ── Tab 2: Agents ─────────────────────────────────────────────────────────

    def _draw_agents_panel(self, p: QPainter, rect: QRectF):
        self._panel(p, rect, "AGENTS")
        body = rect.adjusted(14, 34, -14, -12)

        agents = [a for a in self.store.agents if isinstance(a, dict)]

        if not agents:
            p.setPen(_DIM)
            nf = QFont()
            nf.setPointSize(10)
            p.setFont(nf)
            p.drawText(body, Qt.AlignmentFlag.AlignCenter, "No agents running")
            return

        y        = body.top() + 4
        card_h   = 66
        card_gap = 8
        mono     = QFont("Monospace")
        mono.setPointSize(8)

        for agent in agents:
            if y + card_h > body.bottom() - 2:
                break
            card = QRectF(body.left(), y, body.width(), card_h)
            cp   = QPainterPath()
            cp.addRoundedRect(card, 8, 8)
            p.fillPath(cp, QColor(4, 10, 18, 215))
            p.setPen(QPen(QColor(60, 120, 160, 75), 1))
            p.drawRoundedRect(card, 8, 8)

            a_status  = str(agent.get("status", "idle")).lower()
            dot_color = _GREEN if a_status == "active" else (_AMBER if a_status == "running" else _DIM)

            # Status dot
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(dot_color)
            p.drawEllipse(QRectF(card.left() + 10, card.top() + 10, 10, 10))

            # Name
            nf = QFont()
            nf.setPointSize(10)
            nf.setBold(True)
            p.setFont(nf)
            p.setPen(QColor(220, 235, 248, 220))
            p.drawText(QRectF(card.left() + 28, card.top() + 5, card.width() - 110, 22),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       str(agent.get("name", "Agent"))[:28])

            # Status badge
            p.setFont(mono)
            p.setPen(dot_color)
            p.drawText(QRectF(card.right() - 90, card.top() + 5, 82, 22),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       a_status.upper())

            # Current task
            tf = QFont()
            tf.setPointSize(9)
            p.setFont(tf)
            p.setPen(_DIM)
            task_text = str(agent.get("current_task") or "—")[:48]
            p.drawText(QRectF(card.left() + 10, card.top() + 28, card.width() - 20, 18),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       f"Task: {task_text}")

            # Last active
            last = str(agent.get("last_active", ""))
            if last:
                last_short = last[11:16] if len(last) >= 16 else last
                p.drawText(QRectF(card.left() + 10, card.top() + 46, card.width() - 20, 16),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                           f"Last active: {last_short}")
            y += card_h + card_gap

    # ── Tab 1: Chat ───────────────────────────────────────────────────────────

    def _draw_chat_tab(self, p: QPainter, rect: QRectF) -> None:
        self._panel(p, rect, "CHAT")
        body = rect.adjusted(14, 34, -14, -12)
        input_h = 36
        msg_area = QRectF(body.left(), body.top(), body.width(), body.height() - input_h - 8)
        clip = QPainterPath()
        clip.addRoundedRect(msg_area, 8, 8)
        p.setClipPath(clip)
        p.fillRect(msg_area, QColor(3, 8, 14, 235))

        msg_font = QFont("Monospace")
        msg_font.setPointSize(9)
        p.setFont(msg_font)
        line_h = 18
        max_vis = max(1, int((msg_area.height() - 8) / line_h))
        messages = self.store.chat_history[-max_vis:]
        y = msg_area.top() + 14
        for msg in messages:
            role = str(msg.get("role", "user"))
            text = str(msg.get("text", ""))
            ts = str(msg.get("ts", ""))[11:16]
            if role == "user":
                p.setPen(QColor(100, 200, 255, 200))
                prefix = f"[{ts}] You: "
            else:
                p.setPen(QColor(0, 220, 160, 200))
                prefix = f"[{ts}] Prometheus: "
            full = prefix + text
            p.drawText(
                QRectF(msg_area.left() + 8, y - 12, msg_area.width() - 16, line_h + 4),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                full[:130],
            )
            y += line_h

        p.setClipping(False)

        # Draw input area outline (actual widget positioned by _position_chat_input)
        input_rect = QRectF(body.left(), body.bottom() - input_h, body.width(), input_h)
        ip = QPainterPath()
        ip.addRoundedRect(input_rect, 6, 6)
        p.fillPath(ip, QColor(4, 10, 20, 200))
        p.setPen(QPen(QColor(0, 200, 160, 100), 1))
        p.drawRoundedRect(input_rect, 6, 6)

        # Ensure widget is positioned correctly
        self._position_chat_input()

    # ── Tab 2: Activity (full height) ─────────────────────────────────────────

    def _draw_activity_tab(self, p: QPainter, rect: QRectF) -> None:
        self._panel(p, rect, "ACTIVITY")
        # Filter row at top
        filter_h = 24.0
        filter_y = rect.top() + 34.0
        filters = ["ALL", "TOOLS", "VOICE", "ERRORS"]
        fw = 52.0
        fx = rect.left() + 14.0
        btn_font = QFont("Monospace")
        btn_font.setPointSize(7)
        btn_font.setBold(True)
        p.setFont(btn_font)
        for fl in filters:
            fr = QRectF(fx, filter_y, fw, filter_h)
            is_active = self.store.activity_filter == fl
            fp = QPainterPath()
            fp.addRoundedRect(fr, 4, 4)
            if is_active:
                p.fillPath(fp, QColor(0, 180, 140, 70))
                p.setPen(QPen(QColor(0, 255, 200, 160), 1))
                p.drawRoundedRect(fr, 4, 4)
                p.setPen(QColor(0, 255, 200, 220))
            else:
                p.fillPath(fp, QColor(255, 255, 255, 8))
                p.setPen(QColor(140, 170, 190, 130))
            p.drawText(fr, Qt.AlignmentFlag.AlignCenter, fl)
            fx += fw + 6.0

        # Log body below filters
        log_body = QRectF(rect.left() + 14, filter_y + filter_h + 4,
                          rect.width() - 28, rect.bottom() - filter_y - filter_h - 8)
        clip = QPainterPath()
        clip.addRoundedRect(log_body, 8, 8)
        p.setClipPath(clip)
        p.fillRect(log_body, QColor(3, 8, 14, 235))

        font = QFont("Monospace")
        font.setPointSize(9)
        font.setStyleHint(QFont.StyleHint.Monospace)
        p.setFont(font)

        # Apply filter
        lines = self.store.lines
        fl = self.store.activity_filter
        if fl == "TOOLS":
            lines = [l for l in lines if "→" in l]
        elif fl == "VOICE":
            lines = [l for l in lines if "◆" in l or "◇" in l or "✓" in l]
        elif fl == "ERRORS":
            lines = [l for l in lines if "✗" in l]

        line_h = 17
        max_vis = max(1, int((log_body.height() - 8) / line_h))
        visible = lines[-max_vis:]
        y = log_body.top() + 16
        for line in visible:
            parts = line.split("  ", 2)
            symbol = parts[1].strip() if len(parts) >= 2 else ""
            p.setPen(_SYMBOL_COLORS.get(symbol, _DEFAULT_LINE_COLOR))
            p.drawText(
                QRectF(log_body.left() + 8, y - 11, log_body.width() - 16, line_h + 3),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                line[:260],
            )
            y += line_h

        p.setClipping(False)

    # ── Tab 4: Diagnostics ────────────────────────────────────────────────────

    def _draw_diag_tab(self, p: QPainter, rect: QRectF) -> None:
        self._panel(p, rect, "DIAGNOSTICS")
        body = rect.adjusted(14, 34, -14, -12)
        diag = self.store.diagnostic

        if not diag:
            p.setPen(_DIM)
            nf = QFont()
            nf.setPointSize(10)
            p.setFont(nf)
            p.drawText(body, Qt.AlignmentFlag.AlignCenter, "No diagnostic data\nSay 'run diagnostics' to check systems")
            return

        # Timestamp
        ts = str(diag.get("ts", ""))
        p.setPen(QColor(120, 160, 190, 150))
        ts_font = QFont("Monospace")
        ts_font.setPointSize(8)
        p.setFont(ts_font)
        p.drawText(QRectF(body.left(), body.top(), body.width(), 20),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   f"Last run: {ts[11:19] if len(ts) >= 19 else ts}")

        # Summary line
        spoken = str(diag.get("spoken_summary", ""))
        if spoken:
            sf = QFont()
            sf.setPointSize(10)
            sf.setBold(True)
            p.setFont(sf)
            if "critical" in spoken.lower():
                p.setPen(_RED)
            elif "warning" in spoken.lower():
                p.setPen(_AMBER)
            else:
                p.setPen(_GREEN)
            p.drawText(QRectF(body.left(), body.top() + 20, body.width(), 24),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       spoken[:120])

        # Subsystem grid
        subsystems = [
            ("Voice",       diag.get("voice", {}),             lambda d: d.get("connected", True)),
            ("Ollama",      diag.get("ollama", {}),            lambda d: d.get("available", False)),
            ("Claude Code", diag.get("claude_code", {}),      lambda d: d.get("on_path", False)),
            ("Vault",       diag.get("vault", {}),             lambda d: d.get("db_exists", False)),
            ("Workers",     diag.get("background_workers", {}), lambda d: d.get("stuck_tasks", 0) == 0),
            ("Watchdog",    diag.get("watchdog", {}),          lambda d: bool(d.get("last_check_ts"))),
            ("System",      diag.get("system", {}),            lambda d: d.get("cpu_pct", 0) < 90),
            ("Cost",        diag.get("cost", {}),              lambda d: d.get("pct_used", 0) < 80),
        ]

        row_h = 22.0
        col_w = (body.width() - 8) / 2.0
        y0 = body.top() + 50.0
        label_font = QFont()
        label_font.setPointSize(9)
        val_font = QFont("Monospace")
        val_font.setPointSize(8)

        for i, (name, data, ok_fn) in enumerate(subsystems):
            row = i // 2
            col = i % 2
            rx = body.left() + col * col_w
            ry = y0 + row * (row_h + 4)
            try:
                ok = ok_fn(data)
            except Exception:
                ok = False
            dot_color = _GREEN if ok else _RED

            # Dot
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(dot_color)
            p.drawEllipse(QRectF(rx + 4, ry + 6, 10, 10))
            p.setBrush(Qt.BrushStyle.NoBrush)

            # Name
            p.setFont(label_font)
            p.setPen(QColor(210, 230, 245, 200))
            p.drawText(QRectF(rx + 20, ry, col_w - 24, row_h),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       name)

            # Value detail
            detail = ""
            if name == "Ollama":
                ms = data.get("latency_ms", -1)
                detail = f"{ms}ms" if ms >= 0 else "offline"
            elif name == "Vault":
                detail = f"{data.get('chunk_count', 0)} chunks"
            elif name == "Cost":
                pct = data.get("pct_used", 0)
                detail = f"{pct:.0f}%"
            elif name == "Workers":
                detail = f"{data.get('active_tasks', 0)} active"
            elif name == "System":
                detail = f"CPU {data.get('cpu_pct', 0):.0f}%"

            if detail:
                p.setFont(val_font)
                p.setPen(QColor(140, 170, 200, 150))
                p.drawText(QRectF(rx + col_w - 70, ry, 66, row_h),
                           Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                           detail)

    # ── Tab 5: System ─────────────────────────────────────────────────────────

    def _draw_system_tab(self, p: QPainter, rect: QRectF) -> None:
        self._panel(p, rect, "SYSTEM")
        body = rect.adjusted(14, 34, -14, -12)

        mono = QFont("Monospace")
        mono.setPointSize(9)
        label_font = QFont()
        label_font.setPointSize(9)
        label_font.setBold(True)

        # Resource bars
        bars = [
            ("CPU", self.stats.cpu, f"{self.stats.cpu:.0f}%"),
            ("MEM", self.stats.mem, f"{self.stats.mem:.0f}%"),
            ("NET↓", min(100.0, self.stats.net_down_kbps / 10.0), f"{self.stats.net_down_kbps:.0f} KB/s"),
            ("NET↑", min(100.0, self.stats.net_up_kbps / 4.0),   f"{self.stats.net_up_kbps:.0f} KB/s"),
        ]

        bar_h = 14.0
        bar_gap = 10.0
        label_w = 42.0
        val_w = 72.0
        bar_w = body.width() - label_w - val_w - 8

        y = body.top() + 4.0
        c = self.color()
        for lbl, pct, val_str in bars:
            # Label
            p.setFont(label_font)
            p.setPen(_DIM)
            p.drawText(QRectF(body.left(), y, label_w, bar_h),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, lbl)
            # Bar background
            bar_rect = QRectF(body.left() + label_w, y, bar_w, bar_h)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(20, 40, 55, 200))
            p.drawRoundedRect(bar_rect, 4, 4)
            # Bar fill
            fill_w = bar_rect.width() * max(0.0, min(1.0, pct / 100.0))
            if fill_w > 0:
                fill_rect = QRectF(bar_rect.left(), bar_rect.top(), fill_w, bar_rect.height())
                p.setBrush(QColor(c.red(), c.green(), c.blue(), 180))
                p.drawRoundedRect(fill_rect, 4, 4)
            p.setBrush(Qt.BrushStyle.NoBrush)
            # Value
            p.setFont(mono)
            p.setPen(QColor(c.red(), c.green(), c.blue(), 200))
            p.drawText(QRectF(bar_rect.right() + 6, y, val_w, bar_h),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, val_str)
            y += bar_h + bar_gap

        # Divider
        y += 4.0
        p.setPen(QPen(QColor(60, 120, 160, 50), 1))
        p.drawLine(int(body.left()), int(y), int(body.right()), int(y))
        y += 8.0

        # Worker pool
        p.setFont(label_font)
        p.setPen(QColor(200, 225, 245, 200))
        p.drawText(QRectF(body.left(), y, body.width(), 18),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "BACKGROUND WORKERS")
        y += 20.0

        tasks = [t for t in self.store.bg_tasks if isinstance(t, dict)]
        if not tasks:
            p.setFont(mono)
            p.setPen(_DIM)
            p.drawText(QRectF(body.left() + 8, y, body.width(), 18),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "No active tasks")
            y += 20.0
        else:
            for task in tasks[:4]:
                intent = str(task.get("intent", "Task"))[:40]
                status = str(task.get("status", "running"))
                elapsed = _elapsed(str(task.get("started_at", "")))
                s_color = _GREEN if status == "complete" else (_RED if status in ("failed", "timeout") else _AMBER)
                p.setFont(mono)
                p.setPen(s_color)
                p.drawText(QRectF(body.left() + 8, y, body.width() - 80, 18),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                           f"• {intent}")
                p.setPen(_DIM)
                p.drawText(QRectF(body.right() - 70, y, 66, 18),
                           Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                           elapsed or status)
                y += 18.0

    # ── Tab 6: Cost ───────────────────────────────────────────────────────────

    def _draw_cost_tab(self, p: QPainter, rect: QRectF) -> None:
        self._panel(p, rect, "COST")
        body = rect.adjusted(14, 34, -14, -12)

        diag = self.store.diagnostic
        cost = diag.get("cost", {}) if diag else {}

        mono = QFont("Monospace")
        mono.setPointSize(10)
        label_font = QFont()
        label_font.setPointSize(9)
        label_font.setBold(True)
        small_font = QFont("Monospace")
        small_font.setPointSize(8)

        # Big numbers
        session_usd = float(cost.get("session_usd", 0.0))
        daily_usd   = float(cost.get("daily_usd", 0.0))
        limit_usd   = float(cost.get("daily_limit_usd", 5.0))
        pct_used    = float(cost.get("pct_used", 0.0))

        p.setFont(label_font)
        p.setPen(QColor(160, 190, 210, 180))
        p.drawText(QRectF(body.left(), body.top(), body.width() / 2, 18),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "SESSION")
        p.drawText(QRectF(body.left() + body.width() / 2, body.top(), body.width() / 2, 18),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "DAILY")

        p.setFont(mono)
        c = self.color()
        p.setPen(QColor(c.red(), c.green(), c.blue(), 230))
        p.drawText(QRectF(body.left(), body.top() + 18, body.width() / 2, 28),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   f"${session_usd:.4f}")
        p.drawText(QRectF(body.left() + body.width() / 2, body.top() + 18, body.width() / 2, 28),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   f"${daily_usd:.4f} / ${limit_usd:.2f}")

        # Daily usage bar
        bar_y = body.top() + 54.0
        bar_rect = QRectF(body.left(), bar_y, body.width(), 10.0)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(20, 40, 55, 200))
        p.drawRoundedRect(bar_rect, 4, 4)
        fill_pct = max(0.0, min(1.0, pct_used / 100.0))
        if fill_pct > 0:
            bar_color = _RED if pct_used > 80 else (_AMBER if pct_used > 50 else c)
            fill_rect = QRectF(bar_rect.left(), bar_rect.top(), bar_rect.width() * fill_pct, bar_rect.height())
            p.setBrush(bar_color)
            p.drawRoundedRect(fill_rect, 4, 4)
        p.setBrush(Qt.BrushStyle.NoBrush)

        p.setFont(small_font)
        p.setPen(_DIM)
        p.drawText(QRectF(body.left(), bar_y + 12, body.width(), 16),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   f"{pct_used:.1f}% of daily limit used")

        # Cost log table
        table_y = bar_y + 34.0
        p.setFont(label_font)
        p.setPen(QColor(180, 210, 230, 180))
        p.drawText(QRectF(body.left(), table_y, body.width(), 18),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   "RECENT API CALLS")
        table_y += 20.0

        p.setFont(small_font)
        if not self.store.cost_log:
            p.setPen(_DIM)
            p.drawText(QRectF(body.left() + 8, table_y, body.width(), 18),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       "No cost log data available")
        else:
            row_h = 16.0
            for entry in reversed(self.store.cost_log):
                if table_y + row_h > body.bottom():
                    break
                if not isinstance(entry, dict):
                    continue
                ts = str(entry.get("ts", ""))[11:16]
                model = str(entry.get("model", ""))[:20]
                cost_val = float(entry.get("cost_usd", 0.0))
                line = f"{ts}  {model:<22}  ${cost_val:.5f}"
                p.setPen(QColor(160, 190, 215, 160))
                p.drawText(QRectF(body.left() + 8, table_y, body.width() - 16, row_h),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                           line[:80])
                table_y += row_h

    # ── Compact mode ──────────────────────────────────────────────────────────

    def _draw_compact_identity(self, p: QPainter, rect: QRectF):
        cf = QFont()
        cf.setPointSize(max(8, int(rect.height() * 0.14)))
        cf.setBold(True)
        cf.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
        p.setFont(cf)
        p.setPen(QColor(235, 246, 252, 238))
        p.drawText(QRectF(rect.left(), rect.top() + 4, rect.width(), 26),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "PROMETHEUS")
        c = self.color()
        sf = QFont("Monospace")
        sf.setPointSize(8)
        p.setFont(sf)
        p.setPen(c)
        p.drawText(QRectF(rect.left(), rect.top() + 30, rect.width(), 18),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   f"● {self.store.state.upper()}")

    def _draw_compact_activity(self, p: QPainter, rect: QRectF):
        tab = self.store.active_tab
        if tab == 1:
            text = f"OPS — {len(self.store.bg_tasks)} task(s)"
        elif tab == 2:
            text = f"AGENTS — {len(self.store.agents)} agent(s)"
        elif self.store.lines:
            raw   = self.store.lines[-1]
            parts = raw.split("  ", 2)
            text  = parts[2].strip() if len(parts) >= 3 else raw
        else:
            text = "No recent activity"

        font = QFont("Monospace")
        font.setPointSize(9)
        p.setFont(font)
        p.setPen(_DEFAULT_LINE_COLOR)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text[:80])

    def _draw_compact_metrics(self, p: QPainter, rect: QRectF):
        mono = QFont("Monospace")
        mono.setPointSize(9)
        p.setFont(mono)
        row_h   = rect.height() / 3
        metrics = [
            ("CPU", f"{self.stats.cpu:.0f}%"),
            ("MEM", f"{self.stats.mem:.0f}%"),
            ("NET", f"{self.stats.net_down_kbps:.0f} KB/s"),
        ]
        c = self.color()
        for i, (label, value) in enumerate(metrics):
            ry = rect.top() + i * row_h
            p.setPen(_DIM)
            p.drawText(QRectF(rect.left(), ry, 34, row_h),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
            p.setPen(QColor(c.red(), c.green(), c.blue(), 220))
            p.drawText(QRectF(rect.left() + 36, ry, rect.width() - 36, row_h),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, value)

    # ── Paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = self._outer_rect()

        # Background
        bg = QPainterPath()
        bg.addRoundedRect(rect, 20, 20)
        p.fillPath(bg, QColor(2, 5, 9, 244))
        p.setPen(QPen(QColor(90, 190, 255, 90), 2))
        p.drawRoundedRect(rect, 20, 20)

        # Header bar (both modes)
        self._draw_header_bar(p, rect)

        if self.is_compact_mode():
            # Compact layout: header identical to full mode.
            # Content: PROMETHEUS identity (left) | divider | 3 circular gauges (right).
            # No logs, OPS cards, or agents — center panel hidden entirely.
            content = rect.adjusted(0, _HEADER_H, 0, 0)
            margin  = 8          # 8px from window edge on all sides
            inner   = content.adjusted(margin, margin, -margin, -margin)

            gauge_gap  = 12      # 12px between each gauge
            # Clamp gauge diameter: min 48px, max 64px, scale to available height
            gauge_size = max(48.0, min(64.0, (inner.height() - gauge_gap * 2) / 3))
            gauge_w    = gauge_size

            # Center the gauge stack vertically in the available space
            stack_h = gauge_size * 3 + gauge_gap * 2
            gy0 = inner.top() + max(0.0, (inner.height() - stack_h) / 2)
            gx  = inner.right() - gauge_w

            net_val = min(100.0, self.stats.net_down_kbps / 10.0)
            cpu_gr = QRectF(gx, gy0,                                gauge_w, gauge_size)
            ram_gr = QRectF(gx, gy0 + gauge_size + gauge_gap,       gauge_w, gauge_size)
            net_gr = QRectF(gx, gy0 + (gauge_size + gauge_gap) * 2, gauge_w, gauge_size)

            self._draw_corner_gauge(p, cpu_gr, "CPU", self.stats.cpu, f"{self.stats.cpu:.0f}%")
            self._draw_corner_gauge(p, ram_gr, "RAM", self.stats.mem, f"{self.stats.mem:.0f}%")
            self._draw_corner_gauge(p, net_gr, "NET", net_val,        f"{self.stats.net_down_kbps:.0f}k")

            # Center divider line — equal visual weight between sides
            left_w    = inner.width() - gauge_w - 16
            divider_x = inner.left() + left_w + 8
            p.setPen(QPen(QColor(26, 26, 46, 180), 1))
            p.drawLine(int(divider_x), int(inner.top() + 4),
                       int(divider_x), int(inner.bottom() - 4))

            # Left column: PROMETHEUS + state — guarantee no overlap with divider
            id_rect = QRectF(inner.left(), inner.top(), left_w, inner.height())
            self._draw_compact_identity(p, id_rect)
            return

        # Full mode layout — sidebar + content area
        self._draw_sidebar(p)
        content_rect = self._content_rect()
        content = content_rect.adjusted(8, _HEADER_H + 8, -8, -8)

        tab = self.store.active_tab

        if tab == 0:
            # HOME: existing layout (core + graphs + logs)
            top_h    = max(260, content.height() * 0.62)
            bottom_h = content.height() - top_h - 18
            gap      = 18
            left_w   = content.width() * 0.68
            right_w  = content.width() - left_w - gap

            core_rect = QRectF(content.left(), content.top(), left_w, top_h - 10)
            rx        = core_rect.right() + gap
            graph_h   = (top_h - gap * 3) / 4.0

            cpu_r  = QRectF(rx, content.top(),                right_w, graph_h)
            mem_r  = QRectF(rx, cpu_r.bottom() + gap,         right_w, graph_h)
            down_r = QRectF(rx, mem_r.bottom() + gap,         right_w, graph_h)
            up_r   = QRectF(rx, down_r.bottom() + gap,        right_w, graph_h)
            tab_r  = QRectF(content.left(), core_rect.bottom() + 18, content.width(), bottom_h)

            self._draw_core(p, core_rect)
            self._draw_graph(p, cpu_r,  self.stats.cpu_hist,  "CPU LOAD", f"{self.stats.cpu:.0f}%")
            self._draw_graph(p, mem_r,  self.stats.mem_hist,  "MEMORY",   f"{self.stats.mem:.0f}%")
            self._draw_graph(p, down_r, self.stats.down_hist, "NET DOWN",  f"{self.stats.net_down_kbps:.0f} KB/s")
            self._draw_graph(p, up_r,   self.stats.up_hist,   "NET UP",    f"{self.stats.net_up_kbps:.0f} KB/s")
            self._draw_logs(p, tab_r)

        elif tab == 1:
            # CHAT
            self._draw_chat_tab(p, content.adjusted(-8, -8, 8, 8))

        elif tab == 2:
            # ACTIVITY (full height with filter)
            self._draw_activity_tab(p, content.adjusted(-8, -8, 8, 8))

        elif tab == 3:
            # AGENTS
            self._draw_agents_panel(p, content.adjusted(-8, -8, 8, 8))

        elif tab == 4:
            # DIAG
            self._draw_diag_tab(p, content.adjusted(-8, -8, 8, 8))

        elif tab == 5:
            # SYSTEM (resource bars + worker pool)
            self._draw_system_tab(p, content.adjusted(-8, -8, 8, 8))

        elif tab == 6:
            # COST
            self._draw_cost_tab(p, content.adjusted(-8, -8, 8, 8))


# ── App ───────────────────────────────────────────────────────────────────────

class App:
    def __init__(self):
        print("PROMETHEUS HUD launched")
        self.app   = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(True)
        self.store = Store()
        self.stats = SystemStats()
        self.win   = HUDWindow(self.store, self.stats)

        self.data_timer = QTimer()
        self.data_timer.timeout.connect(self.refresh)
        self.data_timer.start(120)

        self.stats_timer = QTimer()
        self.stats_timer.timeout.connect(self.stats.refresh)
        self.stats_timer.start(1000)

    def refresh(self):
        self.store.refresh()
        self.win.update()

    def run(self):
        self.win.show()
        self.win.activateWindow()
        return self.app.exec()


if __name__ == "__main__":
    raise SystemExit(App().run())

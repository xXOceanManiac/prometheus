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
    QFontMetrics,
    QGuiApplication,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PyQt6.QtWidgets import QApplication, QLineEdit, QMessageBox, QToolTip, QWidget

try:
    import psutil
except Exception:
    psutil = None

# ─────────────────────────────────────────────────────────────────────────────
# PROMETHEUS HUD — Light Source Concept (Production Pass)
# Design: Prometheus is the living amber intelligence behind opaque white cards.
# Light is visible in seams between cards, not overlaid on them.
# ─────────────────────────────────────────────────────────────────────────────

DEMO = os.environ.get("PROMETHEUS_HUD_DEMO") == "1"

STATE_FILE = Path.home() / ".jarvis" / "visual_state.json"
AUDIO_FILE = Path.home() / ".jarvis" / "audio_levels.json"
LOG_DIR = Path.home() / ".jarvis" / "logs"
TASKS_FILE = Path.home() / ".jarvis" / "background_tasks.json"
AGENTS_FILE = Path.home() / ".jarvis" / "agents.json"
HEARTBEAT_FILE = Path.home() / ".jarvis" / "heartbeat.json"
WORKING_MEMORY_FILE = Path.home() / ".jarvis" / "memory_v2" / "working_memory.json"
MISSION_FILE = Path.home() / ".jarvis" / "memory_v2" / "mission_state.json"
COST_LOG_FILE = Path.home() / ".prometheus" / "cost_log.jsonl"

# ── Palette — neutral glass over contained Prometheus light ──────────────────
# The UI should not be amber-washed. Amber is reserved for localized seam light.
C_BG = QColor(246, 247, 245, 255)
C_BG_2 = QColor(250, 250, 248, 255)
C_CARD = QColor(255, 255, 252, 255)
C_CARD_2 = QColor(252, 252, 249, 255)
C_TEXT = QColor(28, 30, 31, 246)
C_TEXT_2 = QColor(78, 78, 74, 218)
C_TEXT_3 = QColor(132, 130, 124, 180)
C_LINE = QColor(214, 216, 212, 112)
C_LINE_2 = QColor(228, 229, 225, 92)
C_AMBER = QColor(220, 136, 28, 255)
C_AMBER_2 = QColor(248, 184, 63, 240)
C_AMBER_FAINT = QColor(220, 136, 28, 30)
C_AMBER_SOFT = QColor(255, 204, 92, 34)
C_GREEN = QColor(73, 152, 86, 230)
C_RED = QColor(208, 70, 55, 230)
C_BLUE = QColor(78, 139, 178, 210)
C_VIOLET = QColor(132, 105, 182, 210)

# ── Layout constants — tighter, more air between cards ───────────────────────
SIDEBAR_W = 152
GAP = 14
HEADER_H = 80
BOTTOM_H = 72

NAV_ITEMS = [
    (0, "✧", "Command"),
    (1, "◌", "Intelligence"),
    (2, "◇", "Knowledge"),
    (3, "⌘", "Operations"),
    (4, "◷", "Calendar"),
    (5, "⚙", "Systems"),
    (6, "⧉", "Integrations"),
    (7, "☼", "Settings"),
]


def clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def mix_rect(a: QRectF, b: QRectF, t: float) -> QRectF:
    return QRectF(
        lerp(a.left(), b.left(), t),
        lerp(a.top(), b.top(), t),
        lerp(a.width(), b.width(), t),
        lerp(a.height(), b.height(), t),
    )


def _elapsed(started_at: str) -> str:
    try:
        dt = datetime.datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%S")
        secs = max(0, int((datetime.datetime.now() - dt).total_seconds()))
        return f"{secs // 60}:{secs % 60:02d}"
    except Exception:
        return ""


def format_log_event(rec: dict) -> str | None:
    kind = str(rec.get("kind", ""))
    ts_full = str(rec.get("ts", ""))
    ts = ts_full[11:19] if len(ts_full) >= 19 else ts_full[:8]

    if kind in {"ptt_turn_started", "wakeword_turn_started"}:
        return (
            f"{ts}  Listening — {'PTT' if kind == 'ptt_turn_started' else 'wake word'}"
        )
    if kind == "barge_in":
        return f"{ts}  Interrupted"
    if kind == "transcript":
        text = str(rec.get("transcript", "")).strip()
        return f"{ts}  “{text[:88]}”" if text else None
    if kind in {"tool_call_received", "tool_execute", "direct_tool_override"}:
        args = rec.get("args") or rec.get("payload") or {}
        action = str(args.get("action", "tool"))
        detail = args.get("app") or args.get("script_name") or args.get("query") or ""
        return f"{ts}  {action}{f': {detail}' if detail else ''}"
    if kind == "visual_state":
        state = str(rec.get("state", ""))
        return f"{ts}  {state.capitalize()}" if state else None
    if kind == "realtime_connected":
        return f"{ts}  Realtime API connected"
    if kind == "prometheus_started":
        return f"{ts}  Prometheus online"
    if kind == "background_task_done":
        desc = str(rec.get("description", ""))[:42]
        return f"{ts}  Background {'done' if rec.get('ok') else 'failed'}: {desc}"
    if kind == "background_task_submitted":
        return f"{ts}  Background task queued: {str(rec.get('description', ''))[:42]}"
    if kind in {
        "realtime_connection_closed",
        "realtime_receiver_error",
        "interrupt_error",
    }:
        return f"{ts}  Connection issue: {str(rec.get('error', ''))[:55]}"
    if kind in {"workspace_project_changed", "workspace_changed"}:
        return (
            f"{ts}  Project: {str(rec.get('name') or rec.get('to') or 'unknown')[:42]}"
        )
    if kind == "session_summary_written":
        return f"{ts}  Session summary saved"
    return None


class Store:
    MAX_ACTIVITY = 50

    def __init__(self) -> None:
        self.state: str = "armed"
        self.mic: float = 0.0
        self.spk: float = 0.0
        self.lines: list[str] = []
        self.active_tab: int = 0
        self.bg_tasks: list[dict] = []
        self.agents: list[dict] = []
        self.heartbeat_ok: bool = False
        self.chat_history: list[dict] = []
        self.diagnostic: dict = {}
        self.cost_log: list[dict] = []
        self.mission: dict = {}
        self.snapshot: dict = {}
        self.active_project: str = ""
        self.active_window: str = ""
        self.open_windows: list[str] = []
        self._log_mtime: float = 0.0
        self._last_chat_resp_ts: str = ""

    @property
    def drive(self) -> float:
        return max(self.mic, self.spk)

    def refresh(self) -> None:
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            self.state = str(data.get("state", "armed"))
            self.active_tab = max(0, min(7, int(data.get("active_hud_tab", 0))))
            self.active_project = str(data.get("active_project", ""))
            self.active_window = str(data.get("active_window", ""))
            self.open_windows = [str(w) for w in (data.get("open_windows") or [])]
        except Exception:
            pass

        try:
            data = json.loads(AUDIO_FILE.read_text(encoding="utf-8"))
            self.mic = float(data.get("mic_level", 0.0))
            self.spk = float(data.get("speaker_level", 0.0))
        except Exception:
            self.mic = self.spk = 0.0

        try:
            files = sorted(LOG_DIR.glob("*.jsonl"))
            if files:
                latest = files[-1]
                mtime = latest.stat().st_mtime
                if mtime != self._log_mtime:
                    self._log_mtime = mtime
                    entries: list[str] = []
                    for raw in latest.read_text(
                        encoding="utf-8", errors="ignore"
                    ).splitlines():
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
                    self.lines = entries[-self.MAX_ACTIVITY :]
        except Exception:
            pass

        try:
            data = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
            self.bg_tasks = data.get("tasks") or []
        except Exception:
            self.bg_tasks = []

        try:
            data = json.loads(AGENTS_FILE.read_text(encoding="utf-8"))
            self.agents = data.get("agents") or []
        except Exception:
            self.agents = []

        try:
            data = json.loads(HEARTBEAT_FILE.read_text(encoding="utf-8"))
            dt = datetime.datetime.strptime(
                str(data.get("ts", "")), "%Y-%m-%dT%H:%M:%S"
            )
            self.heartbeat_ok = (datetime.datetime.now() - dt).total_seconds() <= 10.0
        except Exception:
            self.heartbeat_ok = False

        try:
            if WORKING_MEMORY_FILE.exists():
                wm = json.loads(WORKING_MEMORY_FILE.read_text(encoding="utf-8"))
                diag = wm.get("last_diagnostic")
                if isinstance(diag, dict):
                    self.diagnostic = diag
                chat_resp = wm.get("chat_response")
                if isinstance(chat_resp, dict):
                    ts = str(chat_resp.get("ts", ""))
                    if ts and ts != self._last_chat_resp_ts:
                        self._last_chat_resp_ts = ts
                        self.chat_history.append(
                            {
                                "role": "assistant",
                                "text": str(chat_resp.get("text", ""))[:900],
                                "ts": ts,
                            }
                        )
                        self.chat_history = self.chat_history[-50:]
        except Exception:
            pass

        try:
            if MISSION_FILE.exists():
                ms = json.loads(MISSION_FILE.read_text(encoding="utf-8"))
                if isinstance(ms, dict):
                    self.mission = ms
        except Exception:
            pass

        try:
            from cognition import build_operational_snapshot

            self.snapshot = build_operational_snapshot()
        except Exception:
            pass

        try:
            if COST_LOG_FILE.exists():
                entries = []
                for raw in COST_LOG_FILE.read_text(
                    encoding="utf-8", errors="ignore"
                ).splitlines()[-10:]:
                    try:
                        entries.append(json.loads(raw))
                    except Exception:
                        pass
                self.cost_log = entries
        except Exception:
            self.cost_log = []


class SystemStats:
    def __init__(self) -> None:
        self.cpu = 0.0
        self.mem = 0.0
        self.net_down_kbps = 0.0
        self.net_up_kbps = 0.0
        self.cpu_hist = deque([0.0] * 96, maxlen=96)
        self.mem_hist = deque([0.0] * 96, maxlen=96)
        self.down_hist = deque([0.0] * 96, maxlen=96)
        self.up_hist = deque([0.0] * 96, maxlen=96)
        self._last_net = None
        self._last_t = time.time()
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
                    self.net_down_kbps = max(
                        0.0, (cur.bytes_recv - self._last_net.bytes_recv) / 1024.0 / dt
                    )
                    self.net_up_kbps = max(
                        0.0, (cur.bytes_sent - self._last_net.bytes_sent) / 1024.0 / dt
                    )
                self._last_net = cur
                self._last_t = now
            except Exception:
                self.net_down_kbps = self.net_up_kbps = 0.0
        else:
            self.cpu = 25 + abs(math.sin(now * 0.55)) * 30
            self.mem = 46 + abs(math.sin(now * 0.2 + 0.8)) * 18
            self.net_down_kbps = 20 + abs(math.sin(now * 1.1)) * 280
            self.net_up_kbps = 6 + abs(math.sin(now * 0.9 + 1.2)) * 80
        self.cpu_hist.append(self.cpu)
        self.mem_hist.append(self.mem)
        self.down_hist.append(min(100.0, self.net_down_kbps / 8.0))
        self.up_hist.append(min(100.0, self.net_up_kbps / 3.0))


class HUDWindow(QWidget):
    def __init__(self, store: Store, stats: SystemStats):
        super().__init__()
        self.store = store
        self.stats = stats
        self.phase = 0.0
        self.light_phase = 0.0
        self.layout_t = 0.0
        self.whisper_cache: dict[str, tuple[str, float]] = {}
        self.tile_rects: dict[str, QRectF] = {}

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowFlags(Qt.WindowType.Window)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)

        screen = QGuiApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            self.setGeometry(
                geo.x() + 20,
                geo.y() + 20,
                max(1100, geo.width() - 40),
                max(760, geo.height() - 40),
            )
        else:
            self.resize(1440, 900)
        self.setWindowTitle("PROMETHEUS HUD")

        self.anim = QTimer(self)
        self.anim.timeout.connect(self.tick)
        self.anim.start(16)

        self._chat_input = QLineEdit(self)
        self._chat_input.setPlaceholderText("Ask anything or give a command…")
        self._chat_input.setStyleSheet(
            "QLineEdit { background: rgba(255,254,250,248); color: rgb(40,36,31); "
            "border: 1px solid rgba(218,142,35,100); border-radius: 10px; padding: 7px 14px; "
            "font-size: 13px; selection-background-color: rgba(245,184,70,120); }"
            "QLineEdit:focus { border: 1.5px solid rgba(218,142,35,180); "
            "background: rgba(255,255,253,252); }"
        )
        self._chat_input.returnPressed.connect(self._send_chat)

    # ── State / color helpers ────────────────────────────────────────────────

    def accent(self) -> QColor:
        # Prometheus is an amber-gold intelligence; avoid state colors washing the HUD.
        if self.store.state == "speaking":
            return C_AMBER_2
        if self.store.state == "listening":
            return QColor(238, 154, 38, 255)
        if self.store.state == "processing":
            return QColor(228, 144, 30, 255)
        if self.store.state == "background_working":
            return QColor(210, 138, 38, 235)
        return C_AMBER

    def is_active_mode(self) -> bool:
        return self.store.state in {
            "processing",
            "speaking",
            "listening",
            "background_working",
        }

    def tick(self) -> None:
        self.phase += 0.018
        state_speed = 1.0
        if self.store.state == "processing":
            state_speed = 2.0
        elif self.store.state == "speaking":
            state_speed = 1.6 + self.store.drive * 1.2
        elif self.store.state == "listening":
            state_speed = 1.35
        self.light_phase += 0.012 * state_speed
        target_t = 1.0 if self.is_active_mode() or self._active_mission_text() else 0.0
        self.layout_t += (target_t - self.layout_t) * 0.06

        # Smooth tile interpolation — layout animates without jump cuts
        target = self.compute_target_layout()
        if not self.tile_rects:
            self.tile_rects = {k: QRectF(v) for k, v in target.items()}
        else:
            for key, trect in target.items():
                curr = self.tile_rects.get(key, trect)
                self.tile_rects[key] = mix_rect(curr, trect, 0.10)

        self.update()

    def _outer_rect(self) -> QRectF:
        return QRectF(12, 12, self.width() - 24, self.height() - 34)

    def _active_mission_text(self) -> str:
        ms = self.store.mission or {}
        return str(ms.get("current_mission") or ms.get("active_goal") or "").strip()

    def _next_action_text(self) -> str:
        ms = self.store.mission or {}
        return str(ms.get("next_action") or "").strip()

    def _nav_rect(self, idx: int) -> QRectF:
        outer = self._outer_rect()
        x = outer.left() + 10
        y = outer.top() + 82 + idx * 44
        return QRectF(x, y, SIDEBAR_W - 20, 35)

    def _restart_btn_rect(self) -> QRectF:
        outer = self._outer_rect()
        header_mid = 12 + (HEADER_H - 12) / 2
        return QRectF(outer.right() - 46, outer.top() + header_mid - 13, 26, 26)

    # ── Input / events ───────────────────────────────────────────────────────

    def resizeEvent(self, _event) -> None:
        self.tile_rects = {}
        self._position_chat_input()

    def mousePressEvent(self, event) -> None:
        pos = event.position()
        x, y = pos.x(), pos.y()
        for idx, _icon, _label in NAV_ITEMS:
            if self._nav_rect(idx).contains(x, y):
                self._set_tab(idx)
                return
        if self._restart_btn_rect().contains(x, y):
            self._handle_restart_click()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._restart_btn_rect().contains(event.position()):
            QToolTip.showText(
                event.globalPosition().toPoint(), "Restart Prometheus core", self
            )
        else:
            QToolTip.hideText()
        super().mouseMoveEvent(event)

    def _set_tab(self, tab: int) -> None:
        self.store.active_tab = max(0, min(7, tab))
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
        self._position_chat_input()
        self.update()

    def _position_chat_input(self) -> None:
        if self.store.active_tab != 0:
            self._chat_input.hide()
            return
        r = self.tile_rects if self.tile_rects else self.compute_target_layout()
        command = r.get("command")
        if not command:
            self._chat_input.hide()
            return
        x = int(command.left() + 22)
        y = int(command.bottom() - 52)
        w = int(command.width() - 44)
        self._chat_input.setGeometry(x, y, max(120, w), 34)
        self._chat_input.show()

    def _send_chat(self) -> None:
        text = self._chat_input.text().strip()
        if not text:
            return
        self._chat_input.clear()
        self.store.chat_history.append(
            {"role": "user", "text": text, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
        )
        self.store.chat_history = self.store.chat_history[-50:]
        try:
            data: dict = {}
            if WORKING_MEMORY_FILE.exists():
                try:
                    data = json.loads(WORKING_MEMORY_FILE.read_text(encoding="utf-8"))
                except Exception:
                    pass
            data["chat_input"] = {
                "text": text,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            WORKING_MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = WORKING_MEMORY_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, WORKING_MEMORY_FILE)
        except Exception:
            pass
        self.update()

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

    def closeEvent(self, event) -> None:
        try:
            subprocess.run(
                ["systemctl", "--user", "stop", "prometheus-hud"],
                timeout=1.5,
                start_new_session=True,
            )
        except Exception:
            pass
        event.accept()

    # ── Drawing primitives ───────────────────────────────────────────────────

    def _font(
        self,
        size: int,
        bold: bool = False,
        mono: bool = False,
        spacing: float | None = None,
    ) -> QFont:
        if mono:
            f = QFont("Fira Code")
            f.setStyleHint(QFont.StyleHint.Monospace)
        else:
            f = QFont("Inter")
            f.setStyleHint(QFont.StyleHint.SansSerif)
        f.setPointSize(size)
        f.setBold(bold)
        if spacing is not None:
            f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
        return f

    def _elide(self, text: str, width: float, size: int = 9, bold: bool = False) -> str:
        fm = QFontMetrics(self._font(size, bold))
        return fm.elidedText(text, Qt.TextElideMode.ElideRight, int(width))

    def _round_rect_path(self, rect: QRectF, radius: float) -> QPainterPath:
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        return path

    def _shadow_card(
        self, p: QPainter, rect: QRectF, radius: float = 18, active: bool = False
    ) -> None:
        # Architectural lift: neutral shadows only. Active cards are handled by tiny accents,
        # not glowing amber outlines.
        for i, (alpha, off, spread) in enumerate(
            [(24, 4, 0), (13, 10, 1), (7, 18, 2), (3, 28, 4)]
        ):
            shadow = QRectF(
                rect.left() - spread,
                rect.top() + off,
                rect.width() + spread * 2,
                rect.height() + spread,
            )
            p.fillPath(
                self._round_rect_path(shadow, radius + spread),
                QColor(34, 36, 34, alpha),
            )
        if active:
            # A faint neutral halo lifts the priority card without making it look outlined.
            p.fillPath(
                self._round_rect_path(rect.adjusted(-2, -2, 2, 2), radius + 2),
                QColor(255, 255, 255, 18),
            )

    def _card(
        self,
        p: QPainter,
        rect: QRectF,
        title: str | None = None,
        subtitle: str | None = None,
        icon: str = "✧",
        active: bool = False,
    ) -> QRectF:
        self._shadow_card(p, rect, 16, active)
        path = self._round_rect_path(rect, 16)
        grad = QLinearGradient(rect.left(), rect.top(), rect.right(), rect.bottom())
        grad.setColorAt(0.0, C_CARD)
        grad.setColorAt(1.0, C_CARD_2)
        p.fillPath(path, grad)
        p.setPen(QPen(C_LINE, 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, 16, 16)

        # Active state: tiny light-catching bevel/accent, not a glowing outline.
        if active:
            c = self.accent()
            accent_rect = QRectF(
                rect.left() + 16, rect.top() + 10, min(78, rect.width() * 0.22), 3
            )
            accent_grad = QLinearGradient(
                accent_rect.left(),
                accent_rect.top(),
                accent_rect.right(),
                accent_rect.top(),
            )
            accent_grad.setColorAt(0.0, QColor(c.red(), c.green(), c.blue(), 190))
            accent_grad.setColorAt(1.0, QColor(c.red(), c.green(), c.blue(), 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(accent_grad)
            p.drawRoundedRect(accent_rect, 2, 2)
            p.setBrush(Qt.BrushStyle.NoBrush)

        body = rect.adjusted(18, 16, -18, -16)
        if title:
            p.setFont(self._font(11, True))
            p.setPen(C_TEXT)
            p.drawText(
                QRectF(body.left() + 26, body.top() - 1, body.width() - 26, 18),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                title,
            )
            p.setFont(self._font(14))
            p.setPen(C_AMBER if active else QColor(170, 114, 34, 205))
            p.drawText(
                QRectF(body.left(), body.top() - 1, 20, 18),
                Qt.AlignmentFlag.AlignCenter,
                icon,
            )
            if subtitle:
                p.setFont(self._font(8))
                p.setPen(C_TEXT_3)
                p.drawText(
                    QRectF(body.left() + 26, body.top() + 17, body.width() - 26, 14),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    subtitle,
                )
            body = body.adjusted(0, 34, 0, 0)
        return body

    def _pill(
        self,
        p: QPainter,
        rect: QRectF,
        text: str,
        active: bool = False,
        icon: str | None = None,
    ) -> None:
        path = self._round_rect_path(rect, 10)
        p.fillPath(
            path,
            QColor(255, 255, 253, 246) if not active else QColor(255, 245, 224, 252),
        )
        p.setPen(QPen(C_AMBER_FAINT if active else C_LINE_2, 1))
        p.drawRoundedRect(rect, 10, 10)
        p.setFont(self._font(8, True))
        p.setPen(C_AMBER if active else C_TEXT_2)
        label = f"{icon}  {text}" if icon else text
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

    def _draw_text(
        self,
        p: QPainter,
        rect: QRectF,
        text: str,
        size: int = 10,
        color: QColor = C_TEXT_2,
        bold: bool = False,
        align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
        mono: bool = False,
    ) -> None:
        p.setFont(self._font(size, bold, mono))
        p.setPen(color)
        p.drawText(rect, align, text)

    def _wrap_lines(self, text: str, max_chars: int) -> list[str]:
        words = text.split()
        lines: list[str] = []
        cur = ""
        for word in words:
            test = (cur + " " + word).strip()
            if len(test) > max_chars and cur:
                lines.append(cur)
                cur = word
            else:
                cur = test
        if cur:
            lines.append(cur)
        return lines

    def _whisper_text(
        self,
        p: QPainter,
        key: str,
        rect: QRectF,
        text: str,
        size: int = 10,
        color: QColor = C_TEXT_2,
        max_lines: int = 4,
        speed: float = 95.0,
    ) -> None:
        cached = self.whisper_cache.get(key)
        now = time.time()
        if cached is None or cached[0] != text:
            self.whisper_cache[key] = (text, now)
            cached = (text, now)
        elapsed = now - cached[1]
        visible_chars = int(elapsed * speed)
        shown = text[: max(0, min(len(text), visible_chars))]
        if len(shown) < len(text):
            shown = shown.rstrip() + "▌"
        lines = self._wrap_lines(shown, max(24, int(rect.width() / (size * 0.58))))[
            :max_lines
        ]
        p.setFont(self._font(size))
        y = rect.top()
        for i, line in enumerate(lines):
            alpha = 255
            if i == len(lines) - 1 and len(shown) < len(text):
                alpha = 160 + int(70 * abs(math.sin(self.phase * 4.0)))
            col = QColor(color.red(), color.green(), color.blue(), alpha)
            p.setPen(col)
            p.drawText(
                QRectF(rect.left(), y, rect.width(), size + 9),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                line,
            )
            y += size + 10

    def _sparkline(self, p: QPainter, rect: QRectF, vals, color: QColor) -> None:
        vals = list(vals)
        if len(vals) < 2:
            return
        path = QPainterPath()
        for i, v in enumerate(vals):
            x = rect.left() + (i / (len(vals) - 1)) * rect.width()
            y = rect.bottom() - clamp(float(v) / 100.0) * rect.height()
            if i == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        p.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 170), 1.5))
        p.drawPath(path)

    # ── Background / shell ───────────────────────────────────────────────────

    def _draw_living_background(self, p: QPainter, rect: QRectF) -> None:
        # Neutral shell. No amber wash: Prometheus light is drawn separately in seams.
        base = self._round_rect_path(rect, 22)
        grad = QLinearGradient(rect.left(), rect.top(), rect.right(), rect.bottom())
        grad.setColorAt(0.0, QColor(252, 253, 251, 255))
        grad.setColorAt(0.52, QColor(247, 248, 246, 255))
        grad.setColorAt(1.0, QColor(241, 243, 241, 255))
        p.fillPath(base, grad)

        # Very faint architectural light — neutral, not amber. This keeps the app from
        # feeling flat while allowing the seam glows to be the only strong color source.
        cx = rect.left() + rect.width() * 0.58
        cy = rect.top() + rect.height() * 0.45
        r = rect.width() * 0.42
        rg = QRadialGradient(cx, cy, r)
        rg.setColorAt(0.0, QColor(255, 255, 255, 44))
        rg.setColorAt(0.65, QColor(255, 255, 255, 12))
        rg.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(rg)
        p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))

        # Soft cool grounding at the far left/bottom; prevents the background from becoming beige.
        cx2 = rect.left() + rect.width() * 0.18
        cy2 = rect.top() + rect.height() * 0.78
        r2 = rect.width() * 0.30
        rg2 = QRadialGradient(cx2, cy2, r2)
        rg2.setColorAt(0.0, QColor(224, 232, 232, 30))
        rg2.setColorAt(1.0, QColor(224, 232, 232, 0))
        p.setBrush(rg2)
        p.drawEllipse(QRectF(cx2 - r2, cy2 - r2, r2 * 2, r2 * 2))

        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(198, 202, 198, 96), 1.0))
        p.drawRoundedRect(rect, 22, 22)

    def _draw_gap_glows(self, p: QPainter) -> None:
        """Contained amber light pools in the negative space between opaque cards."""
        r = self.tile_rects
        if not r:
            return
        acc = self.accent()
        # Small state gain. Never turn this into a page-wide wash.
        if self.store.state == "processing":
            gain = 1.28 + 0.12 * abs(math.sin(self.light_phase * 1.2))
        elif self.store.state == "speaking":
            gain = 1.10 + self.store.drive * 0.55
        elif self.store.state == "listening":
            gain = 1.12
        else:
            gain = 0.78 + 0.10 * abs(math.sin(self.light_phase * 0.55))

        p.setPen(Qt.PenStyle.NoPen)

        def pool(
            cx: float, cy: float, radius: float, alpha: int, warm: bool = True
        ) -> None:
            # The pool is intentionally localized. It fades completely before it reaches
            # the whole window, so the UI stays neutral and the light feels contained.
            rr = max(16.0, radius)
            c = acc if warm else C_AMBER_2
            a = int(clamp(alpha * gain, 0, 135))
            rg = QRadialGradient(cx, cy, rr)
            rg.setColorAt(0.0, QColor(255, 225, 146, min(155, a + 18)))
            rg.setColorAt(0.18, QColor(c.red(), c.green(), c.blue(), a))
            rg.setColorAt(0.55, QColor(c.red(), c.green(), c.blue(), int(a * 0.28)))
            rg.setColorAt(1.0, QColor(c.red(), c.green(), c.blue(), 0))
            p.setBrush(rg)
            p.drawEllipse(QRectF(cx - rr, cy - rr, rr * 2, rr * 2))

        def seam_rect(rect: QRectF, horizontal: bool, alpha: int) -> None:
            # A narrow luminous seam, placed only in actual gaps. This is not a card border.
            if rect.width() <= 0 or rect.height() <= 0:
                return
            c = acc
            a = int(clamp(alpha * gain, 0, 90))
            if horizontal:
                g = QLinearGradient(
                    rect.left(), rect.center().y(), rect.right(), rect.center().y()
                )
            else:
                g = QLinearGradient(
                    rect.center().x(), rect.top(), rect.center().x(), rect.bottom()
                )
            g.setColorAt(0.0, QColor(c.red(), c.green(), c.blue(), 0))
            g.setColorAt(0.48, QColor(255, 206, 92, a))
            g.setColorAt(1.0, QColor(c.red(), c.green(), c.blue(), 0))
            p.setBrush(g)
            p.drawRoundedRect(rect, min(8, rect.width() / 2), min(8, rect.height() / 2))

        workspace = r.get("workspace")
        command = r.get("command")
        calendar = r.get("calendar")
        pending = r.get("pending")
        diag = r.get("diag")
        updates = r.get("updates")
        insights = r.get("insights")
        quick = r.get("quick")

        # Sidebar/main seam.
        if workspace:
            x = workspace.left() - GAP / 2
            pool(x, workspace.top() + workspace.height() * 0.42, 150, 30)
            seam_rect(
                QRectF(x - 3, workspace.top() + 16, 6, workspace.height() - 32),
                False,
                20,
            )

        # Main vertical seam between the command column and right stack.
        if command and calendar:
            x = command.right() + GAP / 2
            y = command.top() + command.height() * 0.50
            pool(x, y, min(260, command.height() * 0.54), 54)
            seam_rect(
                QRectF(x - 4, command.top() + 18, 8, command.height() - 36), False, 34
            )

        # Command/lower row seam.
        if command and updates:
            y = command.bottom() + GAP / 2
            x = command.left() + command.width() * 0.48
            pool(x, y, min(300, command.width() * 0.28), 44)
            seam_rect(
                QRectF(command.left() + 20, y - 4, command.width() - 40, 8), True, 26
            )

        # Lower row inner seam.
        if updates and insights:
            x = updates.right() + GAP / 2
            pool(x, updates.center().y(), min(150, updates.height() * 0.42), 24)
            seam_rect(
                QRectF(x - 3, updates.top() + 16, 6, updates.height() - 32), False, 16
            )

        # Right stack seams.
        for top, bottom, alpha in [(calendar, pending, 32), (pending, diag, 26)]:
            if top and bottom:
                y = top.bottom() + GAP / 2
                x = top.left() + top.width() * 0.52
                pool(x, y, min(210, top.width() * 0.34), alpha)
                seam_rect(
                    QRectF(top.left() + 18, y - 4, top.width() - 36, 8),
                    True,
                    int(alpha * 0.58),
                )

        # Quick action seam: low, subtle ember under the workspace.
        if quick and updates:
            y = quick.top() - GAP / 2
            x = quick.left() + quick.width() * 0.30
            pool(x, y, min(260, quick.width() * 0.20), 18)

        # Active mission: a contained bloom *behind* the command card. Because cards are
        # opaque, it reads only around gaps and edges. Keep radius modest.
        if command and (
            self.layout_t > 0.2
            or self.store.state in {"processing", "listening", "speaking"}
        ):
            pool(
                command.left() + command.width() * 0.58,
                command.top() + command.height() * 0.36,
                min(310, max(command.width(), command.height()) * 0.36),
                30,
            )

        p.setBrush(Qt.BrushStyle.NoBrush)

    def _draw_sidebar(self, p: QPainter, rect: QRectF) -> None:
        side = QRectF(
            rect.left() + 10, rect.top() + 10, SIDEBAR_W - 20, rect.height() - 20
        )
        self._shadow_card(p, side, 18)
        p.fillPath(self._round_rect_path(side, 18), QColor(255, 255, 253, 252))
        p.setPen(QPen(C_LINE_2, 1))
        p.drawRoundedRect(side, 18, 18)

        logo = QRectF(side.left() + 14, side.top() + 15, 22, 22)
        self._draw_prometheus_mark(p, logo)
        self._draw_text(
            p,
            QRectF(logo.right() + 9, side.top() + 13, side.width() - 38, 26),
            "PROMETHEUS",
            9,
            C_TEXT,
            True,
        )

        for idx, icon, label in NAV_ITEMS:
            nr = self._nav_rect(idx)
            active = self.store.active_tab == idx
            if active:
                p.fillPath(self._round_rect_path(nr, 9), QColor(255, 246, 230, 214))
            p.setPen(QPen(C_AMBER_FAINT if active else QColor(0, 0, 0, 0), 1))
            p.drawRoundedRect(nr, 9, 9)
            self._draw_text(
                p,
                QRectF(nr.left() + 10, nr.top(), 20, nr.height()),
                icon,
                12,
                C_AMBER if active else C_TEXT_3,
                False,
                Qt.AlignmentFlag.AlignCenter,
            )
            self._draw_text(
                p,
                QRectF(nr.left() + 36, nr.top(), nr.width() - 40, nr.height()),
                label,
                10,
                QColor(136, 82, 23, 235) if active else C_TEXT_2,
                active,
            )

        core = QRectF(side.left() + 10, side.bottom() - 214, side.width() - 20, 52)
        p.fillPath(self._round_rect_path(core, 11), QColor(255, 255, 253, 248))
        p.setPen(QPen(C_LINE_2, 1))
        p.drawRoundedRect(core, 11, 11)
        self._draw_text(
            p,
            QRectF(core.left() + 12, core.top() + 8, core.width() - 36, 16),
            "Prometheus Core",
            9,
            C_TEXT,
            True,
        )
        self._draw_text(
            p,
            QRectF(core.left() + 12, core.top() + 26, core.width() - 36, 14),
            "v2.8.1  •  Sentinel",
            8,
            C_TEXT_3,
        )
        dot_c = C_GREEN if self.store.heartbeat_ok else C_RED
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(dot_c)
        p.drawEllipse(QRectF(core.right() - 18, core.top() + 15, 7, 7))
        p.setBrush(Qt.BrushStyle.NoBrush)

        status = QRectF(side.left() + 10, side.bottom() - 148, side.width() - 20, 132)
        p.fillPath(self._round_rect_path(status, 11), QColor(255, 255, 253, 248))
        p.setPen(QPen(C_LINE_2, 1))
        p.drawRoundedRect(status, 11, 11)
        self._draw_text(
            p,
            QRectF(status.left() + 12, status.top() + 11, status.width() - 24, 15),
            "System Status",
            9,
            C_TEXT,
            True,
        )
        self._draw_text(
            p,
            QRectF(status.left() + 12, status.top() + 29, status.width() - 24, 15),
            "OPTIMAL" if self.store.heartbeat_ok else "OFFLINE",
            8,
            C_GREEN if self.store.heartbeat_ok else C_RED,
            True,
        )
        rows = [
            ("Memory", f"{self.stats.mem:.0f}%"),
            ("Response", "118ms"),
            ("Uptime", "live"),
        ]
        y = status.top() + 52
        for label, val in rows:
            self._draw_text(
                p,
                QRectF(status.left() + 12, y, status.width() - 60, 16),
                label,
                9,
                C_TEXT_2,
            )
            self._draw_text(
                p,
                QRectF(status.right() - 52, y, 40, 16),
                val,
                9,
                C_TEXT,
                False,
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            )
            y += 22

    def _draw_prometheus_mark(self, p: QPainter, rect: QRectF) -> None:
        cx, cy = rect.center().x(), rect.center().y()
        r = min(rect.width(), rect.height()) * 0.32
        c = self.accent()
        p.setPen(QPen(QColor(c.red(), c.green(), c.blue(), 190), 1.4))
        p.setBrush(Qt.BrushStyle.NoBrush)
        for i in range(12):
            ang = (i / 12) * math.tau + self.phase * 0.35
            p.drawLine(
                int(cx + math.cos(ang) * r * 1.2),
                int(cy + math.sin(ang) * r * 1.2),
                int(cx + math.cos(ang) * r * 1.75),
                int(cy + math.sin(ang) * r * 1.75),
            )
        rg = QRadialGradient(cx, cy, r * 1.2)
        rg.setColorAt(0.0, QColor(255, 243, 210, 255))
        rg.setColorAt(1.0, QColor(c.red(), c.green(), c.blue(), 210))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(rg)
        p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))
        p.setBrush(Qt.BrushStyle.NoBrush)

    def _draw_header(self, p: QPainter, rect: QRectF) -> None:
        header = QRectF(
            rect.left() + SIDEBAR_W + GAP,
            rect.top() + 12,
            rect.width() - SIDEBAR_W - GAP - 12,
            HEADER_H - 12,
        )
        # Draw card shell with reduced vertical padding for a roomier header bar
        self._shadow_card(p, header, 16)
        _hpath = self._round_rect_path(header, 16)
        _hgrad = QLinearGradient(
            header.left(), header.top(), header.right(), header.bottom()
        )
        _hgrad.setColorAt(0.0, C_CARD)
        _hgrad.setColorAt(1.0, C_CARD_2)
        p.fillPath(_hpath, _hgrad)
        p.setPen(QPen(C_LINE, 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(header, 16, 16)
        body = header.adjusted(20, 12, -20, -12)

        sun = QRectF(body.left(), body.center().y() - 14, 28, 28)
        self._draw_prometheus_mark(p, sun)
        self._draw_text(
            p,
            QRectF(sun.right() + 14, body.top() + 2, 280, 22),
            "Good morning, Alex.",
            12,
            C_TEXT,
            True,
        )
        state_label = {
            "processing": "Prometheus is reasoning through the next move.",
            "speaking": "Prometheus is briefing you now.",
            "listening": "Prometheus is listening.",
            "background_working": "Prometheus is executing in the background.",
        }.get(
            self.store.state,
            "Prometheus is online and quietly orchestrating the workspace.",
        )
        self._draw_text(
            p,
            QRectF(sun.right() + 14, body.top() + 26, 400, 16),
            state_label,
            9,
            C_TEXT_3,
        )

        metrics_x = body.left() + 450
        metric_w = max(114, (body.right() - metrics_x - 142) / 3)
        metrics = [
            (
                "System Load",
                f"{self.stats.cpu:.0f}%",
                self.stats.cpu_hist,
                self.accent(),
            ),
            (
                "Active Threads",
                str(len(self.store.bg_tasks) + len(self.store.agents)),
                None,
                C_TEXT,
            ),
            ("Context Window", "92%", self.stats.mem_hist, C_AMBER),
        ]
        for i, (label, val, hist, col) in enumerate(metrics):
            x = metrics_x + i * metric_w
            p.setPen(QPen(C_LINE_2, 1))
            p.drawLine(
                int(x - 14), int(body.top() + 6), int(x - 14), int(body.bottom() - 6)
            )
            self._draw_text(
                p, QRectF(x, body.top() + 4, metric_w - 16, 14), label, 8, C_TEXT_3
            )
            self._draw_text(
                p, QRectF(x, body.top() + 21, 48, 20), val, 11, C_TEXT, True
            )
            if hist is not None:
                self._sparkline(
                    p,
                    QRectF(x + 54, body.top() + 26, metric_w - 74, 12),
                    list(hist)[-22:],
                    col,
                )

        focus = QRectF(
            header.right() - 138, header.top() + (header.height() - 32) / 2, 104, 32
        )
        self._pill(p, focus, "Focus Mode", False, "◉")

        rbtn = self._restart_btn_rect()
        p.fillPath(self._round_rect_path(rbtn, 8), QColor(255, 252, 245, 240))
        p.setPen(QPen(C_LINE_2, 1))
        p.drawRoundedRect(rbtn, 8, 8)
        self._draw_text(p, rbtn, "↺", 11, C_TEXT_2, False, Qt.AlignmentFlag.AlignCenter)

    # ── Layout ───────────────────────────────────────────────────────────────

    def compute_target_layout(self) -> dict[str, QRectF]:
        outer = self._outer_rect()
        workspace = QRectF(
            outer.left() + SIDEBAR_W + GAP,
            outer.top() + HEADER_H + GAP,
            outer.width() - SIDEBAR_W - GAP - 12,
            outer.height() - HEADER_H - BOTTOM_H - GAP * 2,
        )
        # Full-width quick actions strip below workspace
        quick = QRectF(
            workspace.left(),
            workspace.bottom() + GAP,
            workspace.width(),
            BOTTOM_H - 8,
        )

        left_w = lerp(workspace.width() * 0.54, workspace.width() * 0.58, self.layout_t)
        right_w = workspace.width() - left_w - GAP
        top_h = lerp(
            workspace.height() * 0.63, workspace.height() * 0.66, self.layout_t
        )
        lower_h = workspace.height() - top_h - GAP

        command = QRectF(workspace.left(), workspace.top(), left_w, top_h)

        cal_h = workspace.height() * 0.37
        pending_h = workspace.height() * 0.31
        cal = QRectF(command.right() + GAP, workspace.top(), right_w, cal_h)
        pending = QRectF(command.right() + GAP, cal.bottom() + GAP, right_w, pending_h)
        diag = QRectF(
            command.right() + GAP,
            pending.bottom() + GAP,
            right_w,
            workspace.bottom() - pending.bottom() - GAP,
        )

        ku_w = left_w * 0.48
        ku = QRectF(workspace.left(), command.bottom() + GAP, ku_w, lower_h)
        insights = QRectF(
            ku.right() + GAP, command.bottom() + GAP, left_w - ku_w - GAP, lower_h
        )

        return {
            "workspace": workspace,
            "command": command,
            "calendar": cal,
            "pending": pending,
            "diag": diag,
            "updates": ku,
            "insights": insights,
            "quick": quick,
        }

    # ── Dashboard cards ──────────────────────────────────────────────────────

    def _draw_command_card(self, p: QPainter, rect: QRectF) -> None:
        mission = (
            self._active_mission_text()
            or "Maintain system awareness and wait for your next command."
        )
        next_action = (
            self._next_action_text()
            or "Review recent activity and surface the next useful move."
        )
        active = self.layout_t > 0.15 or self.store.state in {
            "processing",
            "speaking",
            "background_working",
        }
        body = self._card(
            p,
            rect,
            "Command Center",
            "Prometheus is arranging the workspace around the current mission.",
            "✧",
            active,
        )

        # Proportional text region — avoids hardcoded pixel offsets that clip content
        text_h = max(94, body.height() * 0.27)
        chips_reserve = 50
        panel_h = max(90, body.height() - text_h - chips_reserve)
        panel_y = body.top() + text_h

        headline = (
            "Preparing your active mission."
            if self._active_mission_text()
            else "Prometheus is standing by."
        )
        self._draw_text(
            p,
            QRectF(body.left(), body.top() + 2, body.width(), 28),
            headline,
            15,
            C_TEXT,
            True,
        )
        self._whisper_text(
            p,
            "mission-summary",
            QRectF(body.left(), body.top() + 34, body.width() - 10, text_h - 38),
            f"{mission}  Next: {next_action}",
            10,
            C_TEXT_2,
            max_lines=2,
            speed=110,
        )

        left = QRectF(body.left(), panel_y, body.width() * 0.57, panel_h)
        right = QRectF(
            left.right() + 16, panel_y, body.right() - left.right() - 16, panel_h
        )

        for sub, title in [(left, "Current Mission"), (right, "Next Up")]:
            p.fillPath(self._round_rect_path(sub, 12), QColor(252, 252, 250, 245))
            p.setPen(QPen(C_LINE_2, 1))
            p.drawRoundedRect(sub, 12, 12)
            self._draw_text(
                p,
                QRectF(sub.left() + 14, sub.top() + 12, sub.width() - 28, 15),
                title,
                8,
                C_AMBER,
                True,
            )

        # Mission checklist — clipped to sub-panel
        p.save()
        p.setClipRect(left)
        subtasks = (
            self.store.mission.get("subtasks")
            if isinstance(self.store.mission, dict)
            else None
        )
        completed = (
            self.store.mission.get("completed_subtasks")
            if isinstance(self.store.mission, dict)
            else None
        )
        if not isinstance(subtasks, list) or not subtasks:
            subtasks = [
                {"description": "Analyze recent activity"},
                {"description": "Assemble relevant context"},
                {"description": "Draft next action"},
                {"description": "Prepare summary"},
            ]
        if not isinstance(completed, list):
            completed = []
        total = max(1, len(subtasks) + len(completed))
        done = len(completed)
        bar = QRectF(left.left() + 14, left.top() + 38, left.width() - 28, 5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(232, 222, 205, 180))
        p.drawRoundedRect(bar, 2, 2)
        p.setBrush(C_AMBER)
        p.drawRoundedRect(
            QRectF(
                bar.left(), bar.top(), bar.width() * clamp(done / total), bar.height()
            ),
            2,
            2,
        )
        self._draw_text(
            p,
            QRectF(left.left() + 14, left.top() + 48, left.width() - 28, 14),
            f"{done} of {total} complete",
            8,
            C_TEXT_3,
        )
        y = left.top() + 72
        items = completed[:2] + subtasks[:4]
        if not items:
            items = [{"description": "Awaiting mission context"}]
        row_h = min(26, max(18, (left.bottom() - y - 8) / max(1, len(items[:6]))))
        for i, item in enumerate(items[:6]):
            if y + row_h > left.bottom() - 6:
                break
            is_done = i < min(2, len(completed))
            desc = item.get("description") if isinstance(item, dict) else str(item)
            desc = str(desc or "Task")
            color = (
                C_GREEN
                if is_done
                else (C_AMBER if i == min(2, len(completed)) else C_TEXT_3)
            )
            symbol = "✓" if is_done else ("◔" if i == min(2, len(completed)) else "○")
            self._draw_text(
                p,
                QRectF(left.left() + 14, y, 20, row_h),
                symbol,
                9,
                color,
                True,
                Qt.AlignmentFlag.AlignCenter,
            )
            elided = self._elide(desc, left.width() - 52, 8)
            self._draw_text(
                p,
                QRectF(left.left() + 40, y, left.width() - 54, row_h),
                elided,
                8,
                C_TEXT if not is_done else C_TEXT_2,
                i == min(2, len(completed)),
            )
            y += row_h
        # Calm ambient placeholder when checklist is sparse
        if y < left.bottom() - 54 and len(items) < 5:
            self._whisper_text(
                p,
                "cmd-ambient",
                QRectF(left.left() + 14, y + 8, left.width() - 28, 52),
                "Prometheus is monitoring context, recent activity, and the next useful move.",
                8,
                C_TEXT_3,
                max_lines=3,
                speed=55,
            )
        p.restore()

        # Next Up — clipped to sub-panel
        p.save()
        p.setClipRect(right)
        elided_next = self._elide(next_action, right.width() - 28, 10, True)
        self._draw_text(
            p,
            QRectF(right.left() + 14, right.top() + 38, right.width() - 28, 20),
            elided_next,
            10,
            C_TEXT,
            True,
        )
        self._draw_text(
            p,
            QRectF(right.left() + 14, right.top() + 62, right.width() - 28, 15),
            "Est. 12 min",
            8,
            C_TEXT_3,
        )
        wave_rect = QRectF(
            right.left() + 12, right.bottom() - 74, right.width() - 24, 54
        )
        if wave_rect.top() > right.top() + 90:
            self._draw_wave_mesh(p, wave_rect)
        p.restore()

        # Action chips above input
        chip_y = rect.bottom() - 48
        x = body.left()
        for label, icon in [
            ("Summarize", "□"),
            ("Prepare", "✧"),
            ("Analyze", "◇"),
            ("Draft", "✉"),
            ("More", "⌄"),
        ]:
            w = 90 if label != "More" else 64
            self._pill(p, QRectF(x, chip_y, w, 27), label, False, icon)
            x += w + 7

    def _draw_wave_mesh(self, p: QPainter, rect: QRectF) -> None:
        p.save()
        p.setClipRect(rect)
        for i in range(7):
            path = QPainterPath()
            y = rect.center().y() + math.sin(self.phase * 1.2 + i) * 7 + (i - 3) * 4
            path.moveTo(rect.left(), y)
            path.cubicTo(
                rect.left() + rect.width() * 0.28,
                y - 18,
                rect.left() + rect.width() * 0.58,
                y + 20,
                rect.right(),
                y - 5,
            )
            p.setPen(QPen(QColor(230, 154, 38, 30 + i * 9), 1.0))
            p.drawPath(path)
        p.restore()

    def _draw_calendar_card(self, p: QPainter, rect: QRectF) -> None:
        body = self._card(
            p,
            rect,
            "Calendar Intelligence",
            "Optimized for impact.",
            "☼",
            self.layout_t > 0.4,
        )
        p.save()
        p.setClipRect(body)
        self._draw_text(
            p,
            QRectF(body.right() - 148, body.top() - 32, 136, 20),
            datetime.datetime.now().strftime("%b %d, %Y"),
            9,
            C_TEXT,
            True,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
        )
        sched = [
            ("9:00 AM", "Deep Work Block", "Focus time • 90 min", C_BLUE),
            ("10:00 AM", "Strategy Meeting", "Executive Sync • 60 min", C_AMBER),
            ("11:30 AM", "Client Briefing", "Acme Corp • 60 min", C_AMBER),
            ("1:00 PM", "Product Review", "Roadmap • 45 min", C_BLUE),
        ]
        list_w = body.width() * 0.68
        row_h = max(28, (body.height() - 8) / max(1, len(sched)))
        y = body.top() + 2
        for i, (tm, name, sub, color) in enumerate(sched):
            rr = QRectF(body.left(), y, list_w, row_h - 3)
            if y + row_h > body.bottom():
                break
            if i == 1:
                p.fillPath(self._round_rect_path(rr, 9), QColor(255, 243, 224, 230))
            self._draw_text(
                p, QRectF(rr.left() + 8, rr.top(), 62, rr.height()), tm, 8, C_TEXT_2
            )
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            p.drawEllipse(QRectF(rr.left() + 76, rr.center().y() - 3, 6, 6))
            name_elided = self._elide(name, rr.width() - 100, 8, True)
            self._draw_text(
                p,
                QRectF(rr.left() + 92, rr.top() + 2, rr.width() - 104, 15),
                name_elided,
                8,
                C_TEXT,
                True,
            )
            self._draw_text(
                p,
                QRectF(rr.left() + 92, rr.top() + 17, rr.width() - 104, 13),
                sub,
                7,
                C_TEXT_3,
            )
            y += row_h

        gauge = QRectF(
            body.left() + list_w + 14,
            body.top() + 4,
            body.right() - body.left() - list_w - 18,
            body.height() - 8,
        )
        self._draw_day_score(p, gauge)
        p.restore()

    def _draw_day_score(self, p: QPainter, rect: QRectF) -> None:
        cx = rect.center().x()
        cy = rect.top() + rect.height() * 0.42
        r = min(rect.width(), rect.height()) * 0.27
        p.setPen(QPen(QColor(238, 220, 192, 230), 4))
        p.drawArc(QRectF(cx - r, cy - r, r * 2, r * 2), 30 * 16, 300 * 16)
        p.setPen(QPen(C_AMBER, 4))
        p.drawArc(QRectF(cx - r, cy - r, r * 2, r * 2), 30 * 16, int(300 * 0.82 * 16))
        self._draw_text(
            p,
            QRectF(cx - 42, cy - 18, 84, 32),
            "82",
            22,
            C_TEXT,
            False,
            Qt.AlignmentFlag.AlignCenter,
        )
        self._draw_text(
            p,
            QRectF(cx - 42, cy + 12, 84, 14),
            "Day Score",
            7,
            C_TEXT_3,
            False,
            Qt.AlignmentFlag.AlignCenter,
        )
        self._draw_text(
            p,
            QRectF(rect.left(), cy + r + 10, rect.width(), 18),
            "Optimal ↑",
            8,
            C_GREEN,
            True,
            Qt.AlignmentFlag.AlignCenter,
        )
        pill_r = QRectF(rect.center().x() - 52, rect.bottom() - 30, 104, 27)
        if pill_r.top() > cy + r + 28:
            self._pill(p, pill_r, "Optimize Day", False, "↗")

    def _draw_pending_card(self, p: QPainter, rect: QRectF) -> None:
        body = self._card(p, rect, "Attention & Pending", "5", "✦", False)
        p.save()
        p.setClipRect(body)
        items = [
            (
                "Approve Q2 Budget Forecast",
                "Finance • Awaiting approval",
                "High",
                "2h",
                C_RED,
            ),
            (
                "Review Legal Agreement",
                "Legal • Signature required",
                "High",
                "4h",
                C_RED,
            ),
            ("Team Stand-up Follow-up", "Alex • Action items", "Med", "6h", C_AMBER),
            ("System Update Available", "Prometheus Core v2.8.1", "Low", "1d", C_GREEN),
        ]
        row_h = max(26, body.height() / max(1, len(items)))
        y = body.top()
        for title, sub, prio, age, color in items:
            if y + row_h > body.bottom() + 4:
                break
            title_elided = self._elide(title, body.width() - 116, 8, True)
            self._draw_text(
                p,
                QRectF(body.left() + 6, y + 2, body.width() - 118, 15),
                title_elided,
                8,
                C_TEXT,
                True,
            )
            self._draw_text(
                p,
                QRectF(body.left() + 6, y + 17, body.width() - 118, 13),
                sub,
                7,
                C_TEXT_3,
            )
            self._draw_text(
                p,
                QRectF(body.right() - 106, y + 4, 50, 15),
                prio,
                7,
                color,
                True,
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            )
            self._draw_text(
                p,
                QRectF(body.right() - 46, y + 4, 34, 15),
                age,
                7,
                C_TEXT_3,
                False,
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            )
            p.setPen(QPen(C_LINE_2, 1))
            p.drawLine(
                int(body.left()),
                int(y + row_h - 2),
                int(body.right()),
                int(y + row_h - 2),
            )
            y += row_h
        p.restore()

    def _draw_updates_card(self, p: QPainter, rect: QRectF) -> None:
        body = self._card(p, rect, "Key Updates", "Today", "✩", False)
        p.save()
        p.setClipRect(body)
        lines = (
            self.store.lines[-3:]
            if self.store.lines
            else [
                "8:30 AM  Markets up 1.2% this morning",
                "7:45 AM  Project milestone completed",
                "6:15 AM  Client feedback — overall positive",
            ]
        )
        y = body.top() + 2
        row_h = max(26, body.height() / 3)
        for line in lines[-3:]:
            if y + row_h > body.bottom() + 2:
                break
            elided = self._elide(line, body.width() - 8, 8)
            self._draw_text(
                p,
                QRectF(body.left() + 2, y, body.width() - 4, 18),
                elided,
                8,
                C_TEXT_2,
            )
            p.setPen(QPen(C_LINE_2, 1))
            p.drawLine(int(body.left()), int(y + 24), int(body.right()), int(y + 24))
            y += row_h
        self._draw_text(
            p,
            QRectF(body.left(), body.bottom() - 18, body.width(), 16),
            "View all updates →",
            8,
            C_TEXT_2,
            False,
            Qt.AlignmentFlag.AlignCenter,
        )
        p.restore()

    def _draw_insights_card(self, p: QPainter, rect: QRectF) -> None:
        body = self._card(
            p, rect, "Insights for You", "", "☼", self.store.state == "processing"
        )
        p.save()
        p.setClipRect(body)
        insights = [
            ("Focus Opportunity", "You have 2.5 hrs of deep work available today."),
            ("Meeting Optimization", "Move Product Review to 2 PM for better flow."),
            ("Energy Forecast", "Your focus peaks between 9–11 AM."),
        ]
        y = body.top() + 2
        row_h = max(38, body.height() / max(1, len(insights)))
        for i, (title, desc) in enumerate(insights):
            if y + row_h > body.bottom() + 4:
                break
            self._draw_text(
                p,
                QRectF(body.left() + 2, y, body.width() - 4, 15),
                f"✧  {title}",
                8,
                C_AMBER,
                True,
            )
            self._whisper_text(
                p,
                f"insight-{i}",
                QRectF(body.left() + 20, y + 17, body.width() - 24, 18),
                desc,
                7,
                C_TEXT_3,
                1,
                120,
            )
            y += row_h
        p.restore()

    def _draw_diagnostics_card(self, p: QPainter, rect: QRectF) -> None:
        body = self._card(
            p, rect, "System Diagnostics", "All Systems Nominal", "⌁", False
        )
        p.save()
        p.setClipRect(body)
        metrics = [
            ("CPU", f"{self.stats.cpu:.0f}%", self.stats.cpu_hist, C_GREEN),
            ("Memory", f"{self.stats.mem:.0f}%", self.stats.mem_hist, C_GREEN),
            ("Context", "92%", self._down_hist_proxy(), C_GREEN),
            ("Vector DB", "100%", self._up_hist_proxy(), C_GREEN),
        ]
        col_w = body.width() / 4
        for i, (label, val, hist, color) in enumerate(metrics):
            x = body.left() + i * col_w
            self._draw_text(
                p, QRectF(x, body.top() + 2, col_w - 8, 14), label, 8, C_TEXT_2, True
            )
            self._draw_text(p, QRectF(x, body.top() + 18, 44, 18), val, 11, C_TEXT)
            self._sparkline(
                p,
                QRectF(x + 50, body.top() + 22, col_w - 62, 13),
                list(hist)[-24:],
                color,
            )
            if i > 0:
                p.setPen(QPen(C_LINE_2, 1))
                p.drawLine(
                    int(x - 7), int(body.top() + 4), int(x - 7), int(body.top() + 44)
                )

        if body.height() > 56:
            log_y = body.top() + 52
            p.setPen(QPen(C_LINE_2, 1))
            p.drawLine(
                int(body.left()), int(log_y - 5), int(body.right()), int(log_y - 5)
            )
            recent = (
                self.store.lines[-1]
                if self.store.lines
                else "9:41 AM  Core reasoning engine initialized"
            )
            elided = self._elide(recent, body.width() - 8, 8)
            self._draw_text(
                p,
                QRectF(body.left() + 4, log_y, body.width() - 8, 17),
                elided,
                8,
                C_TEXT_2,
            )

        if body.height() > 72:
            self._draw_text(
                p,
                QRectF(body.left(), body.bottom() - 17, body.width(), 15),
                "Open diagnostics →",
                8,
                C_TEXT_3,
                False,
                Qt.AlignmentFlag.AlignCenter,
            )
        p.restore()

    def _down_hist_proxy(self):
        return deque(
            [92 + math.sin(i * 0.3 + self.phase) * 4 for i in range(96)], maxlen=96
        )

    def _up_hist_proxy(self):
        return deque(
            [98 + math.sin(i * 0.27 + self.phase) * 2 for i in range(96)], maxlen=96
        )

    def _draw_quick_actions(self, p: QPainter, rect: QRectF) -> None:
        body = self._card(p, rect)
        p.save()
        p.setClipRect(body)
        self._draw_text(
            p,
            QRectF(body.left() + 46, body.top() + 1, 150, 18),
            "Quick Actions",
            10,
            C_TEXT,
            True,
        )
        self._draw_text(
            p,
            QRectF(body.left() + 46, body.top() + 21, 200, 14),
            "Common actions and workflows.",
            7,
            C_TEXT_3,
        )
        self._draw_prometheus_mark(p, QRectF(body.left(), body.top() + 2, 34, 34))
        x = body.left() + 230
        actions = [
            "Daily Briefing",
            "Smart Summary",
            "Data Analysis",
            "Content Draft",
            "Task Planner",
            "Learning Coach",
            "+ Add Custom",
        ]
        for label in actions:
            w = 118 if label != "+ Add Custom" else 108
            if x + w > body.right() - 8:
                break
            self._pill(p, QRectF(x, body.top() + 7, w, 27), label)
            x += w + 10
        p.restore()

    # ── Tab pages ────────────────────────────────────────────────────────────

    def _draw_simple_page(
        self, p: QPainter, rect: QRectF, title: str, rows: list[str]
    ) -> None:
        body = self._card(p, rect, title, "Live Prometheus data stream.", "✧", False)
        p.save()
        p.setClipRect(body)
        y = body.top() + 4
        if not rows:
            rows = ["No data available."]
        for row in rows[:22]:
            self._draw_text(
                p,
                QRectF(body.left() + 6, y, body.width() - 12, 20),
                row[:160],
                9,
                C_TEXT_2,
            )
            p.setPen(QPen(C_LINE_2, 1))
            p.drawLine(int(body.left()), int(y + 25), int(body.right()), int(y + 25))
            y += 30
            if y > body.bottom() - 10:
                break
        p.restore()

    # ── Paint ────────────────────────────────────────────────────────────────

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self._outer_rect()
        self._draw_living_background(p, rect)
        self._draw_sidebar(p, rect)
        self._draw_header(p, rect)

        content = QRectF(
            rect.left() + SIDEBAR_W + GAP,
            rect.top() + HEADER_H + GAP,
            rect.width() - SIDEBAR_W - GAP - 12,
            rect.height() - HEADER_H - GAP - 12,
        )

        if self.store.active_tab == 0:
            self._position_chat_input()
            r = self.tile_rects
            if not r:
                r = self.compute_target_layout()
            self._draw_gap_glows(p)
            self._draw_command_card(p, r["command"])
            self._draw_calendar_card(p, r["calendar"])
            self._draw_pending_card(p, r["pending"])
            self._draw_updates_card(p, r["updates"])
            self._draw_insights_card(p, r["insights"])
            self._draw_diagnostics_card(p, r["diag"])
            self._draw_quick_actions(p, r["quick"])
        else:
            self._chat_input.hide()
            page = content.adjusted(0, 0, 0, -BOTTOM_H)
            tab = self.store.active_tab
            if tab == 1:
                rows = [
                    f"{m.get('role', 'assistant')}: {m.get('text', '')}"
                    for m in self.store.chat_history[-22:]
                ]
                self._draw_simple_page(p, page, "Intelligence", rows)
            elif tab == 2:
                self._draw_simple_page(p, page, "Knowledge", self.store.lines[-22:])
            elif tab == 3:
                rows = [
                    str(t.get("intent") or t.get("description") or t)
                    for t in self.store.bg_tasks
                    if isinstance(t, dict)
                ]
                self._draw_simple_page(p, page, "Operations", rows)
            elif tab == 4:
                self._draw_simple_page(
                    p,
                    page,
                    "Calendar",
                    [
                        "Calendar wiring placeholder — connect Prometheus schedule source here."
                    ],
                )
            elif tab == 5:
                rows = [
                    f"CPU {self.stats.cpu:.0f}%",
                    f"Memory {self.stats.mem:.0f}%",
                    f"Net ↓ {self.stats.net_down_kbps:.0f} KB/s",
                    f"Net ↑ {self.stats.net_up_kbps:.0f} KB/s",
                ]
                self._draw_simple_page(p, page, "Systems", rows)
            elif tab == 6:
                rows = [str(a) for a in self.store.agents] or ["No agents registered."]
                self._draw_simple_page(p, page, "Integrations", rows)
            else:
                self._draw_simple_page(
                    p,
                    page,
                    "Settings",
                    [
                        "HUD theme: Prometheus light source",
                        "Motion: adaptive tiles",
                        "Content reveal: whisper mode",
                    ],
                )


class App:
    def __init__(self) -> None:
        print("PROMETHEUS Light HUD (production pass) launched")
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(True)
        self.store = Store()
        self.stats = SystemStats()
        self.win = HUDWindow(self.store, self.stats)

        self.data_timer = QTimer()
        self.data_timer.timeout.connect(self.refresh)
        self.data_timer.start(120)

        self.stats_timer = QTimer()
        self.stats_timer.timeout.connect(self.stats.refresh)
        self.stats_timer.start(1000)

    def refresh(self) -> None:
        self.store.refresh()
        if DEMO:
            self._inject_demo_data()
        self.win.update()

    def _inject_demo_data(self) -> None:
        self.store.state = "processing"
        self.store.heartbeat_ok = True
        self.store.active_project = "Prometheus"
        self.store.active_window = "VS Code — Prometheus"
        if not self.store.lines:
            self.store.lines = [
                "09:12:33  Realtime API connected",
                "09:12:35  Prometheus online",
                "09:14:07  Project: Prometheus",
                "09:16:22  Listening — PTT",
                "09:16:28  “Schedule team standup tomorrow at 10”",
                "09:16:29  calendar_create_flow",
                "09:16:30  Done. 'Team Standup' added to your calendar.",
            ]
        if not self.store.mission:
            self.store.mission = {
                "current_mission": (
                    "Build and ship the Prometheus Light HUD production pass. "
                    "Goal: premium executive command dashboard with living amber intelligence."
                ),
                "next_action": "Write and validate prometheus_desktop_hud_light.py",
                "subtasks": [
                    {"description": "Implement tile interpolation system"},
                    {"description": "Build gap glow rendering"},
                    {"description": "Ship production-quality card layout"},
                ],
                "completed_subtasks": [
                    {"description": "Design Light Source Concept"},
                    {"description": "Define color palette and constants"},
                ],
            }
        if not self.store.bg_tasks:
            self.store.bg_tasks = [
                {
                    "intent": "Compile and test HUD",
                    "status": "running",
                    "started_at": "09:00:00",
                },
            ]

    def run(self) -> int:
        self.win.show()
        self.win.activateWindow()
        return self.app.exec()


if __name__ == "__main__":
    raise SystemExit(App().run())

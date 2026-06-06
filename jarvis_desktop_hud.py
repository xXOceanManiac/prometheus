from __future__ import annotations

import datetime as dt
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QRadialGradient
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

try:
    import psutil
except Exception:
    psutil = None


# ─────────────────────────────────────────────────────────────────────────────
# PROMETHEUS HUD — Stable Polished Dashboard
# Target filename: jarvis_desktop_hud.py
#
# Design goals:
# - Normal WM-managed window. No transparency, no always-on-top, no auto-relaunch.
# - Readable fonts.
# - Scrollable content that does not snap back every refresh.
# - Visual metric graphs instead of tiny % readouts.
# - FireCore card that controls/launches the real FireCore project, without fragile
#   window embedding.
# - Writes ~/.prometheus/fire_state.json so the current Godot FireCore reacts.
# ─────────────────────────────────────────────────────────────────────────────

STATE_FILE = Path.home() / ".jarvis" / "visual_state.json"
AUDIO_FILE = Path.home() / ".jarvis" / "audio_levels.json"
TASKS_FILE = Path.home() / ".jarvis" / "background_tasks.json"
AGENTS_FILE = Path.home() / ".jarvis" / "agents.json"
HEARTBEAT_FILE = Path.home() / ".jarvis" / "heartbeat.json"
WORKING_MEMORY_FILE = Path.home() / ".jarvis" / "memory_v2" / "working_memory.json"
MISSION_FILE = Path.home() / ".jarvis" / "memory_v2" / "mission_state.json"

LOG_GLOBS = [
    Path.home() / ".jarvis" / "logs" / "*.jsonl",
    Path.home() / "PROMETHEUS" / "logs" / "*.jsonl",
    Path.home() / "Desktop" / "PROMETHEUS" / "Prometheus_Main" / "logs" / "*.jsonl",
    Path.home() / ".prometheus" / "logs" / "*.jsonl",
]

FIRECORE_DIR = Path("/home/tatel/Desktop/PROMETHEUS/FireCore")
FIRECORE_STATE_FILE = Path.home() / ".prometheus" / "fire_state.json"

REFRESH_MS = 2500
METRIC_MS = 1000
MAX_ACTIVITY_LINES = 140
MAX_BLOCK_CHARS = 2400

STATE_COLORS = {
    "idle": "#D0A644",
    "armed": "#D0A644",
    "listening": "#F0BC55",
    "processing": "#6FA9D6",
    "thinking": "#6FA9D6",
    "reasoning": "#6FA9D6",
    "speaking": "#F09A35",
    "background_working": "#3CC89E",
    "executing": "#3CC89E",
    "tool_running": "#3CC89E",
    "warning": "#DA4848",
    "error": "#DA4848",
    "offline": "#DA4848",
}


def read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def one_line(value: Any, fallback: str = "—") -> str:
    if value is None:
        return fallback
    text = str(value).replace("\n", " ").strip()
    return text if text else fallback


def compact_json(value: Any, max_chars: int = MAX_BLOCK_CHARS) -> str:
    try:
        if isinstance(value, (dict, list)):
            text = json.dumps(value, indent=2, ensure_ascii=False)
        else:
            text = str(value)
    except Exception:
        text = repr(value)
    return text[:max_chars] + ("\n…" if len(text) > max_chars else "")


def iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def elapsed(ts: str) -> str:
    try:
        stamp = dt.datetime.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S")
        secs = int(max(0, (dt.datetime.now() - stamp).total_seconds()))
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h {(secs % 3600) // 60}m ago"
        return f"{secs // 86400}d ago"
    except Exception:
        return "—"


def normalize_state(state: str) -> str:
    s = str(state or "").lower().strip()
    if s == "listening":
        return "listening"
    if s in {"processing", "thinking", "reasoning"}:
        return "processing"
    if s == "speaking":
        return "speaking"
    if s in {"background_working", "executing", "tool_running"}:
        return "executing"
    if s in {"warning", "error", "offline"}:
        return "warning"
    return "idle"


def accent_for(raw_state: str) -> str:
    return STATE_COLORS.get(
        str(raw_state or "").lower().strip(),
        STATE_COLORS.get(normalize_state(raw_state), "#D0A644"),
    )


def firecore_values(state: str) -> dict[str, float]:
    return {
        "idle": {"intensity": 0.55, "rotation_speed": 0.55, "spark_rate": 0.20},
        "listening": {"intensity": 0.68, "rotation_speed": 0.65, "spark_rate": 0.18},
        "processing": {"intensity": 0.90, "rotation_speed": 1.10, "spark_rate": 0.35},
        "speaking": {"intensity": 0.78, "rotation_speed": 0.85, "spark_rate": 0.25},
        "executing": {"intensity": 1.00, "rotation_speed": 1.30, "spark_rate": 0.50},
        "warning": {"intensity": 0.95, "rotation_speed": 1.05, "spark_rate": 0.55},
    }.get(state, {"intensity": 0.55, "rotation_speed": 0.55, "spark_rate": 0.20})


def latest_log_file() -> Path | None:
    files: list[Path] = []
    for pattern in LOG_GLOBS:
        try:
            files.extend(pattern.parent.glob(pattern.name))
        except Exception:
            pass
    files = [p for p in files if p.exists()]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def format_log_event(rec: dict[str, Any]) -> str | None:
    kind = str(rec.get("kind", rec.get("event", "")))
    ts = str(rec.get("ts", rec.get("time", "")))
    clock = ts[11:19] if len(ts) >= 19 else ts[:8]

    if kind in {"ptt_turn_started", "wakeword_turn_started"}:
        return (
            f"{clock}  LISTEN  {'PTT' if kind == 'ptt_turn_started' else 'wake word'}"
        )
    if kind in {"transcript", "user_transcript"}:
        text = one_line(rec.get("transcript") or rec.get("text"), "")
        return f"{clock}  USER    {text}" if text else None
    if kind in {"tool_call_received", "tool_execute", "direct_tool_override"}:
        args = rec.get("args") or rec.get("payload") or {}
        if not isinstance(args, dict):
            args = {}
        action = one_line(args.get("action") or kind)
        detail = one_line(
            args.get("app") or args.get("script_name") or args.get("query"), ""
        )
        return f"{clock}  TOOL    {action}{(' — ' + detail) if detail else ''}"
    if kind in {"background_task_submitted", "background_task_done"}:
        status = "DONE" if kind == "background_task_done" else "QUEUE"
        desc = one_line(rec.get("description") or rec.get("intent") or rec.get("task"))
        return f"{clock}  TASK    {status} — {desc}"
    if kind in {"realtime_connected", "prometheus_started"}:
        return f"{clock}  CORE    {kind.replace('_', ' ').title()}"
    if kind in {"workspace_project_changed", "workspace_changed"}:
        name = one_line(rec.get("name") or rec.get("to") or rec.get("project"))
        return f"{clock}  WORK    {name}"
    if kind == "visual_state":
        state = one_line(rec.get("state"))
        return f"{clock}  STATE   {state}"
    if "error" in kind or "closed" in kind or "failed" in kind:
        err = one_line(rec.get("error") or rec.get("message") or kind)
        return f"{clock}  ISSUE   {err}"
    return None


def read_activity_lines() -> list[str]:
    path = latest_log_file()
    if path is None:
        return []
    lines: list[str] = []
    try:
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines()[
            -1000:
        ]:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except Exception:
                continue
            line = format_log_event(rec)
            if line:
                lines.append(line[:340])
    except Exception:
        return []
    return lines[-MAX_ACTIVITY_LINES:]


class MetricGraph(QFrame):
    def __init__(self, title: str, suffix: str = "%", max_points: int = 90):
        super().__init__()
        self.setObjectName("MetricGraph")
        self.title = title
        self.suffix = suffix
        self.max_points = max_points
        self.values: list[float] = []
        self.current = 0.0
        self.accent = "#D0A644"
        self.setMinimumHeight(76)

    def push(self, value: float, accent: str) -> None:
        self.current = float(value)
        self.accent = accent
        self.values.append(self.current)
        self.values = self.values[-self.max_points :]
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)

        card = QPainterPath()
        card.addRoundedRect(rect, 14, 14)
        p.fillPath(card, QColor("#101319"))
        p.setPen(QPen(QColor("#252B35"), 1))
        p.drawRoundedRect(rect, 14, 14)

        p.setFont(QFont("Inter", 13, QFont.Weight.Bold))
        p.setPen(QColor("#EFE8DB"))
        p.drawText(
            QRectF(14, 10, rect.width() * 0.55, 28),
            Qt.AlignmentFlag.AlignLeft,
            self.title,
        )

        p.setFont(QFont("Inter", 20, QFont.Weight.Bold))
        p.setPen(QColor(self.accent))
        value = f"{self.current:.0f}{self.suffix}"
        p.drawText(
            QRectF(rect.width() * 0.52, 7, rect.width() * 0.43, 34),
            Qt.AlignmentFlag.AlignRight,
            value,
        )

        graph = QRectF(14, 50, rect.width() - 28, max(1.0, rect.height() - 64))
        p.setPen(QPen(QColor("#222832"), 1))
        for frac in (0.0, 0.5, 1.0):
            y = graph.top() + graph.height() * frac
            p.drawLine(QPointF(graph.left(), y), QPointF(graph.right(), y))

        if len(self.values) < 2:
            return

        max_v = 100.0 if self.suffix == "%" else max(max(self.values), 100.0)
        dx = graph.width() / max(1, self.max_points - 1)
        visible = self.values[-self.max_points :]
        start_x = graph.right() - dx * (len(visible) - 1)

        points: list[QPointF] = []
        for i, val in enumerate(visible):
            norm = max(0.0, min(1.0, val / max_v))
            points.append(
                QPointF(start_x + i * dx, graph.bottom() - norm * graph.height())
            )

        p.setPen(QPen(QColor(self.accent), 2.25))
        for a, b in zip(points, points[1:]):
            p.drawLine(a, b)

        p.setBrush(QColor(self.accent))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(points[-1], 3.7, 3.7)


class MetricPanel(QFrame):
    """Non-scrolling graph panel. All metric graphs stay visible."""

    def __init__(self):
        super().__init__()
        self.setObjectName("Card")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(6)

        self.title = QLabel("Metric Graphs")
        self.title.setObjectName("CardTitle")
        root.addWidget(self.title)

        self.cpu_graph = MetricGraph("CPU")
        self.mem_graph = MetricGraph("Memory")
        self.disk_graph = MetricGraph("Disk")
        self.mic_graph = MetricGraph("Mic", suffix="")
        self.spk_graph = MetricGraph("Speaker", suffix="")

        for graph in (
            self.cpu_graph,
            self.mem_graph,
            self.disk_graph,
            self.mic_graph,
            self.spk_graph,
        ):
            graph.setMinimumHeight(76)
            graph.setMaximumHeight(88)
            root.addWidget(graph)


class ScrollCard(QFrame):
    def __init__(self, title: str):
        super().__init__()
        self.setObjectName("Card")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._last_key = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        self.title = QLabel(title)
        self.title.setObjectName("CardTitle")
        root.addWidget(self.title)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        self.body_widget = QWidget()
        self.body_widget.setObjectName("CardBody")
        self.body = QVBoxLayout(self.body_widget)
        self.body.setContentsMargins(0, 0, 6, 0)
        self.body.setSpacing(9)
        self.body.addStretch(1)

        self.scroll.setWidget(self.body_widget)
        root.addWidget(self.scroll, 1)

    def clear(self) -> None:
        while self.body.count() > 0:
            item = self.body.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.body.addStretch(1)

    def update_if_changed(self, key: str, render_fn: Callable[[], None]) -> None:
        if key == self._last_key:
            return
        bar = self.scroll.verticalScrollBar()
        old_pos = bar.value()
        was_bottom = old_pos >= max(0, bar.maximum() - 12)
        self.clear()
        render_fn()
        self._last_key = key
        QTimer.singleShot(
            0,
            lambda: bar.setValue(
                bar.maximum() if was_bottom else min(old_pos, bar.maximum())
            ),
        )

    def _insert_widget(self, widget: QWidget) -> None:
        self.body.insertWidget(max(0, self.body.count() - 1), widget)

    def add_label(
        self,
        text: str,
        style_name: str = "Body",
        selectable: bool = True,
        wrap: bool = True,
    ) -> QLabel:
        label = QLabel(str(text))
        label.setObjectName(style_name)
        label.setWordWrap(wrap)
        if selectable:
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._insert_widget(label)
        return label

    def add_row(self, left: str, right: str, right_style: str = "Strong") -> None:
        row_widget = QWidget()
        row = QHBoxLayout(row_widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)

        l = QLabel(str(left))
        l.setObjectName("Muted")
        l.setWordWrap(True)
        l.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        r = QLabel(str(right))
        r.setObjectName(right_style)
        r.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        r.setWordWrap(True)
        r.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        row.addWidget(l, 1)
        row.addWidget(r, 2)
        self._insert_widget(row_widget)

    def add_json_block(self, title: str, value: Any) -> None:
        self.add_label(title, "Muted")
        self.add_label(compact_json(value), "MonoBlock")


class FireCoreControlCard(QFrame):
    """Stable FireCore card. No embedding. No auto-launch.

    It controls the real FireCore through ~/.prometheus/fire_state.json and gives
    clear launch/status controls. This avoids WM glitches and restart loops.
    """

    def __init__(self):
        super().__init__()
        self.setObjectName("Card")
        self.state = "idle"
        self.accent = "#D0A644"
        self.phase = 0.0

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("FireCore")
        title.setObjectName("CardTitle")
        self.status = QLabel("not checked")
        self.status.setObjectName("Muted")

        self.launch_btn = QPushButton("Launch FireCore")
        self.launch_btn.clicked.connect(self.launch)

        header.addWidget(title)
        header.addWidget(self.status, 1)
        header.addWidget(self.launch_btn)
        root.addLayout(header)

        self.visual = FireOrbPreview()
        root.addWidget(self.visual, 1)

        self.detail = QLabel("State file: ~/.prometheus/fire_state.json")
        self.detail.setObjectName("Muted")
        self.detail.setWordWrap(True)
        root.addWidget(self.detail)

    def set_state(self, state: str, accent: str, status: str) -> None:
        self.state = normalize_state(state)
        self.accent = accent
        self.status.setText(status)
        self.visual.set_state(self.state, accent)

    def launch(self) -> None:
        if not (FIRECORE_DIR / "project.godot").exists():
            QMessageBox.warning(
                self, "FireCore", f"FireCore project not found:\n{FIRECORE_DIR}"
            )
            return

        exe = shutil.which("godot")
        candidates = [
            Path("/home/tatel/Desktop/godot/Godot_v4.6.2-stable_linux.x86_64"),
            Path("/home/tatel/Downloads/Godot_v4.6.2-stable_linux.x86_64"),
            Path("/home/tatel/Desktop/Godot_v4.6.2-stable_linux.x86_64"),
        ]
        cmd = [exe, "--path", str(FIRECORE_DIR)] if exe else None
        if cmd is None:
            for c in candidates:
                if c.exists():
                    cmd = [str(c), "--path", str(FIRECORE_DIR)]
                    break

        if cmd is None:
            QMessageBox.warning(self, "FireCore", "Godot executable not found.")
            return

        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.status.setText("launched externally")


class FireOrbPreview(QFrame):
    """Small HUD-native preview that mirrors state color. It is not the real FireCore."""

    def __init__(self):
        super().__init__()
        self.setObjectName("FirePreview")
        self.setMinimumHeight(260)
        self.state = "idle"
        self.accent = "#D0A644"
        self.phase = 0.0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(33)

    def set_state(self, state: str, accent: str) -> None:
        self.state = state
        self.accent = accent

    def tick(self) -> None:
        speed = {
            "idle": 0.018,
            "listening": 0.024,
            "processing": 0.040,
            "speaking": 0.030,
            "executing": 0.045,
            "warning": 0.044,
        }.get(self.state, 0.02)
        self.phase += speed
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)

        path = QPainterPath()
        path.addRoundedRect(rect, 14, 14)
        p.fillPath(path, QColor("#050607"))
        p.setPen(QPen(QColor("#252B34"), 1))
        p.drawRoundedRect(rect, 14, 14)

        accent = QColor(self.accent)
        cx, cy = rect.center().x(), rect.center().y()
        orb_r = min(rect.width(), rect.height()) * 0.24
        pulse = 1.0 + 0.06 * math.sin(self.phase * 5.0)
        if self.state in {"processing", "executing"}:
            pulse += 0.06 * math.sin(self.phase * 9.0)
        if self.state == "warning":
            pulse += 0.045 * math.sin(self.phase * 14.0)

        glow_r = orb_r * 2.4
        glow = QRadialGradient(QPointF(cx, cy), glow_r)
        glow.setColorAt(0.0, QColor(accent.red(), accent.green(), accent.blue(), 80))
        glow.setColorAt(0.45, QColor(accent.red(), accent.green(), accent.blue(), 32))
        glow.setColorAt(1.0, QColor(accent.red(), accent.green(), accent.blue(), 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glow)
        p.drawEllipse(QRectF(cx - glow_r, cy - glow_r, glow_r * 2, glow_r * 2))

        body_r = orb_r * pulse
        body = QRadialGradient(
            QPointF(cx - body_r * 0.25, cy - body_r * 0.25), body_r * 1.25
        )
        body.setColorAt(0.00, QColor(255, 245, 182, 245))
        body.setColorAt(0.28, QColor(255, 171, 38, 235))
        body.setColorAt(0.78, QColor(accent.red(), max(70, accent.green()), 18, 225))
        body.setColorAt(1.00, QColor(65, 26, 5, 220))
        p.setBrush(body)
        p.drawEllipse(QRectF(cx - body_r, cy - body_r, body_r * 2, body_r * 2))

        p.setBrush(Qt.BrushStyle.NoBrush)
        for i in range(5):
            yoff = (i - 2) * body_r * 0.22 + math.sin(
                self.phase * (1.6 + i * 0.2) + i
            ) * body_r * 0.08
            band_w = body_r * (1.84 - abs(i - 2) * 0.16)
            band_h = body_r * (0.22 + i * 0.015)
            color = QColor(
                255, 205, 72, 105 + int(45 * abs(math.sin(self.phase * 2.2 + i)))
            )
            p.setPen(QPen(color, 2.0 if i == 2 else 1.3))
            p.drawArc(
                QRectF(cx - band_w / 2, cy + yoff - band_h / 2, band_w, band_h),
                0,
                360 * 16,
            )

        p.setFont(QFont("Inter", 12, QFont.Weight.Bold))
        p.setPen(accent)
        p.drawText(
            QRectF(0, rect.bottom() - 42, rect.width(), 24),
            Qt.AlignmentFlag.AlignCenter,
            self.state.upper(),
        )


class PrometheusHUD(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Prometheus HUD")
        self.resize(1540, 940)

        self.state_data: dict[str, Any] = {}
        self.audio_data: dict[str, Any] = {}
        self.heartbeat_data: dict[str, Any] = {}
        self.mission_data: dict[str, Any] = {}
        self.working_memory: dict[str, Any] = {}
        self.tasks: list[Any] = []
        self.agents: list[Any] = []
        self.activity: list[str] = []
        self.last_firecore_state = ""
        self.last_firecore_payload: dict[str, Any] = {}
        self._last_accent = ""

        self._build_ui()

        self.data_timer = QTimer(self)
        self.data_timer.timeout.connect(self.refresh_data)
        self.data_timer.start(REFRESH_MS)

        self.metric_timer = QTimer(self)
        self.metric_timer.timeout.connect(self.update_metrics)
        self.metric_timer.start(METRIC_MS)

        self.refresh_data()
        self.update_metrics()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        header = QFrame()
        header.setObjectName("Header")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 10, 16, 10)
        header_layout.setSpacing(12)

        self.brand = QLabel("PROMETHEUS")
        self.brand.setObjectName("Brand")
        self.state_pill = QLabel("INITIALIZING")
        self.state_pill.setObjectName("StatePill")
        self.project_label = QLabel("Project: —")
        self.project_label.setObjectName("HeaderText")
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh_data)

        header_layout.addWidget(self.brand)
        header_layout.addWidget(self.state_pill)
        header_layout.addWidget(self.project_label, 1)
        header_layout.addWidget(self.refresh_btn)
        root.addWidget(header)

        grid = QGridLayout()
        grid.setSpacing(12)

        self.firecore_card = FireCoreControlCard()
        self.mission_card = ScrollCard("Mission")
        self.command_card = ScrollCard("Command")
        self.activity_card = ScrollCard("Activity")
        self.metrics_card = MetricPanel()
        self.memory_card = ScrollCard("Working Memory")
        self.tasks_card = ScrollCard("Background Tasks")
        self.agents_card = ScrollCard("Agents")

        self.cpu_graph = self.metrics_card.cpu_graph
        self.mem_graph = self.metrics_card.mem_graph
        self.disk_graph = self.metrics_card.disk_graph
        self.mic_graph = self.metrics_card.mic_graph
        self.spk_graph = self.metrics_card.spk_graph

        # Layout: readable visual dashboard.
        # Left: FireCore / Mission / Command.
        # Center: Activity + equal Tasks/Agents cards.
        # Right: ONLY Metric Graphs + Working Memory.
        grid.addWidget(self.firecore_card, 0, 0, 2, 1)
        grid.addWidget(self.mission_card, 2, 0)
        grid.addWidget(self.command_card, 3, 0)

        grid.addWidget(self.activity_card, 0, 1, 4, 1)

        grid.addWidget(self.metrics_card, 0, 2, 3, 1)
        grid.addWidget(self.memory_card, 3, 2, 2, 1)

        # Bottom middle split: equal width/height task cards.
        grid.addWidget(self.tasks_card, 4, 0)
        grid.addWidget(self.agents_card, 4, 1)

        grid.setColumnStretch(0, 4)
        grid.setColumnStretch(1, 4)
        grid.setColumnStretch(2, 4)
        grid.setRowStretch(0, 2)
        grid.setRowStretch(1, 2)
        grid.setRowStretch(2, 2)
        grid.setRowStretch(3, 2)
        grid.setRowStretch(4, 2)
        root.addLayout(grid, 1)

        self.command_input = QLineEdit()
        self.command_input.setPlaceholderText("Type a command for Prometheus…")
        self.command_input.returnPressed.connect(self.submit_command)
        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self.submit_command)
        self.render_command()

    def accent(self) -> str:
        return accent_for(str(self.state_data.get("state", "idle")))

    def apply_theme(self) -> None:
        accent = self.accent()
        self.setStyleSheet(f"""
            QWidget {{
                background: #090B0E;
                color: #EFE8DB;
                font-family: Inter, Segoe UI, Ubuntu, sans-serif;
                font-size: 15px;
            }}

            #Header, #Card {{
                background: #12151A;
                border: 1px solid #292E36;
                border-radius: 16px;
            }}

            #Brand {{
                color: {accent};
                font-size: 21px;
                font-weight: 900;
                letter-spacing: 5px;
            }}

            #StatePill {{
                background: {accent};
                color: #080A0C;
                border-radius: 12px;
                padding: 6px 12px;
                font-weight: 900;
                letter-spacing: 1px;
            }}

            #HeaderText {{
                color: #A8A194;
                font-size: 14px;
            }}

            #CardTitle {{
                color: {accent};
                font-size: 17px;
                font-weight: 900;
                letter-spacing: 1px;
            }}

            #CardBody {{
                background: transparent;
            }}

            #Body {{
                color: #E8E0D2;
                font-size: 15px;
                line-height: 1.35em;
            }}

            #Muted {{
                color: #9D9588;
                font-size: 14px;
            }}

            #Strong {{
                color: #F4EDDF;
                font-size: 15px;
                font-weight: 800;
            }}

            #Good {{
                color: #7EE083;
                font-size: 15px;
                font-weight: 800;
            }}

            #Bad {{
                color: #FF6B5F;
                font-size: 15px;
                font-weight: 800;
            }}

            #Mono, #MonoBlock {{
                color: #CBC2B3;
                font-family: JetBrains Mono, Fira Code, Ubuntu Mono, monospace;
                font-size: 13px;
                background: #0D1014;
                border: 1px solid #222832;
                border-radius: 8px;
                padding: 8px;
            }}

            #MetricGraph {{
                background: transparent;
                border: none;
            }}

            #FirePreview {{
                background: #050607;
                border: 1px solid #252B34;
                border-radius: 14px;
            }}

            QLineEdit {{
                background: #0D1014;
                border: 1px solid {accent};
                border-radius: 10px;
                color: #F4EDDF;
                padding: 12px 13px;
                font-size: 16px;
            }}

            QPushButton {{
                background: #242A33;
                color: #F2EEE5;
                border: 1px solid #3A404B;
                border-radius: 10px;
                padding: 10px 14px;
                font-size: 14px;
                font-weight: 800;
            }}

            QPushButton:hover {{
                background: #303846;
                border-color: {accent};
            }}

            QScrollArea {{
                background: transparent;
                border: none;
            }}

            QScrollBar:vertical {{
                background: #0D1014;
                width: 12px;
                margin: 2px;
                border-radius: 6px;
            }}

            QScrollBar::handle:vertical {{
                background: #3D4550;
                border-radius: 6px;
                min-height: 28px;
            }}

            QScrollBar::handle:vertical:hover {{
                background: {accent};
            }}
        """)

    def refresh_data(self) -> None:
        self.state_data = read_json(STATE_FILE, {})
        self.audio_data = read_json(AUDIO_FILE, {})
        self.heartbeat_data = read_json(HEARTBEAT_FILE, {})
        self.mission_data = read_json(MISSION_FILE, {})
        self.working_memory = read_json(WORKING_MEMORY_FILE, {})

        tasks_payload = read_json(TASKS_FILE, {})
        self.tasks = (
            tasks_payload.get("tasks", [])
            if isinstance(tasks_payload, dict)
            else (tasks_payload if isinstance(tasks_payload, list) else [])
        )

        agents_payload = read_json(AGENTS_FILE, {})
        self.agents = (
            agents_payload.get("agents", [])
            if isinstance(agents_payload, dict)
            else (agents_payload if isinstance(agents_payload, list) else [])
        )

        self.activity = read_activity_lines()
        self.sync_firecore_state()
        self.render_all()

    def update_metrics(self) -> None:
        accent = self.accent()
        cpu = psutil.cpu_percent(interval=None) if psutil else 0.0
        mem = psutil.virtual_memory().percent if psutil else 0.0
        disk = psutil.disk_usage(str(Path.home())).percent if psutil else 0.0
        mic = float(self.audio_data.get("mic_level", 0.0) or 0.0) * 100.0
        spk = float(self.audio_data.get("speaker_level", 0.0) or 0.0) * 100.0

        self.cpu_graph.push(cpu, accent)
        self.mem_graph.push(mem, accent)
        self.disk_graph.push(disk, accent)
        self.mic_graph.push(mic, accent)
        self.spk_graph.push(spk, accent)

    def heartbeat_ok(self) -> bool:
        try:
            ts = str(self.heartbeat_data.get("ts", ""))
            stamp = dt.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
            return (dt.datetime.now() - stamp).total_seconds() <= 18
        except Exception:
            return False

    def sync_firecore_state(self) -> None:
        raw_state = str(self.state_data.get("state", "idle"))
        state = normalize_state(raw_state)
        mic = float(self.audio_data.get("mic_level", 0.0) or 0.0)
        spk = float(self.audio_data.get("speaker_level", 0.0) or 0.0)
        values = firecore_values(state)

        entering_energetic = state in {
            "executing",
            "warning",
        } and self.last_firecore_state not in {"executing", "warning"}

        payload = {
            "state": state,
            "flare": entering_energetic,
            "speaking_level": spk if state == "speaking" else 0.0,
            "mic_level": mic,
            "speaker_level": spk,
            "intensity": values["intensity"],
            "rotation_speed": values["rotation_speed"],
            "spark_rate": values["spark_rate"],
            "source": "jarvis_desktop_hud.py",
            "ts": iso_now(),
        }

        comparable = dict(payload)
        comparable.pop("ts", None)
        if comparable == self.last_firecore_payload:
            return

        try:
            atomic_write_json(FIRECORE_STATE_FILE, payload)
            self.last_firecore_state = state
            self.last_firecore_payload = comparable
        except Exception:
            pass

    def render_all(self) -> None:
        new_accent = self.accent()
        if new_accent != self._last_accent:
            self._last_accent = new_accent
            self.apply_theme()
        raw_state = str(self.state_data.get("state", "idle"))
        heartbeat = "ONLINE" if self.heartbeat_ok() else "OFFLINE"
        self.state_pill.setText(f"{heartbeat} · {raw_state.upper()}")

        project = one_line(
            self.state_data.get("active_project") or self.state_data.get("project")
        )
        window = one_line(self.state_data.get("active_window"), "")
        self.project_label.setText(
            f"Project: {project}" + (f" · {window}" if window else "")
        )

        fc_status = (
            "found · state synced"
            if (FIRECORE_DIR / "project.godot").exists()
            and FIRECORE_STATE_FILE.exists()
            else "missing or unsynced"
        )
        self.firecore_card.set_state(raw_state, self.accent(), fc_status)

        self.render_mission()
        self.render_activity()
        self.render_memory()
        self.render_tasks()
        self.render_agents()

    def render_mission(self) -> None:
        key = json.dumps(self.mission_data, sort_keys=True, default=str)

        def draw() -> None:
            current = self.mission_data.get("current_mission") or self.mission_data.get(
                "active_goal"
            )
            next_action = self.mission_data.get("next_action")
            subtasks = self.mission_data.get("subtasks") or []
            completed = self.mission_data.get("completed_subtasks") or []
            self.mission_card.add_label(current or "No active mission written.", "Body")
            self.mission_card.add_row("Next", next_action or "No next action set.")
            self.mission_card.add_row(
                "Progress", f"{len(completed)} done · {len(subtasks)} open"
            )
            if subtasks:
                self.mission_card.add_label("Open", "Muted")
                for item in subtasks[:14]:
                    desc = (
                        item.get("description") if isinstance(item, dict) else str(item)
                    )
                    self.mission_card.add_label(f"• {desc}", "Body")

        self.mission_card.update_if_changed(key, draw)

    def render_activity(self) -> None:
        key = "\n".join(self.activity)

        def draw() -> None:
            if not self.activity:
                self.activity_card.add_label(
                    "No recent activity log lines found.", "Muted"
                )
                self.activity_card.add_label(
                    "Checked: " + ", ".join(str(g) for g in LOG_GLOBS), "MonoBlock"
                )
                return
            for line in self.activity:
                self.activity_card.add_label(line, "Mono", wrap=False)

        self.activity_card.update_if_changed(key, draw)

    def render_memory(self) -> None:
        key = json.dumps(self.working_memory, sort_keys=True, default=str)

        def draw() -> None:
            if not isinstance(self.working_memory, dict) or not self.working_memory:
                self.memory_card.add_label("No working-memory data.", "Muted")
                return
            diag = self.working_memory.get("last_diagnostic")
            if isinstance(diag, dict):
                self.memory_card.add_label("last_diagnostic", "Muted")
                for k, v in list(diag.items())[:12]:
                    self.memory_card.add_row(str(k), compact_json(v, 320))
            for key2 in ("chat_input", "chat_response"):
                val = self.working_memory.get(key2)
                if isinstance(val, dict):
                    self.memory_card.add_json_block(key2, val)
            shown = {"last_diagnostic", "chat_input", "chat_response"}
            for k, v in self.working_memory.items():
                if k not in shown:
                    self.memory_card.add_json_block(str(k), v)

        self.memory_card.update_if_changed(key, draw)

    def render_tasks(self) -> None:
        key = json.dumps(self.tasks, sort_keys=True, default=str)

        def draw() -> None:
            if not self.tasks:
                self.tasks_card.add_label("No background tasks.", "Muted")
                return
            for idx, task in enumerate(self.tasks[:32], start=1):
                if isinstance(task, dict):
                    title = (
                        task.get("description")
                        or task.get("intent")
                        or task.get("name")
                        or f"Task {idx}"
                    )
                    self.tasks_card.add_label(f"{idx}. {title}", "Body")
                    for field in ("status", "ok", "started_at", "updated_at", "error"):
                        if field in task:
                            self.tasks_card.add_row(field, one_line(task.get(field)))
                else:
                    self.tasks_card.add_label(f"{idx}. {task}", "Body")

        self.tasks_card.update_if_changed(key, draw)

    def render_agents(self) -> None:
        key = json.dumps(self.agents, sort_keys=True, default=str)

        def draw() -> None:
            if not self.agents:
                self.agents_card.add_label("No registered agents.", "Muted")
                return
            for idx, agent in enumerate(self.agents[:32], start=1):
                if isinstance(agent, dict):
                    name = agent.get("name") or agent.get("id") or f"Agent {idx}"
                    status = agent.get("status") or agent.get("state") or "—"
                    self.agents_card.add_row(str(name), str(status))
                    for field in ("role", "current_task", "model", "last_seen"):
                        if field in agent:
                            self.agents_card.add_row(field, one_line(agent.get(field)))
                else:
                    self.agents_card.add_label(str(agent), "Body")

        self.agents_card.update_if_changed(key, draw)

    def render_command(self) -> None:
        self.command_card.clear()
        command_row = QWidget()
        command_layout = QHBoxLayout(command_row)
        command_layout.setContentsMargins(0, 0, 0, 0)
        command_layout.setSpacing(8)
        self.command_input.setParent(command_row)
        self.send_btn.setParent(command_row)
        command_layout.addWidget(self.command_input, 1)
        command_layout.addWidget(self.send_btn)
        self.command_card._insert_widget(command_row)

    def submit_command(self) -> None:
        text = self.command_input.text().strip()
        if not text:
            return
        self.command_input.clear()
        wm = read_json(WORKING_MEMORY_FILE, {})
        if not isinstance(wm, dict):
            wm = {}
        wm["chat_input"] = {"text": text, "ts": iso_now()}
        atomic_write_json(WORKING_MEMORY_FILE, wm)
        self.working_memory = wm
        self.render_memory()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Prometheus HUD")
    font = QFont("Inter")
    font.setPointSize(12)
    app.setFont(font)
    win = PrometheusHUD()
    win.show()
    return app.exec()


# ─────────────────────────────────────────────────────────────────────────────
# Store / SystemStats / HUDWindow — compatibility layer for tests and
# future HUD refactor.  These classes provide a clean data-model interface
# without touching the existing PrometheusHUD render logic.
# ─────────────────────────────────────────────────────────────────────────────

class Store:
    """
    HUD data store.  Holds all state fields the HUD reads.
    refresh() reads the mission file and any other live state.
    """

    def __init__(self) -> None:
        self.mission: dict = {}
        self.chat_history: list = []
        self.activity_filter: str = ""
        self.diagnostic: dict = {}
        self.cost_log: list = []
        self.active_tab: int = 0
        self.news_articles: list = []
        self.news_status: str = "demo"

    def refresh(self) -> None:
        self.mission = read_json(MISSION_FILE, {})


class SystemStats:
    """Snapshot of system resource usage (CPU %, RAM %, disk %)."""

    def __init__(self) -> None:
        try:
            self.cpu: float = psutil.cpu_percent(interval=None) if psutil else 0.0
            self.ram: float = psutil.virtual_memory().percent if psutil else 0.0
            self.disk: float = psutil.disk_usage(str(Path.home())).percent if psutil else 0.0
        except Exception:
            self.cpu = 0.0
            self.ram = 0.0
            self.disk = 0.0


class HUDWindow(PrometheusHUD):
    """
    HUDWindow wraps PrometheusHUD with the Store/SystemStats interface.
    Adds _set_tab() for tab-based navigation and _draw_mission_strip()
    for custom mission-strip painting hooks.
    """

    def __init__(
        self,
        store: "Store | None" = None,
        stats: "SystemStats | None" = None,
    ) -> None:
        self._store = store if store is not None else Store()
        self._stats = stats if stats is not None else SystemStats()
        super().__init__()

    def _set_tab(self, tab_idx: int) -> None:
        """Switch the active HUD tab and update the store."""
        self._store.active_tab = tab_idx

    def _draw_mission_strip(self, painter: Any = None) -> None:
        """Hook for painting a mission-status strip. No-op until custom paint is added."""


# ─────────────────────────────────────────────────────────────────────────────
# NewsCard — Guardian-powered news widget for the Prometheus HUD.
#
# Fetches up to 50 Guardian articles in a background thread, applies
# Prometheus relevance scoring, selects the best 9, and displays 3 at a time.
# A QTimer cycles through sets 1-3 → 4-6 → 7-9 automatically.
# Clicking an article row opens it in the default browser.
# ─────────────────────────────────────────────────────────────────────────────

_NEWS_CYCLE_INTERVAL_MS = 12_000   # rotate to next set every 12 seconds
_NEWS_REFRESH_INTERVAL_MS = 300_000  # re-fetch from Guardian every 5 minutes


class _ArticleRow(QFrame):
    """Single compact article row: tag pill + title + time-ago."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("ArticleRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._href = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(3)

        meta_row = QHBoxLayout()
        meta_row.setContentsMargins(0, 0, 0, 0)
        meta_row.setSpacing(8)

        self.tag_label = QLabel()
        self.tag_label.setObjectName("NewsPill")
        self.time_label = QLabel()
        self.time_label.setObjectName("Muted")
        self.source_label = QLabel("The Guardian")
        self.source_label.setObjectName("Muted")

        meta_row.addWidget(self.tag_label)
        meta_row.addWidget(self.source_label)
        meta_row.addStretch(1)
        meta_row.addWidget(self.time_label)

        self.title_label = QLabel()
        self.title_label.setObjectName("NewsTitle")
        self.title_label.setWordWrap(True)

        self.summary_label = QLabel()
        self.summary_label.setObjectName("Muted")
        self.summary_label.setWordWrap(True)

        layout.addLayout(meta_row)
        layout.addWidget(self.title_label)
        layout.addWidget(self.summary_label)

    def load(self, article: dict) -> None:
        self._href = article.get("href", "")
        self.tag_label.setText(article.get("tag", ""))
        self.time_label.setText(article.get("time_ago", ""))
        title = article.get("title", "")
        if len(title) > 90:
            title = title[:87] + "…"
        self.title_label.setText(title)
        summary = article.get("summary", "")
        if len(summary) > 110:
            summary = summary[:107] + "…"
        self.summary_label.setText(summary)

    def clear(self) -> None:
        self._href = ""
        self.tag_label.setText("")
        self.time_label.setText("")
        self.title_label.setText("")
        self.summary_label.setText("")

    def mousePressEvent(self, event: Any) -> None:
        if self._href and event.button() == Qt.MouseButton.LeftButton:
            try:
                import subprocess as _sp
                _sp.Popen(["xdg-open", self._href],
                          stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
            except Exception:
                pass
        super().mousePressEvent(event)


class NewsCard(QFrame):
    """
    Guardian news card for the Prometheus HUD.

    Fetches articles via prometheus.services.guardian_news in a background
    thread and cycles through three sets of 3 articles via QTimer.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("Card")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self._articles: list[dict] = []
        self._status: str = "loading"
        self._set_index: int = 0   # which set of 3 is visible (0, 1, 2)
        self._fetch_result: "list[dict] | None" = None
        self._fetch_status: str = ""
        self._fetch_error: str = ""

        self._build_ui()
        self._start_fetch()

        # Cycle timer — advances set every N seconds
        self._cycle_timer = QTimer(self)
        self._cycle_timer.timeout.connect(self._advance_set)
        self._cycle_timer.start(_NEWS_CYCLE_INTERVAL_MS)

        # Refresh timer — re-fetches from Guardian periodically
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._start_fetch)
        self._refresh_timer.start(_NEWS_REFRESH_INTERVAL_MS)

        # Poll timer — checks if background fetch completed
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._check_fetch_result)
        self._poll_timer.start(200)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(8)

        # Header row
        header = QHBoxLayout()
        title = QLabel("News")
        title.setObjectName("CardTitle")
        self._status_label = QLabel("Loading…")
        self._status_label.setObjectName("Muted")
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self._status_label)
        root.addLayout(header)

        # Set indicator (which of 3 sets is visible)
        dots = QHBoxLayout()
        dots.setContentsMargins(0, 0, 0, 0)
        dots.setSpacing(6)
        self._dots: list[QLabel] = []
        for _ in range(3):
            d = QLabel("●")
            d.setObjectName("Muted")
            self._dots.append(d)
            dots.addWidget(d)
        dots.addStretch(1)
        root.addLayout(dots)

        # Three article rows
        self._rows: list[_ArticleRow] = []
        for _ in range(3):
            row = _ArticleRow()
            self._rows.append(row)
            root.addWidget(row)

        self._hint = QLabel("Auto-cycling • Click to open")
        self._hint.setObjectName("Muted")
        root.addWidget(self._hint)
        root.addStretch(1)

    def _start_fetch(self) -> None:
        """Launch a background thread to fetch news."""
        import threading

        self._fetch_result = None
        self._fetch_status = ""
        self._fetch_error = ""
        if self._status == "loading":
            pass  # already shown

        def _worker() -> None:
            try:
                from prometheus.services.guardian_news import get_news
                articles, status = get_news()
                self._fetch_result = articles
                self._fetch_status = status
            except Exception as exc:
                self._fetch_result = []
                self._fetch_status = "error"
                self._fetch_error = str(exc)[:100]

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

    def _check_fetch_result(self) -> None:
        """Called by poll timer. If fetch is done, update display."""
        if self._fetch_result is None:
            return
        articles = self._fetch_result
        status = self._fetch_status
        self._fetch_result = None  # consume

        if articles:
            self._articles = articles
            self._status = status
        else:
            if not self._articles:
                try:
                    from prometheus.services.guardian_news import _fallback_articles
                    self._articles = _fallback_articles()
                except Exception:
                    pass
            self._status = "error" if status == "error" else "demo"

        self._render_current_set()

    def _advance_set(self) -> None:
        """Rotate to the next set of 3 articles."""
        if len(self._articles) >= 9:
            self._set_index = (self._set_index + 1) % 3
        self._render_current_set()

    def _render_current_set(self) -> None:
        """Populate the 3 article rows from the current set."""
        start = self._set_index * 3
        batch = self._articles[start:start + 3]

        for i, row in enumerate(self._rows):
            if i < len(batch):
                row.load(batch[i])
            else:
                row.clear()

        # Update status label
        labels = {
            "live": "Live",
            "fallback": "Demo",
            "demo": "Demo",
            "error": "Error",
            "loading": "Loading…",
        }
        self._status_label.setText(labels.get(self._status, self._status.title()))

        # Update set indicator dots
        for i, d in enumerate(self._dots):
            d.setObjectName("Strong" if i == self._set_index else "Muted")
            d.setStyleSheet("" if i == self._set_index else "color: #444;")


if __name__ == "__main__":
    raise SystemExit(main())

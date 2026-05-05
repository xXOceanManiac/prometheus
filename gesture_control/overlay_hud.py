from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from PyQt6.QtCore import Qt, QTimer, QPoint, QRect
from PyQt6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen, QBrush
from PyQt6.QtWidgets import QApplication, QWidget

from gesture_engine import HAND_CONNECTIONS


@dataclass
class OverlayState:
    active: bool = True
    gesture_name: str = 'idle'
    pinch_ratio: float = 0.0
    scroll_delta: int = 0
    drag_active: bool = False
    status: str = 'starting'
    hand_points: List[Tuple[float, float]] = field(default_factory=list)
    cursor_px: Optional[Tuple[int, int]] = None
    visible: bool = False
    screenshot_flash_until: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class OverlayHUD(QWidget):
    def __init__(self, state: OverlayState):
        super().__init__()
        self.state = state
        self._init_ui()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update)
        self.timer.start(16)

    def _init_ui(self):
        screen = QGuiApplication.primaryScreen().geometry()
        self.setGeometry(screen)
        self.setWindowTitle('Prometheus Gesture HUD')
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.show()

    def paintEvent(self, event):
        import time
        with self.state.lock:
            active = self.state.active
            gesture_name = self.state.gesture_name
            pinch_ratio = self.state.pinch_ratio
            scroll_delta = self.state.scroll_delta
            drag_active = self.state.drag_active
            status = self.state.status
            hand_points = list(self.state.hand_points)
            cursor_px = self.state.cursor_px
            visible = self.state.visible
            flash = self.state.screenshot_flash_until > time.time()

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        if flash:
            painter.fillRect(self.rect(), QColor(255, 255, 255, 48))

        if visible and hand_points:
            self._draw_hand(painter, hand_points)
        if cursor_px:
            self._draw_cursor_target(painter, cursor_px)
        self._draw_status(painter, active, gesture_name, pinch_ratio, scroll_delta, drag_active, status)

    def _draw_hand(self, painter: QPainter, hand_points: List[Tuple[float, float]]):
        w = self.width()
        h = self.height()

        def to_px(p):
            x = int((1.0 - p[0]) * w)
            y = int(p[1] * h)
            return QPoint(x, y)

        points = [to_px(p) for p in hand_points]

        painter.setPen(QPen(QColor(120, 180, 255, 170), 3))
        for a, b in HAND_CONNECTIONS:
            painter.drawLine(points[a], points[b])

        for i, pt in enumerate(points):
            if i in (4, 8):
                painter.setBrush(QBrush(QColor(0, 255, 200, 180)))
                painter.setPen(QPen(QColor(255, 255, 255, 190), 2))
                r = 10
            elif i in (12, 16, 20):
                painter.setBrush(QBrush(QColor(255, 210, 110, 160)))
                painter.setPen(Qt.PenStyle.NoPen)
                r = 7
            else:
                painter.setBrush(QBrush(QColor(180, 220, 255, 120)))
                painter.setPen(Qt.PenStyle.NoPen)
                r = 5
            painter.drawEllipse(pt, r, r)

        painter.setPen(QPen(QColor(255, 80, 80, 210), 4))
        painter.drawLine(points[4], points[8])

    def _draw_cursor_target(self, painter: QPainter, cursor_px: Tuple[int, int]):
        x, y = cursor_px
        painter.setPen(QPen(QColor(255, 255, 255, 180), 2))
        painter.drawEllipse(QPoint(x, y), 14, 14)
        painter.drawLine(x - 18, y, x + 18, y)
        painter.drawLine(x, y - 18, x, y + 18)

    def _draw_status(self, painter, active, gesture_name, pinch_ratio, scroll_delta, drag_active, status):
        box = QRect(18, self.height() - 118, 340, 92)
        painter.setPen(QPen(QColor(120, 180, 255, 185), 2))
        painter.setBrush(QBrush(QColor(8, 14, 22, 150)))
        painter.drawRoundedRect(box, 14, 14)

        painter.setPen(QColor(240, 248, 255, 225))
        title_font = QFont('DejaVu Sans', 11)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.drawText(box.adjusted(14, 14, -14, -14), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft, 'PROMETHEUS GESTURE')

        info_font = QFont('DejaVu Sans Mono', 9)
        painter.setFont(info_font)
        lines = [
            f"state: {'active' if active else 'paused'}",
            f"gesture: {gesture_name} {'(drag)' if drag_active else ''}",
            f"pinch: {pinch_ratio:.2f}   scroll: {scroll_delta:+d}",
            f"cam: {status}",
        ]
        y = box.top() + 38
        for line in lines:
            painter.drawText(box.left() + 14, y, line)
            y += 17


def run_overlay(state: OverlayState):
    import sys
    app = QApplication(sys.argv)
    hud = OverlayHUD(state)
    sys.exit(app.exec())

from __future__ import annotations

import threading
import time
from typing import Callable

from pynput import keyboard

from prometheus.infra.config import CONFIG
from prometheus.infra.utils import log_event


# Build key sets at import time so getattr is only called once.
_CTRL_KEYS: frozenset = frozenset(
    k for k in [
        keyboard.Key.ctrl,
        getattr(keyboard.Key, "ctrl_l", None),
        getattr(keyboard.Key, "ctrl_r", None),
    ]
    if k is not None
)
_WIN_KEYS: frozenset = frozenset(
    k for k in [
        keyboard.Key.cmd,
        getattr(keyboard.Key, "cmd_l", None),
        getattr(keyboard.Key, "cmd_r", None),
    ]
    if k is not None
)
_ALT_KEYS: frozenset = frozenset(
    k for k in [
        keyboard.Key.alt,
        getattr(keyboard.Key, "alt_l", None),
        getattr(keyboard.Key, "alt_r", None),
        getattr(keyboard.Key, "alt_gr", None),
    ]
    if k is not None
)
_REQUIRED: frozenset = frozenset({"ctrl", "win", "alt"})


def _key_category(key) -> str | None:
    if key in _CTRL_KEYS:
        return "ctrl"
    if key in _WIN_KEYS:
        return "win"
    if key in _ALT_KEYS:
        return "alt"
    return None


class PushToTalkController:
    """
    Activates when Ctrl + Win + Alt are ALL held simultaneously for hold_seconds.
    Releasing any one of the three commits the turn immediately.
    """

    def __init__(
        self,
        on_activated: Callable[[], None],
        on_released: Callable[[], None],
    ) -> None:
        self.on_activated = on_activated
        self.on_released = on_released
        self.hold_seconds = float(CONFIG.get("ptt_hold_seconds", 0.25))

        self._listener: keyboard.Listener | None = None
        self._monitor_thread: threading.Thread | None = None
        self._running = False

        self._lock = threading.Lock()
        # Categories currently held: subset of {"ctrl", "win", "alt"}
        self._held: set[str] = set()
        # Timestamp when all three categories first became simultaneously held.
        # Reset to 0.0 whenever any required key is released.
        self._all_held_since: float = 0.0
        # Whether a recording turn is currently live.
        self._activated: bool = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
        )
        self._listener.start()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, daemon=True, name="ptt-monitor"
        )
        self._monitor_thread.start()
        log_event(
            "ptt_started",
            {"key": "ctrl+win+alt", "hold_seconds": self.hold_seconds},
        )

    def stop(self) -> None:
        self._running = False
        if self._listener:
            self._listener.stop()
            self._listener = None
        log_event("ptt_stopped", {})

    def is_activated(self) -> bool:
        with self._lock:
            return self._activated

    # ------------------------------------------------------------------
    # Keyboard callbacks (run in the pynput listener thread)
    # ------------------------------------------------------------------

    def _on_press(self, key) -> None:
        cat = _key_category(key)
        if cat is None:
            return
        with self._lock:
            self._held.add(cat)
            # Start the hold timer the first moment all three are down.
            if self._held >= _REQUIRED and self._all_held_since == 0.0:
                self._all_held_since = time.time()

    def _on_release(self, key) -> None:
        cat = _key_category(key)
        if cat is None:
            return
        should_fire_release = False
        with self._lock:
            self._held.discard(cat)
            if cat in _REQUIRED:
                # Any required key released: reset hold timer.
                self._all_held_since = 0.0
                if self._activated:
                    self._activated = False
                    should_fire_release = True
        if should_fire_release:
            log_event("ptt_released", {})
            self.on_released()

    # ------------------------------------------------------------------
    # Hold-duration monitor (runs in _monitor_thread)
    # ------------------------------------------------------------------

    def _monitor_loop(self) -> None:
        while self._running:
            should_activate = False
            with self._lock:
                if (
                    self._held >= _REQUIRED
                    and not self._activated
                    and self._all_held_since > 0.0
                    and (time.time() - self._all_held_since) >= self.hold_seconds
                ):
                    self._activated = True
                    should_activate = True
            if should_activate:
                log_event("ptt_activated", {})
                self.on_activated()
            time.sleep(0.01)

"""
sensors/window_sensor.py — Active window polling via xdotool.

Polls every 1.5 seconds. Emits WINDOW_CHANGED only when active window changes.
Falls back gracefully if xdotool is not installed.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from typing import Any

from event_bus import Event, EventType, Priority, get_bus
from utils import log_event

_POLL_INTERVAL = 1.5

_CACHE: dict[str, Any] = {
    "window_title": "",
    "app_name": "",
    "window_class": "",
    "updated_at": "",
}
_LAST: dict[str, str] = {"title": "\x00", "wclass": "\x00"}


def get_cache() -> dict[str, Any]:
    """Return a snapshot of the last known window state."""
    return dict(_CACHE)


class WindowSensor:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running = False
        self._available = shutil.which("xdotool") is not None

    def is_available(self) -> bool:
        return self._available

    def get_status(self) -> dict[str, Any]:
        return {
            "name": "window_sensor",
            "available": self._available,
            "running": self._running,
            "last_title": _CACHE.get("window_title", ""),
        }

    async def start(self) -> None:
        if not self._available:
            log_event("window_sensor_unavailable", {"reason": "xdotool not found"})
            return
        if self._running:
            return
        self._running = True
        self._task = asyncio.ensure_future(self._poll_loop())
        log_event("window_sensor_started", {})

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _poll_loop(self) -> None:
        loop = asyncio.get_event_loop()
        while self._running:
            try:
                title, wclass = await loop.run_in_executor(None, _get_active_window)
                if title != _LAST["title"] or wclass != _LAST["wclass"]:
                    _LAST["title"] = title
                    _LAST["wclass"] = wclass
                    app_name = _app_from_title(title)
                    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
                    _CACHE.update({
                        "window_title": title,
                        "app_name": app_name,
                        "window_class": wclass,
                        "updated_at": ts,
                    })
                    get_bus().publish(Event(
                        event_type=EventType.WINDOW_CHANGED,
                        source="window_sensor",
                        payload={
                            "window_title": title,
                            "app_name": app_name,
                            "window_class": wclass,
                            "timestamp": ts,
                        },
                        priority=Priority.NORMAL,
                    ))
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log_event("window_sensor_poll_error", {"error": str(exc)[:80]})
            await asyncio.sleep(_POLL_INTERVAL)


def _get_active_window() -> tuple[str, str]:
    """Return (window_title, window_class). Returns ('', '') on any failure."""
    title = ""
    wclass = ""
    try:
        r = subprocess.run(
            ["xdotool", "getactivewindow", "getwindowname"],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0:
            title = r.stdout.strip()[:200]
    except Exception:
        pass
    try:
        r2 = subprocess.run(
            ["xdotool", "getactivewindow", "getwindowclassname"],
            capture_output=True, text=True, timeout=2,
        )
        if r2.returncode == 0:
            wclass = r2.stdout.strip()[:80]
    except Exception:
        pass
    return title, wclass


def _app_from_title(title: str) -> str:
    if not title:
        return ""
    t = title.lower()
    for kw in ("vs code", "vscode", " code"):
        if kw in t:
            return "vscode"
    for kw in ("firefox", "chrome", "chromium", "brave"):
        if kw in t:
            return kw
    for kw in ("konsole", "terminal", "alacritty", "kitty", "bash", "zsh"):
        if kw in t:
            return "terminal"
    for kw in ("obsidian",):
        if kw in t:
            return "obsidian"
    return title.split("—")[0].split("-")[0].strip()[:30]

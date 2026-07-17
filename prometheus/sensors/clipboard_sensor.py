"""
sensors/clipboard_sensor.py — PRIMARY X selection monitoring via xclip.

Polls every 2 seconds. Emits TEXT_SELECTED when highlighted text changes.
Skips empty or whitespace-only selections. Truncates at 2000 chars.
Falls back gracefully if xclip is not installed.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from typing import Any

from prometheus.sensors.event_bus import Event, EventType, Priority, get_bus
from prometheus.infra.utils import log_event

_POLL_INTERVAL = 2.0
_MAX_CHARS = 2000

_CACHE: dict[str, Any] = {
    "selected_text": "",
    "char_count": 0,
    "updated_at": "",
}
_last_text: str = "\x00"  # sentinel so first real selection always fires


def get_cache() -> dict[str, Any]:
    """Return a snapshot of the last known clipboard selection."""
    return dict(_CACHE)


class ClipboardSensor:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running = False
        self._available = shutil.which("xclip") is not None

    def is_available(self) -> bool:
        return self._available

    def get_status(self) -> dict[str, Any]:
        return {
            "name": "clipboard_sensor",
            "available": self._available,
            "running": self._running,
            "char_count": _CACHE.get("char_count", 0),
        }

    async def start(self) -> None:
        if not self._available:
            log_event("clipboard_sensor_unavailable", {"reason": "xclip not found"})
            return
        if self._running:
            return
        self._running = True
        self._task = asyncio.ensure_future(self._poll_loop())
        log_event("clipboard_sensor_started", {})

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _poll_loop(self) -> None:
        global _last_text
        loop = asyncio.get_event_loop()
        while self._running:
            try:
                text = await loop.run_in_executor(None, _get_primary_selection)
                if text and text != _last_text:
                    _last_text = text
                    char_count = len(text)
                    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
                    _CACHE.update({
                        "selected_text": text,
                        "char_count": char_count,
                        "updated_at": ts,
                    })
                    get_bus().publish(Event(
                        event_type=EventType.TEXT_SELECTED,
                        source="clipboard_sensor",
                        payload={
                            "selected_text": text,
                            "char_count": char_count,
                            "timestamp": ts,
                        },
                        priority=Priority.NORMAL,
                    ))
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log_event("clipboard_sensor_poll_error", {"error": str(exc)[:80]})
            await asyncio.sleep(_POLL_INTERVAL)


def _get_primary_selection() -> str:
    """Return PRIMARY X selection, stripped and capped. Returns '' on any failure."""
    try:
        r = subprocess.run(
            ["xclip", "-o", "-selection", "primary"],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode != 0:
            return ""
        text = r.stdout.strip()
        if not text:
            return ""
        return text[:_MAX_CHARS]
    except Exception:
        return ""

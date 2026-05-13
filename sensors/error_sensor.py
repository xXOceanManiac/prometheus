"""
sensors/error_sensor.py — Live error detection via journalctl follow.

Tails journalctl in follow mode (--user session).
Emits ERROR_DETECTED (Priority.HIGH) on pattern match.
Deduplicates identical error lines within 30 seconds.
Caps payload at 500 chars per line. Never dumps full log contents.
Falls back gracefully if journalctl is not installed.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
import time
from collections import deque
from typing import Any

from event_bus import Event, EventType, Priority, get_bus
from utils import log_event

_DEDUPE_SECS = 30.0
_MAX_LINE = 500
_MAX_CACHE = 20

_ERROR_RX = re.compile(
    r"\b(ERROR|FATAL|CRITICAL|Exception|Traceback|failed|refused|timeout)\b",
    re.I,
)
_SEVERITY_MAP: dict[str, str] = {
    "fatal": "fatal",
    "critical": "critical",
    "error": "error",
    "exception": "error",
    "traceback": "error",
    "failed": "warning",
    "refused": "warning",
    "timeout": "warning",
}

_CACHE: deque[dict[str, Any]] = deque(maxlen=_MAX_CACHE)
_DEDUPE: dict[str, float] = {}  # md5_prefix -> last emit monotonic time


def get_cache() -> list[dict[str, Any]]:
    """Return list of last ERROR_DETECTED payloads."""
    return list(_CACHE)


def inject_line(line: str, source: str = "journalctl") -> None:
    """Process a raw log line. Public so tests can inject lines directly."""
    _process_line(line, source)


class ErrorSensor:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running = False
        self._available = shutil.which("journalctl") is not None
        self._proc: asyncio.subprocess.Process | None = None

    def is_available(self) -> bool:
        return self._available

    def get_status(self) -> dict[str, Any]:
        return {
            "name": "error_sensor",
            "available": self._available,
            "running": self._running,
            "recent_errors": len(_CACHE),
        }

    async def start(self) -> None:
        if not self._available:
            log_event("error_sensor_unavailable", {"reason": "journalctl not found"})
            return
        if self._running:
            return
        self._running = True
        self._task = asyncio.ensure_future(self._tail_loop())
        log_event("error_sensor_started", {})

    async def stop(self) -> None:
        self._running = False
        if self._proc:
            try:
                self._proc.kill()
                await self._proc.wait()
            except Exception:
                pass
            self._proc = None
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _tail_loop(self) -> None:
        while self._running:
            try:
                await self._run_journalctl()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log_event("error_sensor_tail_error", {"error": str(exc)[:120]})
                await asyncio.sleep(5.0)

    async def _run_journalctl(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            "journalctl", "--user", "-f", "--no-pager", "--output=short",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert self._proc.stdout is not None
        async for raw in self._proc.stdout:
            if not self._running:
                break
            line = raw.decode(errors="ignore").strip()
            if line:
                _process_line(line[:_MAX_LINE], source="journalctl")
        await self._proc.wait()


def _process_line(line: str, source: str) -> None:
    m = _ERROR_RX.search(line)
    if not m:
        return

    line_hash = hashlib.md5(line.encode()).hexdigest()[:16]
    now = time.monotonic()
    if line_hash in _DEDUPE and now - _DEDUPE[line_hash] < _DEDUPE_SECS:
        return
    _DEDUPE[line_hash] = now

    pattern_text = m.group(0)
    severity = _SEVERITY_MAP.get(pattern_text.lower(), "error")

    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    entry: dict[str, Any] = {
        "source": source,
        "raw_line": line[:_MAX_LINE],
        "error_pattern": pattern_text,
        "severity": severity,
        "timestamp": ts,
    }
    _CACHE.append(entry)

    get_bus().publish(Event(
        event_type=EventType.ERROR_DETECTED,
        source="error_sensor",
        payload=entry,
        priority=Priority.HIGH,
    ))

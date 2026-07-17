"""
sensors/filesystem_sensor.py — Project file watching via inotifywait.

Watches current mission project paths for file changes.
Emits FILE_CHANGED events per change with 3-second debounce per file.
Reloads watch paths when mission changes via set_watch_paths().
Falls back gracefully if inotifywait is not installed.
"""
from __future__ import annotations

import asyncio
import shutil
import time
from collections import deque
from pathlib import Path
from typing import Any

from prometheus.sensors.event_bus import Event, EventType, Priority, get_bus
from prometheus.infra.utils import log_event

_DEBOUNCE_SECS = 3.0
_MAX_CACHE = 20

_CACHE: deque[dict[str, Any]] = deque(maxlen=_MAX_CACHE)
_DEBOUNCE: dict[str, float] = {}  # full_path -> last emit monotonic time


def get_cache() -> list[dict[str, Any]]:
    """Return list of last FILE_CHANGED events."""
    return list(_CACHE)


class FilesystemSensor:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running = False
        self._available = shutil.which("inotifywait") is not None
        self._watch_paths: list[str] = []
        self._proc: asyncio.subprocess.Process | None = None

    def is_available(self) -> bool:
        return self._available

    def get_status(self) -> dict[str, Any]:
        return {
            "name": "filesystem_sensor",
            "available": self._available,
            "running": self._running,
            "watch_paths": list(self._watch_paths),
            "recent_changes": len(_CACHE),
        }

    def set_watch_paths(self, paths: list[str]) -> None:
        """Update watched directories. Only watches paths that exist."""
        valid = [p for p in paths if Path(p).is_dir()]
        if valid != self._watch_paths:
            self._watch_paths = valid
            if self._running and self._proc:
                asyncio.ensure_future(self._kill_proc())

    async def start(self) -> None:
        if not self._available:
            log_event("filesystem_sensor_unavailable", {"reason": "inotifywait not found"})
            return
        if self._running:
            return
        self._running = True
        self._task = asyncio.ensure_future(self._watch_loop())
        log_event("filesystem_sensor_started", {})

    async def stop(self) -> None:
        self._running = False
        await self._kill_proc()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _kill_proc(self) -> None:
        if self._proc:
            try:
                self._proc.kill()
                await self._proc.wait()
            except Exception:
                pass
            self._proc = None

    async def _watch_loop(self) -> None:
        while self._running:
            if not self._watch_paths:
                await asyncio.sleep(2.0)
                continue
            try:
                await self._run_inotifywait()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log_event("filesystem_sensor_error", {"error": str(exc)[:120]})
                await asyncio.sleep(5.0)

    async def _run_inotifywait(self) -> None:
        cmd = [
            "inotifywait", "-m", "-r",
            "-e", "create,modify,delete,moved_from,moved_to",
            "--format", "%e %w %f",
        ] + self._watch_paths

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert self._proc.stdout is not None

        async for raw in self._proc.stdout:
            if not self._running:
                break
            line = raw.decode(errors="ignore").strip()
            if line:
                _handle_inotify_line(line, self._watch_paths)

        await self._proc.wait()


def _handle_inotify_line(line: str, watch_paths: list[str]) -> None:
    """Parse one inotifywait output line and emit FILE_CHANGED event if not debounced."""
    parts = line.split(" ", 2)
    if len(parts) < 3:
        return
    change_type, watch_dir, filename = parts[0], parts[1], parts[2]
    full_path = watch_dir + filename
    now = time.monotonic()

    if full_path in _DEBOUNCE and now - _DEBOUNCE[full_path] < _DEBOUNCE_SECS:
        return
    _DEBOUNCE[full_path] = now

    parts_path = Path(watch_dir).parts
    project = parts_path[-2] if len(parts_path) >= 2 else watch_dir

    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    entry: dict[str, Any] = {
        "path": watch_dir,
        "filename": filename,
        "change_type": change_type,
        "project": project,
        "timestamp": ts,
    }
    _CACHE.append(entry)

    get_bus().publish(Event(
        event_type=EventType.FILE_CHANGED,
        source="filesystem_sensor",
        payload=entry,
        priority=Priority.NORMAL,
    ))

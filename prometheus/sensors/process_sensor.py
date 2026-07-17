"""
sensors/process_sensor.py — Dev server process monitoring via /proc.

Polls /proc every 10 seconds. Tracks processes matching known dev server patterns.
Emits PROCESS_CHANGED when a relevant process starts or stops.
Irrelevant system processes are silently ignored.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from prometheus.sensors.event_bus import Event, EventType, Priority, get_bus
from prometheus.infra.utils import log_event

_POLL_INTERVAL = 10.0

_DEV_PATTERNS = frozenset({
    "node", "python", "uvicorn", "gunicorn", "webpack", "vite",
    "next", "gatsby", "rails", "django", "flask", "fastapi",
    "cargo", "go run", "bun", "deno", "docker", "npm run",
})

# pid -> {pid, name, cmdline_summary, started_at}
_REGISTRY: dict[int, dict[str, Any]] = {}
# Flat list for world model consumption (name + cmdline_summary)
_CACHE: list[dict[str, Any]] = []


def get_cache() -> list[dict[str, Any]]:
    """Return snapshot of currently running dev processes."""
    return list(_CACHE)


def get_registry() -> dict[int, dict[str, Any]]:
    """Return full registry including pid and started_at."""
    return dict(_REGISTRY)


class ProcessSensor:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running = False

    def is_available(self) -> bool:
        return os.path.isdir("/proc")

    def get_status(self) -> dict[str, Any]:
        return {
            "name": "process_sensor",
            "available": self.is_available(),
            "running": self._running,
            "tracked_processes": len(_REGISTRY),
        }

    async def start(self) -> None:
        if not self.is_available():
            log_event("process_sensor_unavailable", {"reason": "/proc not available"})
            return
        if self._running:
            return
        self._running = True
        self._task = asyncio.ensure_future(self._poll_loop())
        log_event("process_sensor_started", {})

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
                await loop.run_in_executor(None, scan_processes)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log_event("process_sensor_poll_error", {"error": str(exc)[:80]})
            await asyncio.sleep(_POLL_INTERVAL)


def scan_processes() -> None:
    """Scan /proc for dev server processes; update registry and emit events."""
    global _CACHE
    current: dict[int, dict[str, Any]] = {}

    try:
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                cmdline = _read_cmdline(pid)
                if not cmdline:
                    continue
                name = _match_dev_pattern(cmdline)
                if not name:
                    continue
                current[pid] = {
                    "pid": pid,
                    "name": name,
                    "cmdline_summary": cmdline[:80],
                    "started_at": _REGISTRY.get(pid, {}).get(
                        "started_at", time.strftime("%Y-%m-%dT%H:%M:%S")
                    ),
                }
            except (PermissionError, FileNotFoundError, ProcessLookupError):
                continue
    except Exception as exc:
        log_event("process_sensor_scan_error", {"error": str(exc)[:80]})
        return

    ts = time.strftime("%Y-%m-%dT%H:%M:%S")

    # Detect newly started processes
    for pid, info in current.items():
        if pid not in _REGISTRY:
            _REGISTRY[pid] = info
            get_bus().publish(Event(
                event_type=EventType.PROCESS_CHANGED,
                source="process_sensor",
                payload={
                    **info,
                    "status": "started",
                    "change_type": "started",
                    "timestamp": ts,
                },
                priority=Priority.NORMAL,
            ))

    # Detect stopped processes
    for pid in list(_REGISTRY.keys()):
        if pid not in current:
            info = _REGISTRY.pop(pid)
            get_bus().publish(Event(
                event_type=EventType.PROCESS_CHANGED,
                source="process_sensor",
                payload={
                    **info,
                    "status": "stopped",
                    "change_type": "stopped",
                    "timestamp": ts,
                },
                priority=Priority.NORMAL,
            ))

    _CACHE = [
        {
            "pid": info["pid"],
            "name": info["name"],
            "cmdline_summary": info["cmdline_summary"],
        }
        for info in _REGISTRY.values()
    ]


def _read_cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            data = f.read(512)
        return data.replace(b"\x00", b" ").decode(errors="ignore").strip()
    except Exception:
        return ""


def _match_dev_pattern(cmdline: str) -> str:
    """Return the most specific (longest) dev pattern found in cmdline, or ''."""
    cmd_lower = cmdline.lower()
    best = ""
    for pattern in _DEV_PATTERNS:
        if pattern in cmd_lower and len(pattern) > len(best):
            best = pattern
    return best

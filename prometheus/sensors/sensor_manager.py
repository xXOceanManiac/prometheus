"""
sensor_manager.py — Starts and supervises all Prometheus desktop sensors.

Each sensor runs as an asyncio task. If a sensor crashes, it is restarted
after 5 seconds. Exposes get_sensor_status() for health queries.

Usage:
    from prometheus.sensors.sensor_manager import SensorManager
    manager = SensorManager()
    await manager.start()
    status = manager.get_sensor_status()
    await manager.stop()
"""
from __future__ import annotations

import asyncio
from typing import Any

from prometheus.sensors.window_sensor import WindowSensor
from prometheus.sensors.clipboard_sensor import ClipboardSensor
from prometheus.sensors.filesystem_sensor import FilesystemSensor
from prometheus.sensors.error_sensor import ErrorSensor
from prometheus.sensors.process_sensor import ProcessSensor
from prometheus.infra.utils import log_event

_RESTART_DELAY = 5.0


class SensorManager:
    def __init__(self) -> None:
        self._sensors: dict[str, Any] = {
            "window":     WindowSensor(),
            "clipboard":  ClipboardSensor(),
            "filesystem": FilesystemSensor(),
            "error":      ErrorSensor(),
            "process":    ProcessSensor(),
        }
        self._supervisor_tasks: dict[str, asyncio.Task] = {}
        self._running = False

    async def start(self) -> None:
        """Start all sensor supervisors."""
        if self._running:
            return
        self._running = True
        for name, sensor in self._sensors.items():
            task = asyncio.ensure_future(self._supervise(name, sensor))
            self._supervisor_tasks[name] = task
        log_event("sensor_manager_started", {"sensors": list(self._sensors.keys())})

    async def stop(self) -> None:
        """Stop all sensors and cancel supervisor tasks."""
        self._running = False
        for name, sensor in self._sensors.items():
            try:
                await sensor.stop()
            except Exception as exc:
                log_event("sensor_manager_stop_error", {
                    "sensor": name, "error": str(exc)[:80]
                })
        for name, task in self._supervisor_tasks.items():
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        log_event("sensor_manager_stopped", {})

    def get_sensor_status(self) -> dict[str, dict[str, Any]]:
        """Return health status of every sensor."""
        return {name: sensor.get_status() for name, sensor in self._sensors.items()}

    def set_filesystem_watch_paths(self, paths: list[str]) -> None:
        """Forward new watch paths to the filesystem sensor."""
        fs = self._sensors.get("filesystem")
        if fs is not None:
            fs.set_watch_paths(paths)

    # ── Supervisor ────────────────────────────────────────────────────────────

    async def _supervise(self, name: str, sensor: Any) -> None:
        """Run sensor; restart on crash after _RESTART_DELAY seconds."""
        while self._running:
            try:
                await sensor.start()
                # Wait for the sensor's internal task to finish
                task = getattr(sensor, "_task", None)
                if task is not None:
                    await asyncio.shield(task)
                else:
                    # Sensor was unavailable and start() returned immediately
                    return
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log_event("sensor_crashed", {
                    "sensor": name,
                    "error": str(exc)[:120],
                    "restart_in_secs": _RESTART_DELAY,
                })

            if not self._running:
                break
            await asyncio.sleep(_RESTART_DELAY)
            log_event("sensor_restarting", {"sensor": name})
            # Reset sensor state before restart
            try:
                await sensor.stop()
            except Exception:
                pass
            sensor._running = False
            sensor._task = None

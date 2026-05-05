"""
watchdog.py — System health watchdog for Prometheus.

Runs as a background daemon thread, performing health checks every 30 seconds.
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from utils import log_event
from working_memory import WorkingMemory

_TASK_TIMEOUT_MINUTES = 10
_VOICE_LOST_MINUTES = 5
_GIT_LOG_DEPTH = 30


def _parse_iso(ts: str) -> float:
    """Parse an ISO datetime string (YYYY-MM-DDTHH:MM:SS) into a Unix timestamp."""
    try:
        import datetime
        return datetime.datetime.fromisoformat(ts).timestamp()
    except Exception:
        return 0.0


class PrometheusWatchdog:
    """
    Daemon thread that monitors Prometheus health.

    Checks every 30 seconds:
      1. Voice connection liveness
      2. Stalled background tasks (running > 10 min)
      3. Cost limits
      4. Git checkpoint presence during active tasks
    """

    _INTERVAL = 30.0

    def __init__(
        self,
        working_memory: WorkingMemory | None = None,
        cost_tracker: Any | None = None,  # CostTracker — optional dep injection
        realtime_client: Any | None = None,
    ) -> None:
        self._wm = working_memory or WorkingMemory()
        self._cost_tracker = cost_tracker
        self._client = realtime_client
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._voice_lost_at: float | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Launch the check loop in a daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="prometheus-watchdog",
        )
        self._thread.start()
        log_event("watchdog_started", {})

    def stop(self) -> None:
        """Signal the loop to exit. Thread exits within one interval."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._INTERVAL + 5)
        log_event("watchdog_stopped", {})

    def is_alive(self) -> bool:
        """Return True if the watchdog thread is running."""
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # Check loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        """Main loop: wait INTERVAL seconds, run all checks, repeat until stopped."""
        while not self._stop_event.wait(timeout=self._INTERVAL):
            try:
                self._check_voice_connection()
                self._check_background_threads()
                self._check_cost_limits()
                self._check_git_state()
            except Exception as exc:
                log_event("watchdog_check_error", {"error": str(exc)[:200]})

    # ------------------------------------------------------------------
    # Individual checks (callable directly for testing)
    # ------------------------------------------------------------------

    def _check_voice_connection(self) -> bool:
        """
        Check WorkingMemory["voice_connected"].
        If False for > 5 minutes, log and attempt reconnect.
        """
        try:
            wm = self._wm.read()
            connected = bool(wm.get("voice_connected", True))
            if not connected:
                if self._voice_lost_at is None:
                    self._voice_lost_at = time.time()
                lost_minutes = (time.time() - self._voice_lost_at) / 60.0
                if lost_minutes > _VOICE_LOST_MINUTES:
                    log_event("watchdog_voice_lost", {"lost_minutes": round(lost_minutes, 1)})
                    if self._client is not None and hasattr(self._client, "reconnect"):
                        try:
                            self._client.reconnect()
                            log_event("watchdog_reconnect_attempted", {})
                        except Exception as exc:
                            log_event("watchdog_reconnect_failed", {"error": str(exc)[:200]})
            else:
                self._voice_lost_at = None
            return connected
        except Exception as exc:
            log_event("watchdog_voice_check_error", {"error": str(exc)[:200]})
            return True

    def _check_background_threads(self) -> bool:
        """
        Check for any task with status="running" running > 10 minutes.
        Updates status to "timeout" in WorkingMemory if found.
        """
        _TASK_KEYS = ("last_orchestration_result", "last_coding_result")
        now = time.time()
        found_timeout = False

        try:
            wm = self._wm.read()
            for key in _TASK_KEYS:
                task = wm.get(key)
                if not isinstance(task, dict):
                    continue
                if task.get("status") != "running":
                    continue
                started_at = str(task.get("started_at", ""))
                if not started_at:
                    continue
                started_epoch = _parse_iso(started_at)
                if started_epoch == 0.0:
                    continue
                running_minutes = (now - started_epoch) / 60.0
                if running_minutes > _TASK_TIMEOUT_MINUTES:
                    goal = str(task.get("goal", ""))[:80]
                    log_event("watchdog_task_timeout", {
                        "key": key,
                        "goal": goal,
                        "running_minutes": round(running_minutes, 1),
                    })
                    task["status"] = "timeout"
                    self._wm.write({key: task})
                    found_timeout = True

            return not found_timeout
        except Exception as exc:
            log_event("watchdog_thread_check_error", {"error": str(exc)[:200]})
            return True

    def _check_cost_limits(self) -> bool:
        """
        Check CostTracker limits. If exceeded, set WorkingMemory["cost_limit_reached"] = True.
        """
        if self._cost_tracker is None:
            return True
        try:
            result = self._cost_tracker.check_limits()
            if not result.get("ok", True):
                reason = result.get("reason", "cost limit reached")
                log_event("watchdog_cost_limit", {"reason": reason})
                self._wm.write({"cost_limit_reached": True})
                return False
            return True
        except Exception as exc:
            log_event("watchdog_cost_check_error", {"error": str(exc)[:200]})
            return True

    def _check_git_state(self) -> bool:
        """
        Warn if a task is running but there are no recent prometheus-checkpoint commits.
        """
        try:
            wm = self._wm.read()
            task_running = any(
                isinstance(wm.get(k), dict) and wm.get(k, {}).get("status") == "running"
                for k in ("last_orchestration_result", "last_coding_result")
            )
            if not task_running:
                return True

            result = subprocess.run(
                ["git", "log", "--oneline", f"-{_GIT_LOG_DEPTH}", "--grep=prometheus-checkpoint:"],
                capture_output=True,
                text=True,
                cwd=str(Path(__file__).parent),
                timeout=10,
            )
            if result.returncode == 0 and not result.stdout.strip():
                log_event("watchdog_no_checkpoint_warning", {
                    "git_log_depth": _GIT_LOG_DEPTH,
                    "note": "task running but no recent checkpoint",
                })
                return False
            return True
        except Exception as exc:
            log_event("watchdog_git_check_error", {"error": str(exc)[:200]})
            return True

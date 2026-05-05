"""
launch.py — Single entry point for Prometheus v4.0.0.

Starts all components in the correct order and handles graceful shutdown.

Usage:
  python launch.py [--no-hud] [--no-voice] [--dev] [--cost-limit FLOAT]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

# ── Ensure project root is on path ──────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_PROMETHEUS_DIR = Path.home() / ".prometheus"
_VERSION = "4.0.0"
__version__ = "4.0.0"


def _parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="prometheus",
        description="Prometheus v4.0.0 — autonomous desktop assistant",
    )
    parser.add_argument("--no-hud", action="store_true", help="Disable the HUD overlay")
    parser.add_argument("--no-voice", action="store_true", help="Disable the Realtime voice client")
    parser.add_argument("--dev", action="store_true", help="Enable verbose debug logging")
    parser.add_argument(
        "--cost-limit",
        type=float,
        default=5.00,
        metavar="USD",
        help="Override daily cost limit in USD (default: 5.00)",
    )
    return parser.parse_args(args)


def _prom_log(kind: str, payload: dict | None = None) -> None:
    """
    Write a log entry to ~/.prometheus/prometheus.jsonl.
    This is a secondary unified log for critical lifecycle events.
    """
    try:
        _PROMETHEUS_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "kind": kind,
            **(payload or {}),
        }
        prom_log = _PROMETHEUS_DIR / "prometheus.jsonl"
        with prom_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _ensure_bashrc_alias() -> None:
    """Add a 'prometheus' alias to ~/.bashrc if not already present."""
    bashrc = Path.home() / ".bashrc"
    alias_line = f"alias prometheus='cd {_PROJECT_ROOT} && python launch.py'"
    try:
        existing = bashrc.read_text(encoding="utf-8") if bashrc.exists() else ""
        if "alias prometheus=" not in existing:
            with bashrc.open("a", encoding="utf-8") as f:
                f.write(f"\n# Prometheus launcher\n{alias_line}\n")
    except Exception:
        pass


def _run_proactive_loop_thread(client: Any = None, workspace: Any = None) -> None:
    """
    Run ProactiveLoop in a dedicated daemon thread with its own asyncio event loop.
    Compatible with --no-voice mode (client=None → cycles log but don't surface).
    """
    try:
        from proactive_loop import ProactiveLoop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        pl = ProactiveLoop(client=client, workspace_manager=workspace)
        loop.run_until_complete(pl.run())
    except Exception as exc:
        try:
            from utils import log_event
            log_event("proactive_loop_thread_error", {"error": str(exc)[:200]})
        except Exception:
            pass


def _fire_no_voice_briefing() -> None:
    """
    Generate and log a startup briefing without the voice client.
    Uses the template fallback so no LLM is required.
    Fires after a 3-second delay.
    """
    time.sleep(3.0)
    try:
        from session_briefing import _template_briefing, _time_of_day_label
        from working_memory import WorkingMemory
        wm = WorkingMemory().read()
        next_ctx = str(wm.get("next_session_context") or "").strip()

        context = {
            "time_of_day": _time_of_day_label(),
            "active_project": str(wm.get("active_workspace") or "Prometheus").strip(),
            "next_session_context": next_ctx,
            "recent_sessions": [],
            "vault_memories": [],
            "background_tasks": [],
        }
        text = _template_briefing(context)
        from utils import log_event
        log_event("briefing_generated", {
            "length": len(text),
            "no_voice": True,
            "snippet": text[:120],
            "has_prev_context": bool(next_ctx),
        })
        _prom_log("briefing_generated", {
            "length": len(text),
            "snippet": text[:120],
            "has_prev_context": bool(next_ctx),
        })
    except Exception as exc:
        try:
            from utils import log_event
            log_event("briefing_no_voice_error", {"error": str(exc)[:200]})
        except Exception:
            pass


class PrometheusApp:
    """
    Orchestrates startup and shutdown of all Prometheus components.

    Usage:
        app = PrometheusApp(["--no-voice", "--no-hud"])
        app.start()          # non-blocking
        app.run()            # blocks until stop() called (production use)
        app.stop()           # graceful shutdown
    """

    def __init__(self, args: list[str] | None = None) -> None:
        self._args = _parse_args(args)
        self._stop_event = threading.Event()
        self._started = False

        # Components (assigned in start())
        self.cost_tracker: Any = None
        self.git_safety: Any = None
        self.working_memory: Any = None
        self.orchestrator: Any = None
        self.watchdog: Any = None
        self.hud: Any = None
        self.realtime_client: Any = None
        self._log_viewer: Any = None
        self._proactive_thread: threading.Thread | None = None
        self._briefing_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Initialize all components. Non-blocking."""
        if self._started:
            return

        _PROMETHEUS_DIR.mkdir(parents=True, exist_ok=True)

        # ── 1. Logging ───────────────────────────────────────────────────
        from utils import log_event

        if self._args.dev:
            os.environ.setdefault("PROMETHEUS_DEV", "1")

        # ── 2. CostTracker ───────────────────────────────────────────────
        from cost_tracker import CostTracker
        self.cost_tracker = CostTracker(
            daily_limit_usd=self._args.cost_limit,
            session_limit_usd=min(self._args.cost_limit, 2.00),
        )
        cost_summary = self.cost_tracker.session_summary()
        log_event("prometheus_cost_init", {
            "daily_limit": self._args.cost_limit,
            "daily_spent_so_far": cost_summary["daily_total"],
        })

        # ── 3. GitSafety ─────────────────────────────────────────────────
        from git_safety import GitSafety
        self.git_safety = GitSafety()

        # ── 4. WorkingMemory ─────────────────────────────────────────────
        from working_memory import WorkingMemory
        self.working_memory = WorkingMemory()

        # ── 5. LogViewer ─────────────────────────────────────────────────
        from log_viewer import LogViewer
        log_path = str(_PROMETHEUS_DIR / "prometheus.jsonl")
        self._log_viewer = LogViewer(log_path=log_path)

        # ── 6. Orchestrator ───────────────────────────────────────────────
        from orchestrator import Orchestrator
        self.orchestrator = Orchestrator(cost_tracker=self.cost_tracker)

        # ── 7. Watchdog ───────────────────────────────────────────────────
        from watchdog import PrometheusWatchdog
        self.watchdog = PrometheusWatchdog(
            working_memory=self.working_memory,
            cost_tracker=self.cost_tracker,
        )
        self.watchdog.start()

        # ── 8. HUD ────────────────────────────────────────────────────────
        if not self._args.no_hud:
            try:
                from hud import PrometheusHUD
                self.hud = PrometheusHUD(
                    working_memory=self.working_memory,
                    git_safety=self.git_safety,
                    log_path=log_path,
                )
                self.hud.start()
            except Exception as exc:
                log_event("hud_init_error", {"error": str(exc)[:200]})

        # ── 9. ProactiveLoop (daemon thread) ──────────────────────────────
        self._proactive_thread = threading.Thread(
            target=_run_proactive_loop_thread,
            args=(None, None),  # client=None compatible with --no-voice
            daemon=True,
            name="proactive-loop",
        )
        self._proactive_thread.start()

        # ── 10. Startup briefing ──────────────────────────────────────────
        if self._args.no_voice:
            # No-voice mode: generate briefing text offline and log it
            self._briefing_thread = threading.Thread(
                target=_fire_no_voice_briefing,
                daemon=True,
                name="briefing-no-voice",
            )
            self._briefing_thread.start()

        # ── 11. RealtimeClient (voice) ────────────────────────────────────
        if not self._args.no_voice:
            try:
                from realtime_client import RealtimeJarvisClient
                self.realtime_client = RealtimeJarvisClient()
            except Exception as exc:
                log_event("prometheus_voice_init_error", {"error": str(exc)[:200]})

        # ── 12. Bashrc alias ──────────────────────────────────────────────
        _ensure_bashrc_alias()

        # ── 13. prometheus_start ──────────────────────────────────────────
        start_payload = {
            "version": _VERSION,
            "no_hud": self._args.no_hud,
            "no_voice": self._args.no_voice,
            "dev": self._args.dev,
            "cost_limit": self._args.cost_limit,
        }
        log_event("prometheus_start", start_payload)
        _prom_log("prometheus_start", start_payload)

        # Register signal handlers
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        self._started = True

    def stop(self) -> None:
        """Graceful shutdown."""
        if not self._started:
            self._stop_event.set()
            return

        from utils import log_event

        # Write shutdown log FIRST — before any blocking operations so SIGTERM
        # under a tight timeout still captures this critical lifecycle event.
        try:
            summary = self._log_viewer.summarize_session() if self._log_viewer else ""
        except Exception:
            summary = ""

        shutdown_payload = {
            "version": _VERSION,
            "summary": summary[:500],
        }
        log_event("prometheus_shutdown", shutdown_payload)
        _prom_log("prometheus_shutdown", shutdown_payload)

        # Session wrap-up (best-effort — may call LLM, must not block shutdown)
        try:
            from session_summarizer import SessionSummarizer
            ss = SessionSummarizer()
            ss.trigger_wrapup(client=None)
        except Exception as exc:
            log_event("wrapup_error", {"error": str(exc)[:200]})

        # Stop voice
        if self.realtime_client is not None:
            try:
                if hasattr(self.realtime_client, "disconnect"):
                    self.realtime_client.disconnect()
            except Exception:
                pass

        # Stop watchdog
        if self.watchdog is not None:
            try:
                self.watchdog.stop()
            except Exception:
                pass

        self._stop_event.set()
        self._started = False

    def run(self) -> None:
        """
        Block until stop() is called or SIGTERM received.
        For production use — call start() first or it will be called here.
        """
        self.start()
        try:
            if self.realtime_client is not None and hasattr(self.realtime_client, "run"):
                asyncio.run(self.realtime_client.run())
            else:
                self._stop_event.wait()
        except KeyboardInterrupt:
            pass
        finally:
            if self._started:
                self.stop()

    def _handle_signal(self, signum: int, frame: Any) -> None:
        """SIGINT / SIGTERM handler."""
        self.stop()

    def is_running(self) -> bool:
        return self._started and not self._stop_event.is_set()


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def main(args: list[str] | None = None) -> None:
    app = PrometheusApp(args)
    app.run()


if __name__ == "__main__":
    main()

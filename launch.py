"""
launch.py — Single entry point for Prometheus v4.0.0.

Starts all components in the correct order and handles graceful shutdown.

Usage:
  python launch.py [--no-hud] [--no-voice] [--dev] [--cost-limit FLOAT]
"""
from __future__ import annotations

import argparse
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
            from hud import PrometheusHUD
            self.hud = PrometheusHUD(
                working_memory=self.working_memory,
                git_safety=self.git_safety,
                log_path=log_path,
            )
            self.hud.start()

        # ── 9. RealtimeClient (voice) ─────────────────────────────────────
        if not self._args.no_voice:
            try:
                from realtime_client import RealtimeJarvisClient
                self.realtime_client = RealtimeJarvisClient()
            except Exception as exc:
                log_event("prometheus_voice_init_error", {"error": str(exc)[:200]})

        # ── 10. Bashrc alias ──────────────────────────────────────────────
        _ensure_bashrc_alias()

        # ── 11. prometheus_start ──────────────────────────────────────────
        log_event("prometheus_start", {
            "version": _VERSION,
            "no_hud": self._args.no_hud,
            "no_voice": self._args.no_voice,
            "dev": self._args.dev,
            "cost_limit": self._args.cost_limit,
        })

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

        # Session summary
        try:
            summary = self._log_viewer.summarize_session() if self._log_viewer else ""
        except Exception:
            summary = ""

        log_event("prometheus_shutdown", {
            "version": _VERSION,
            "summary": summary[:500],
        })

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

        # Session wrap-up
        try:
            from session_summarizer import trigger_wrapup
            trigger_wrapup()
        except Exception:
            pass

        self._stop_event.set()
        self._started = False

    def run(self) -> None:
        """
        Block until stop() is called or SIGTERM received.
        For production use — call start() first.
        """
        self.start()
        try:
            if self.realtime_client is not None and hasattr(self.realtime_client, "run"):
                import asyncio
                asyncio.run(self.realtime_client.run())
            else:
                self._stop_event.wait()
        except KeyboardInterrupt:
            pass
        finally:
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

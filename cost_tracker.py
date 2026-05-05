"""
cost_tracker.py — API cost tracking and limit enforcement for Prometheus.

Tracks spend across Realtime API and Claude Code invocations.
Persists daily totals to ~/.prometheus/cost_log.jsonl.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from utils import log_event

_PROMETHEUS_DIR = Path.home() / ".prometheus"

# Cost rates: USD per million tokens
_RATES: dict[str, dict[str, float]] = {
    "claude-sonnet-4":      {"input": 3.0,   "output": 15.0},
    "claude-code-headless": {"input": 3.0,   "output": 15.0},
    "gpt-4o-realtime":      {"input": 100.0, "output": 200.0},
}
_DEFAULT_RATES: dict[str, float] = {"input": 3.0, "output": 15.0}


class CostTracker:
    """
    Thread-safe cost tracker. Records per-call costs, enforces daily and session limits.

    Cost log format (JSONL):
      {"timestamp": "...", "source": "...", "input_tokens": N, "output_tokens": N,
       "model": "...", "cost_usd": N}
    """

    def __init__(
        self,
        daily_limit_usd: float = 5.00,
        session_limit_usd: float = 2.00,
        log_path: str | None = None,
    ) -> None:
        self.daily_limit_usd = float(daily_limit_usd)
        self.session_limit_usd = float(session_limit_usd)
        self._log_path = Path(log_path) if log_path else _PROMETHEUS_DIR / "cost_log.jsonl"
        self._lock = threading.Lock()
        self.session_total: float = 0.0
        self.daily_total: float = 0.0
        self._calls: int = 0
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._load_daily_total()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(
        self,
        source: str,
        input_tokens: int,
        output_tokens: int,
        model: str,
    ) -> float:
        """
        Record an API call cost and append to the log.

        Returns cost_usd for this call.
        """
        rates = _RATES.get(model, _DEFAULT_RATES)
        cost = (input_tokens * rates["input"] + output_tokens * rates["output"]) / 1_000_000.0

        with self._lock:
            self.session_total += cost
            self.daily_total += cost
            self._calls += 1
            entry = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "source": str(source),
                "input_tokens": int(input_tokens),
                "output_tokens": int(output_tokens),
                "model": str(model),
                "cost_usd": round(cost, 8),
            }
            try:
                with self._log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry) + "\n")
            except Exception:
                pass

        log_event("cost_recorded", {
            "source": source,
            "cost_usd": round(cost, 6),
            "session_total": round(self.session_total, 6),
            "daily_total": round(self.daily_total, 6),
        })
        return cost

    def check_limits(self) -> dict[str, Any]:
        """
        Check whether any spend limit is reached.

        Returns:
            {"ok": bool, "reason": str | None, "session_total": float, "daily_total": float}
        """
        with self._lock:
            session = self.session_total
            daily = self.daily_total

        if daily >= self.daily_limit_usd:
            reason = (
                f"Daily limit ${self.daily_limit_usd:.2f} reached "
                f"(${daily:.2f} spent)"
            )
            return {"ok": False, "reason": reason, "session_total": session, "daily_total": daily}

        if session >= self.session_limit_usd:
            reason = (
                f"Session limit ${self.session_limit_usd:.2f} reached "
                f"(${session:.2f} spent)"
            )
            return {"ok": False, "reason": reason, "session_total": session, "daily_total": daily}

        return {"ok": True, "reason": None, "session_total": session, "daily_total": daily}

    def session_summary(self) -> dict[str, Any]:
        """Return session metrics."""
        with self._lock:
            return {
                "session_total": round(self.session_total, 6),
                "daily_total": round(self.daily_total, 6),
                "calls": self._calls,
            }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_daily_total(self) -> None:
        """
        Load today's cumulative cost from the log file.
        Resets to 0.0 if the most recent entry is from a previous calendar day.
        """
        today = time.strftime("%Y-%m-%d")
        if not self._log_path.exists():
            return
        try:
            lines = self._log_path.read_text(encoding="utf-8").splitlines()
            daily_total = 0.0
            last_date: str | None = None

            for line in lines:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                ts = str(entry.get("timestamp", ""))
                entry_date = ts[:10] if ts else ""
                if entry_date == today:
                    daily_total += float(entry.get("cost_usd", 0.0))
                    last_date = entry_date
                elif entry_date:
                    last_date = entry_date

            if last_date and last_date != today:
                # All entries are from a previous day — reset
                self.daily_total = 0.0
            else:
                self.daily_total = daily_total

        except Exception:
            self.daily_total = 0.0

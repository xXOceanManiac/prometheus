"""
log_viewer.py — Structured log replay and session summarization for Prometheus.

Reads JSONL log files and provides tail, filter, and summarize operations.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

_DEFAULT_LOG_PATH = Path.home() / ".prometheus" / "prometheus.jsonl"


class LogViewer:
    """
    Reads a Prometheus JSONL log file and provides structured replay.

    Each log line is a JSON dict with at minimum: {"ts": "...", "kind": "...", ...}
    The "kind" field maps to what utils.log_event() writes.
    """

    def __init__(self, log_path: str | None = None) -> None:
        self._log_path = Path(log_path) if log_path else _DEFAULT_LOG_PATH

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def tail(self, n: int = 50) -> list[dict[str, Any]]:
        """Return the last n log entries as parsed dicts."""
        lines = self._read_lines()
        return [self._parse(l) for l in lines[-n:] if l.strip()]

    def filter(
        self,
        event_name: str | None = None,
        since_minutes: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Return entries matching the given filters.

        Both filters are optional — if neither is provided, returns all entries.
        event_name: matches the "kind" field.
        since_minutes: only entries with "ts" within the last N minutes.
        """
        lines = self._read_lines()
        entries: list[dict[str, Any]] = []

        cutoff_ts: str | None = None
        if since_minutes is not None:
            cutoff_epoch = time.time() - (since_minutes * 60)
            cutoff_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cutoff_epoch))

        for line in lines:
            line = line.strip()
            if not line:
                continue
            entry = self._parse(line)
            if event_name is not None and entry.get("kind") != event_name:
                continue
            if cutoff_ts is not None and str(entry.get("ts", "")) < cutoff_ts:
                continue
            entries.append(entry)

        return entries

    def summarize_session(self) -> str:
        """
        Summarize all events since the most recent prometheus_start entry.

        Groups by event name (kind), counts occurrences, and returns a formatted string.
        """
        lines = self._read_lines()

        # Find the index of the last prometheus_start
        start_idx = 0
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            entry = self._parse(line)
            if entry.get("kind") == "prometheus_start":
                start_idx = i

        session_lines = [l for l in lines[start_idx:] if l.strip()]
        counts: dict[str, int] = defaultdict(int)
        for line in session_lines:
            entry = self._parse(line)
            kind = str(entry.get("kind") or "unknown")
            counts[kind] += 1

        total = len(session_lines)
        lines_out = [f"Session summary ({total} events):"]
        for kind in sorted(counts, key=lambda k: -counts[k]):
            lines_out.append(f"  - {kind}: {counts[kind]}")

        return "\n".join(lines_out)

    def errors_since_startup(self) -> list[dict[str, Any]]:
        """Return all log entries with level='error' since the most recent prometheus_start."""
        lines = self._read_lines()

        start_idx = 0
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            entry = self._parse(line)
            if entry.get("kind") == "prometheus_start":
                start_idx = i

        result = []
        for line in lines[start_idx:]:
            if not line.strip():
                continue
            entry = self._parse(line)
            if str(entry.get("level", "")).lower() == "error":
                result.append(entry)
        return result

    def tail_formatted(self, n: int = 100) -> str:
        """Return last n entries as a formatted multi-line JSON string."""
        entries = self.tail(n)
        return "\n".join(json.dumps(e, indent=None) for e in entries)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_lines(self) -> list[str]:
        """Read all lines from the log file. Returns [] if file does not exist."""
        try:
            if not self._log_path.exists():
                return []
            return self._log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            return []

    @staticmethod
    def _parse(line: str) -> dict[str, Any]:
        """Parse a JSONL line. Returns {} on failure."""
        try:
            return json.loads(line)
        except Exception:
            return {"raw": line[:200]}

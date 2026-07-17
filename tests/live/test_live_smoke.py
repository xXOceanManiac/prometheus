"""
tests/live/test_live_smoke.py — Read-only live smoke tests.

Run explicitly with:

    PROMETHEUS_LIVE_TESTS=1 .venv/bin/python -m pytest tests/live -q

Every test here is strictly read-only against the real machine:
- reads the real ~/.jarvis config and heartbeat
- reads the real vault SQLite database
- performs a read-only Google Calendar fetch
- reads Lumen's file-based outbox
- reads the HUD dashboard state file

No test may write files outside tmp, control devices, create Realtime
sessions, or call paid model APIs.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.live


# ── Vault ─────────────────────────────────────────────────────────────────────

class TestVaultLive:

    def test_vault_path_configured_and_db_exists(self):
        from prometheus.infra.config import CONFIG
        vault_path = str(CONFIG.get("vault_path", "")).strip()
        assert vault_path, "vault_path is empty in ~/.jarvis/config.json"
        assert Path(vault_path).exists(), f"vault directory not found: {vault_path}"
        db = Path(vault_path) / "data" / "memory.db"
        assert db.exists(), f"vault db not found at {db}"

    def test_query_vault_returns_results(self):
        from prometheus.memory.memory_core import query_vault
        results = query_vault("python project assistant", limit=3)
        assert isinstance(results, list)
        assert results, "vault returned no results for a broad query"
        for r in results:
            assert "chunk_id" in r
            assert "text" in r

    def test_query_vault_respects_limit_and_uniqueness(self):
        from prometheus.memory.memory_core import query_vault
        results = query_vault("prometheus assistant memory project", limit=2)
        assert len(results) <= 2
        ids = [r["chunk_id"] for r in results if r.get("chunk_id")]
        assert len(ids) == len(set(ids))


# ── Google Calendar (read-only) ───────────────────────────────────────────────

class TestGoogleCalendarLive:

    def test_calendar_get_today_reads_real_calendar(self):
        from prometheus.calendar.read_tools import calendar_get_today
        result = calendar_get_today()
        assert result.get("ok"), f"calendar read failed: {result.get('error')}"
        assert isinstance(result.get("events"), list)

    def test_calendar_list_upcoming_reads_real_calendar(self):
        from prometheus.calendar.read_tools import calendar_list_upcoming
        result = calendar_list_upcoming(max_results=5, days=7)
        assert result.get("ok"), f"calendar read failed: {result.get('error')}"
        assert isinstance(result.get("events"), list)


# ── Lumen (file-based, read-only) ─────────────────────────────────────────────

class TestLumenLive:

    def test_lumen_project_present(self):
        from prometheus.infra.paths import LUMEN_ROOT
        assert LUMEN_ROOT.is_dir(), f"Lumen project not found at {LUMEN_ROOT}"

    def test_lumen_outbox_readable(self):
        from prometheus.infra.paths import LUMEN_OUTBOX_DIR
        # The outbox may legitimately be empty; reading it must work.
        if not LUMEN_OUTBOX_DIR.is_dir():
            pytest.skip("Lumen outbox directory not created yet")
        entries = list(LUMEN_OUTBOX_DIR.glob("*.json"))
        for e in entries[:3]:
            json.loads(e.read_text(encoding="utf-8"))


# ── Running service state (read-only) ─────────────────────────────────────────

class TestServiceLive:

    def test_heartbeat_is_fresh(self):
        hb = Path.home() / ".jarvis" / "heartbeat.json"
        if not hb.exists():
            pytest.skip("Prometheus service not running (no heartbeat file)")
        data = json.loads(hb.read_text(encoding="utf-8"))
        ts = datetime.strptime(data["ts"], "%Y-%m-%dT%H:%M:%S")
        age = (datetime.now() - ts).total_seconds()
        assert age < 60, f"heartbeat is stale ({age:.0f}s old) — service hung or stopped"

    def test_dashboard_state_is_fresh_and_valid(self):
        state_file = Path.home() / "Desktop" / "PROMETHEUS" / "state" / "dashboard_state.json"
        if not state_file.exists():
            pytest.skip("dashboard_state.json not present (service not running)")
        data = json.loads(state_file.read_text(encoding="utf-8"))
        assert "cards" in data and "state" in data
        updated = datetime.fromisoformat(data["updated_at"])
        age = (datetime.now(timezone.utc) - updated).total_seconds()
        assert age < 300, f"dashboard state is stale ({age:.0f}s old)"

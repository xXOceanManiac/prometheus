"""
tests/acceptance/test_trace_id_propagation.py

Trace IDs must flow from PTT → commit → tool_execute → tool_result so that
every tool call in a session is fully attributable to a specific user turn.

This file verifies:
  1. trace_id format and uniqueness
  2. trace_id set at PTT start, carried through end_audio
  3. tool_execute log carries the same trace_id as the invoking call
  4. tool_result log carries the same trace_id
  5. error events carry trace_id
  6. skipped-turn events carry trace_id
  7. _trace_slug derives a readable suffix from the transcript

All tests are offline.
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_client():
    import realtime_client as rc
    speaker = MagicMock()
    speaker.finish_realtime = MagicMock()
    client = rc.RealtimePrometheusClient(speaker=speaker, tools=MagicMock())
    client.api_key = "test-key"
    client.connected = True
    client.ws = MagicMock()
    return client


TRACE_RE = re.compile(r"^\d{8}-\d{6}-.+$")


# ── Gate: trace_id format and uniqueness ──────────────────────────────────────

class TestTraceIdFormat:
    """make_trace_id() produces correctly formatted, unique IDs."""

    def test_format_is_date_time_slug(self):
        from utils import make_trace_id
        tid = make_trace_id()
        assert TRACE_RE.match(tid), f"unexpected format: {tid!r}"

    def test_contains_date_prefix(self):
        from utils import make_trace_id
        from datetime import date
        tid = make_trace_id()
        today = date.today().strftime("%Y%m%d")
        assert tid.startswith(today), f"trace_id must start with today's date: {tid!r}"

    def test_twenty_ids_are_all_unique(self):
        from utils import make_trace_id
        ids = [make_trace_id() for _ in range(20)]
        assert len(set(ids)) == 20, "duplicate trace IDs detected"

    def test_id_has_three_parts_separated_by_hyphens(self):
        from utils import make_trace_id
        tid = make_trace_id()
        parts = tid.split("-")
        assert len(parts) >= 3, f"expected at least 3 hyphen-separated parts, got: {tid!r}"

    def test_slug_part_is_not_empty(self):
        from utils import make_trace_id
        tid = make_trace_id()
        slug = tid.split("-", 2)[-1]
        assert slug, "slug part of trace_id must not be empty"


# ── Gate: _trace_slug ─────────────────────────────────────────────────────────

class TestTraceSlug:
    """_trace_slug() derives a human-readable 2-word slug from transcript text."""

    def test_slug_from_simple_phrase(self):
        from utils import _trace_slug
        slug = _trace_slug("turn the lights red")
        assert slug, "slug must not be empty"

    def test_slug_is_kebab_case_or_words(self):
        from utils import _trace_slug
        slug = _trace_slug("open spotify")
        # Accept any non-empty alphanumeric-with-hyphens string
        assert re.match(r"^[a-z0-9-]+$", slug), f"unexpected slug format: {slug!r}"

    def test_slug_handles_empty_transcript(self):
        from utils import _trace_slug
        slug = _trace_slug("")
        assert isinstance(slug, str)  # must not raise

    def test_slug_handles_single_word(self):
        from utils import _trace_slug
        slug = _trace_slug("hello")
        assert isinstance(slug, str)

    def test_slug_length_is_reasonable(self):
        from utils import _trace_slug
        slug = _trace_slug("what time is it right now please")
        assert len(slug) <= 40, f"slug too long: {slug!r}"


# ── Gate: trace_id in PTT events ─────────────────────────────────────────────

class TestPTTTraceIdPropagation:
    """user_turn_started, user_turn_committed, and skip events all carry trace_id."""

    def test_ptt_start_sets_trace_id(self, monkeypatch):
        client = _make_client()
        logged = []
        monkeypatch.setattr("realtime_client.log_event", lambda k, p: logged.append((k, p)))

        async def fake_send(d): pass
        client.send = fake_send
        asyncio.run(client.begin_user_turn())

        started = [p for k, p in logged if k == "user_turn_started"]
        assert started, "user_turn_started not logged"
        assert started[0].get("trace_id"), "user_turn_started must carry trace_id"

    def test_ptt_start_trace_id_matches_format(self, monkeypatch):
        client = _make_client()
        logged = []
        monkeypatch.setattr("realtime_client.log_event", lambda k, p: logged.append((k, p)))

        async def fake_send(d): pass
        client.send = fake_send
        asyncio.run(client.begin_user_turn())

        started = [p for k, p in logged if k == "user_turn_started"]
        if started:
            assert TRACE_RE.match(started[0]["trace_id"]), \
                f"bad trace_id format: {started[0]['trace_id']!r}"

    def test_end_audio_short_buffer_skip_carries_trace_id(self, monkeypatch):
        client = _make_client()
        client.awaiting_user_audio = True
        client._audio_bytes_since_commit = 0  # too short → skip
        client._current_trace_id = "20260608-120000-test-skip-xx01"
        client._response_active = False

        logged = []
        monkeypatch.setattr("realtime_client.log_event", lambda k, p: logged.append((k, p)))

        async def fake_send(d): pass
        client.send = fake_send
        asyncio.run(client.end_audio())

        skip_events = [p for k, p in logged if k == "user_turn_commit_skipped"]
        assert skip_events, "user_turn_commit_skipped not logged"
        assert skip_events[0]["trace_id"] == "20260608-120000-test-skip-xx01"

    def test_end_audio_stt_attempt_carries_trace_id(self, monkeypatch):
        """With sufficient audio, end_audio logs user_turn_commit_attempt with the current trace_id.
        Pass 12: _response_active is not checked in end_audio — STT fires regardless."""
        client = _make_client()
        client.awaiting_user_audio = True
        client._audio_bytes_since_commit = 9999
        client._captured_audio = bytearray(b"\x00" * 9999)
        client._current_trace_id = "20260608-120000-test-active-xx02"
        client._response_active = True  # no longer blocks end_audio in Pass 12

        logged = []
        monkeypatch.setattr("realtime_client.log_event", lambda k, p: logged.append((k, p)))

        async def fake_send(d): pass
        client.send = fake_send
        with patch.object(client, "_transcribe_ptt", new_callable=AsyncMock):
            asyncio.run(client.end_audio())

        attempt = [p for k, p in logged if k == "user_turn_commit_attempt"]
        assert attempt, "user_turn_commit_attempt not logged"
        assert attempt[0]["trace_id"] == "20260608-120000-test-active-xx02"

    def test_end_audio_commit_attempt_carries_trace_id(self, monkeypatch):
        """user_turn_commit_attempt must carry the current trace_id.
        Pass 12: replaces the old user_turn_committed (Realtime buffer commit) event."""
        client = _make_client()
        client.awaiting_user_audio = True
        client._audio_bytes_since_commit = 9999
        client._captured_audio = bytearray(b"\x00" * 9999)
        client._response_active = False
        client._current_trace_id = "20260608-120000-test-commit-xx03"

        logged = []
        monkeypatch.setattr("realtime_client.log_event", lambda k, p: logged.append((k, p)))

        async def fake_send(d): pass
        client.send = fake_send
        with patch.object(client, "_transcribe_ptt", new_callable=AsyncMock):
            asyncio.run(client.end_audio())

        attempt = [p for k, p in logged if k == "user_turn_commit_attempt"]
        assert attempt, "user_turn_commit_attempt not logged"
        assert attempt[0]["trace_id"] == "20260608-120000-test-commit-xx03"
        assert attempt[0].get("stt_mode") == "standalone"


# ── Gate: trace_id flows through tool execution ───────────────────────────────

class TestToolExecutionTraceIdPropagation:
    """tool_execute and tool_result log events must carry the same trace_id."""

    def test_tool_execute_log_has_trace_id(self, monkeypatch):
        from tools import ToolRegistry
        logged = []
        monkeypatch.setattr("tools.log_event", lambda k, p: logged.append((k, p)))
        r = ToolRegistry()
        r.execute({"action": "tell_time"}, trace_id="20260608-120000-ttest-xx04")

        exec_events = [p for k, p in logged if k == "tool_execute"]
        assert exec_events, "tool_execute not logged"
        assert exec_events[0]["trace_id"] == "20260608-120000-ttest-xx04"

    def test_tool_result_log_has_trace_id(self, monkeypatch):
        from tools import ToolRegistry
        logged = []
        monkeypatch.setattr("tools.log_event", lambda k, p: logged.append((k, p)))
        r = ToolRegistry()
        r.execute({"action": "tell_time"}, trace_id="20260608-120000-ttest-xx05")

        result_events = [p for k, p in logged if k == "tool_result"]
        assert result_events, "tool_result not logged"
        assert result_events[0]["trace_id"] == "20260608-120000-ttest-xx05"

    def test_execute_and_result_trace_ids_match(self, monkeypatch):
        from tools import ToolRegistry
        logged = []
        monkeypatch.setattr("tools.log_event", lambda k, p: logged.append((k, p)))
        r = ToolRegistry()
        tid = "20260608-120000-ttest-match-xx06"
        r.execute({"action": "tell_time"}, trace_id=tid)

        exec_id = next((p["trace_id"] for k, p in logged if k == "tool_execute"), None)
        result_id = next((p["trace_id"] for k, p in logged if k == "tool_result"), None)
        assert exec_id == tid
        assert result_id == tid

    def test_no_trace_id_fallback_is_string(self, monkeypatch):
        from tools import ToolRegistry
        logged = []
        monkeypatch.setattr("tools.log_event", lambda k, p: logged.append((k, p)))
        r = ToolRegistry()
        r.execute({"action": "tell_time"})  # no trace_id kwarg

        exec_events = [p for k, p in logged if k == "tool_execute"]
        assert exec_events
        assert isinstance(exec_events[0].get("trace_id"), str), \
            "trace_id must always be a string even without explicit caller value"

    def test_trace_id_in_tool_execute_log_has_payload(self, monkeypatch):
        from tools import ToolRegistry
        logged = []
        monkeypatch.setattr("tools.log_event", lambda k, p: logged.append((k, p)))
        r = ToolRegistry()
        r.execute({"action": "tell_time"}, trace_id="20260608-120000-action-xx07")

        exec_events = [p for k, p in logged if k == "tool_execute"]
        assert exec_events, "tool_execute must be logged"
        # The payload carrying the action is logged inside the event
        ev = exec_events[0]
        action_in_payload = (
            ev.get("action") == "tell_time"
            or (isinstance(ev.get("payload"), dict) and ev["payload"].get("action") == "tell_time")
        )
        assert action_in_payload, \
            f"tool_execute must log the action name (directly or in payload): {ev!r}"


# ── Gate: error events carry trace_id ─────────────────────────────────────────

class TestErrorEventsCarryTraceId:
    """Errors logged during tool execution and Realtime API must include trace_id."""

    def test_tool_failure_logged_with_trace_id(self, monkeypatch):
        from tools import ToolRegistry
        logged = []
        monkeypatch.setattr("tools.log_event", lambda k, p: logged.append((k, p)))
        r = ToolRegistry()
        r.execute({"action": "_nonexistent_abc"}, trace_id="20260608-120000-err-xx08")

        result_events = [p for k, p in logged if k == "tool_result"]
        assert result_events, "tool_result must be logged even on failure"
        assert result_events[0]["trace_id"] == "20260608-120000-err-xx08"

    def test_tool_result_log_includes_status(self, monkeypatch):
        from tools import ToolRegistry, ToolStatus
        logged = []
        monkeypatch.setattr("tools.log_event", lambda k, p: logged.append((k, p)))
        r = ToolRegistry()
        r.execute({"action": "tell_time"}, trace_id="20260608-120000-stat-xx09")

        result_events = [p for k, p in logged if k == "tool_result"]
        assert result_events
        assert "status" in result_events[0] or "verified" in result_events[0], \
            "tool_result log must include status or verified field"


# ── Gate: trace ID unique per turn ────────────────────────────────────────────

class TestTraceIdUniquePerTurn:
    """Each PTT press gets a fresh trace ID. Two consecutive presses get different IDs."""

    def test_two_begin_user_turn_calls_get_different_trace_ids(self, monkeypatch):
        client = _make_client()
        seen_ids = []

        call_count = [0]
        def fake_make_trace_id():
            call_count[0] += 1
            return f"fake-{call_count[0]}-unique"

        monkeypatch.setattr("realtime_client.make_trace_id", fake_make_trace_id)
        monkeypatch.setattr("realtime_client.log_event",
                            lambda k, p: seen_ids.append(p.get("trace_id"))
                            if k == "user_turn_started" else None)

        async def fake_send(d): pass
        client.send = fake_send
        asyncio.run(client.begin_user_turn())
        asyncio.run(client.begin_user_turn())

        assert len(seen_ids) == 2
        assert seen_ids[0] != seen_ids[1], "consecutive turns must have distinct trace IDs"

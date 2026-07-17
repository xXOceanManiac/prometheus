"""Tests for per-request trace ID generation and propagation."""
from __future__ import annotations

import re
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# make_trace_id — format and uniqueness
# ---------------------------------------------------------------------------

class TestMakeTraceId:
    def test_format_matches_pattern(self):
        from prometheus.infra.utils import make_trace_id
        tid = make_trace_id()
        assert re.match(r"^\d{8}-\d{6}-[a-z0-9]{4}$", tid), f"bad format: {tid!r}"

    def test_timestamp_part_is_numeric(self):
        from prometheus.infra.utils import make_trace_id
        tid = make_trace_id()
        date_part, time_part, _ = tid.split("-", 2)
        assert date_part.isdigit() and len(date_part) == 8
        assert time_part.isdigit() and len(time_part) == 6

    def test_random_suffix_is_4_chars(self):
        from prometheus.infra.utils import make_trace_id
        tid = make_trace_id()
        suffix = tid.rsplit("-", 1)[-1]
        assert len(suffix) == 4
        assert re.match(r"^[a-z0-9]+$", suffix)

    def test_consecutive_ids_are_unique(self):
        from prometheus.infra.utils import make_trace_id
        ids = [make_trace_id() for _ in range(50)]
        assert len(set(ids)) == len(ids), "duplicate trace IDs generated"

    def test_id_is_string(self):
        from prometheus.infra.utils import make_trace_id
        assert isinstance(make_trace_id(), str)


# ---------------------------------------------------------------------------
# _trace_slug — slug derivation
# ---------------------------------------------------------------------------

class TestTraceSlug:
    def test_basic_two_word_slug(self):
        from prometheus.infra.utils import _trace_slug
        assert _trace_slug("turn the lights red") == "turn-lights"

    def test_stopwords_filtered(self):
        from prometheus.infra.utils import _trace_slug
        # "open" and "spotify" are not stopwords — should appear
        result = _trace_slug("open Spotify on Xbox")
        assert "open" in result or "spotify" in result

    def test_empty_string_returns_empty(self):
        from prometheus.infra.utils import _trace_slug
        assert _trace_slug("") == ""

    def test_all_stopwords_returns_empty(self):
        from prometheus.infra.utils import _trace_slug
        assert _trace_slug("a an the to in") == ""

    def test_short_words_filtered(self):
        from prometheus.infra.utils import _trace_slug
        # single-char words are filtered (length > 1 check)
        result = _trace_slug("i a the hello world")
        assert "hello" in result or "world" in result

    def test_max_words_respected(self):
        from prometheus.infra.utils import _trace_slug
        result = _trace_slug("open google chrome browser now please", max_words=2)
        assert result.count("-") <= 1  # at most 2 words joined = 1 dash


# ---------------------------------------------------------------------------
# ToolRegistry.execute — trace_id in tool_execute and tool_result logs
# ---------------------------------------------------------------------------

class TestToolExecuteTraceId:
    def _make_registry(self):
        from prometheus.execution.tools import ToolRegistry
        r = ToolRegistry()
        return r

    def test_tool_execute_log_includes_trace_id(self, tmp_path, monkeypatch):
        from prometheus.execution.tools import ToolRegistry
        logged = []
        monkeypatch.setattr("prometheus.execution.tools.log_event", lambda kind, payload: logged.append((kind, payload)))

        r = ToolRegistry()
        r.execute({"action": "get_time"}, trace_id="20260607-221405-test-f3a9")

        tool_execute_events = [p for k, p in logged if k == "tool_execute"]
        assert tool_execute_events, "tool_execute event not logged"
        assert tool_execute_events[0].get("trace_id") == "20260607-221405-test-f3a9"

    def test_tool_result_log_includes_trace_id(self, monkeypatch):
        from prometheus.execution.tools import ToolRegistry
        logged = []
        monkeypatch.setattr("prometheus.execution.tools.log_event", lambda kind, payload: logged.append((kind, payload)))

        r = ToolRegistry()
        r.execute({"action": "get_time"}, trace_id="20260607-221405-get-time-ab12")

        tool_result_events = [p for k, p in logged if k == "tool_result"]
        assert tool_result_events, "tool_result event not logged"
        ev = tool_result_events[0]
        assert ev.get("trace_id") == "20260607-221405-get-time-ab12"
        assert "action" in ev
        assert "ok" in ev
        assert "message" in ev
        assert "data_keys" in ev

    def test_tool_result_no_secrets_in_data_keys(self, monkeypatch):
        from prometheus.execution.tools import ToolRegistry
        logged = []
        monkeypatch.setattr("prometheus.execution.tools.log_event", lambda kind, payload: logged.append((kind, payload)))

        r = ToolRegistry()
        r.execute({"action": "get_time"}, trace_id="test-trace")

        for k, p in logged:
            if k == "tool_result":
                # data_keys must be a list of strings, not the actual data values
                assert isinstance(p.get("data_keys"), list)
                for key in p["data_keys"]:
                    assert isinstance(key, str)

    def test_empty_trace_id_still_works(self, monkeypatch):
        from prometheus.execution.tools import ToolRegistry
        logged = []
        monkeypatch.setattr("prometheus.execution.tools.log_event", lambda kind, payload: logged.append((kind, payload)))

        r = ToolRegistry()
        r.execute({"action": "get_time"})  # no trace_id — backward compat

        tool_execute_events = [p for k, p in logged if k == "tool_execute"]
        assert tool_execute_events
        # trace_id present but empty string — no crash
        assert tool_execute_events[0].get("trace_id") == ""

    def test_no_action_emits_tool_result(self, monkeypatch):
        from prometheus.execution.tools import ToolRegistry
        logged = []
        monkeypatch.setattr("prometheus.execution.tools.log_event", lambda kind, payload: logged.append((kind, payload)))

        r = ToolRegistry()
        r.execute({}, trace_id="trace-no-action-xyz1")

        result_events = [p for k, p in logged if k == "tool_result"]
        assert result_events, "tool_result not logged for no-action case"
        assert result_events[0]["trace_id"] == "trace-no-action-xyz1"
        assert result_events[0]["ok"] is False

    def test_tool_result_message_truncated(self, monkeypatch):
        from prometheus.execution.tools import ToolRegistry
        logged = []
        monkeypatch.setattr("prometheus.execution.tools.log_event", lambda kind, payload: logged.append((kind, payload)))

        r = ToolRegistry()
        r.execute({"action": "get_time"}, trace_id="t1")

        for k, p in logged:
            if k == "tool_result":
                assert len(p.get("message", "")) <= 200


# ---------------------------------------------------------------------------
# Trace_id slug refinement — logic unit test (no realtime connection needed)
# ---------------------------------------------------------------------------

class TestTraceIdSlugRefinement:
    """Test the slug-refinement logic that runs at transcript time."""

    def _refine(self, current_trace_id: str, transcript: str) -> str:
        """Replicate the slug-refinement logic from realtime_client.py."""
        from prometheus.infra.utils import _trace_slug
        slug = _trace_slug(transcript)
        if slug and current_trace_id:
            parts = current_trace_id.rsplit("-", 1)
            if len(parts) == 2:
                return f"{parts[0]}-{slug}-{parts[1]}"
        return current_trace_id

    def test_slug_inserted_before_rnd_suffix(self):
        refined = self._refine("20260607-221405-f3a9", "turn lights red")
        assert refined.endswith("-f3a9")
        assert "lights" in refined or "turn" in refined

    def test_empty_transcript_leaves_trace_id_unchanged(self):
        tid = "20260607-221405-f3a9"
        assert self._refine(tid, "") == tid

    def test_stopword_only_transcript_leaves_unchanged(self):
        tid = "20260607-221405-f3a9"
        assert self._refine(tid, "a the and") == tid

    def test_rnd_suffix_preserved_as_grep_key(self):
        refined = self._refine("20260607-221405-ab12", "open spotify")
        assert "ab12" in refined

    def test_empty_trace_id_not_modified(self):
        assert self._refine("", "turn lights red") == ""


# ---------------------------------------------------------------------------
# Legacy log compatibility — logs without trace_id must not crash anything
# ---------------------------------------------------------------------------

class TestLegacyLogCompat:
    def test_log_event_without_trace_id_does_not_raise(self, tmp_path):
        from prometheus.infra.utils import log_event
        from prometheus.infra.config import LOG_DIR
        # Legacy event — no trace_id field — must succeed silently
        log_event("legacy_test_event", {"some_field": "some_value"})

    def test_tool_execute_with_no_trace_id_arg(self, monkeypatch):
        from prometheus.execution.tools import ToolRegistry
        logged = []
        monkeypatch.setattr("prometheus.execution.tools.log_event", lambda kind, payload: logged.append((kind, payload)))
        r = ToolRegistry()
        # Old call style without trace_id — must not raise
        result = r.execute({"action": "get_time"})
        assert result is not None

    def test_tool_result_log_always_present(self, monkeypatch):
        """tool_result must be logged for every execute() call regardless of trace_id."""
        from prometheus.execution.tools import ToolRegistry
        logged = []
        monkeypatch.setattr("prometheus.execution.tools.log_event", lambda kind, payload: logged.append((kind, payload)))
        r = ToolRegistry()
        r.execute({"action": "get_time"})

        kinds = [k for k, _ in logged]
        assert "tool_result" in kinds

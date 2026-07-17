"""
tests/test_calendar_read_tools.py — Unit tests for read-only calendar tools.

All tests use mocks/fake data. No live Google Calendar API calls.
No OAuth browser flow. No Home Assistant calls. No subprocess execution.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from prometheus.agents.calendar_read_tools import (
    calendar_find_free_blocks,
    calendar_get_date,
    calendar_get_today,
    calendar_get_tomorrow,
    calendar_list_upcoming,
    calendar_next_event,
    calendar_summarize_day,
    _is_all_day,
    _event_to_dict,
    _get_local_tz,
)
from prometheus.integrations.google_calendar import (
    GoogleCalendarConfig,
    GoogleCalendarEvent,
)


# ── Fixtures and helpers ───────────────────────────────────────────────────────

def _cfg(enabled: bool = True) -> GoogleCalendarConfig:
    return GoogleCalendarConfig(
        enabled=enabled,
        dry_run=True,
        default_calendar_id="primary",
        credentials_path="/tmp/fake_creds.json",
        token_path="/tmp/fake_token.json",
        timezone="America/New_York",
    )


def _timed_event(title: str, start_hour: int, end_hour: int, d: date | None = None) -> GoogleCalendarEvent:
    day = (d or date.today()).isoformat()
    return GoogleCalendarEvent(
        event_id=f"ev_{title.lower().replace(' ', '_')}",
        calendar_id="primary",
        title=title,
        start_time=f"{day}T{start_hour:02d}:00:00-04:00",
        end_time=f"{day}T{end_hour:02d}:00:00-04:00",
        location=None,
        description=None,
        html_link=None,
        raw={},
    )


def _all_day_event(title: str, d: date | None = None) -> GoogleCalendarEvent:
    day = (d or date.today()).isoformat()
    return GoogleCalendarEvent(
        event_id=f"ev_allday_{title.lower().replace(' ', '_')}",
        calendar_id="primary",
        title=title,
        start_time=day,
        end_time=day,
        location=None,
        description=None,
        html_link=None,
        raw={},
    )


_ENV_ENABLED = {
    "GOOGLE_CALENDAR_ENABLED": "true",
    "GOOGLE_CALENDAR_CREDENTIALS_PATH": "/tmp/fake_creds.json",
    "GOOGLE_CALENDAR_TOKEN_PATH": "/tmp/fake_token.json",
}

_ENV_DISABLED = {"GOOGLE_CALENDAR_ENABLED": "false"}


def _patch_service_and_events(events: list[GoogleCalendarEvent]):
    """Context manager stack for patching service build + list_calendar_events."""
    return (
        patch("prometheus.agents.calendar_read_tools.build_google_calendar_service"),
        patch("prometheus.agents.calendar_read_tools.list_calendar_events", return_value=events),
        patch.dict(os.environ, _ENV_ENABLED, clear=False),
    )


# ── _is_all_day ───────────────────────────────────────────────────────────────

class TestIsAllDay:
    def test_date_only_is_all_day(self):
        e = _all_day_event("Holiday")
        assert _is_all_day(e) is True

    def test_datetime_is_not_all_day(self):
        e = _timed_event("Meeting", 9, 10)
        assert _is_all_day(e) is False

    def test_empty_start_time_is_not_all_day(self):
        e = GoogleCalendarEvent(
            event_id="x", calendar_id="primary", title="?",
            start_time="", end_time=None, location=None,
            description=None, html_link=None, raw={},
        )
        assert _is_all_day(e) is False


# ── _get_local_tz ─────────────────────────────────────────────────────────────

class TestGetLocalTz:
    def test_valid_timezone(self):
        cfg = _cfg()
        cfg = GoogleCalendarConfig(timezone="America/New_York")
        tz = _get_local_tz(cfg)
        assert tz.key == "America/New_York"

    def test_invalid_timezone_falls_back(self):
        cfg = GoogleCalendarConfig(timezone="Invalid/Zone_XYZ")
        tz = _get_local_tz(cfg)
        assert tz.key == "America/New_York"

    def test_empty_timezone_falls_back(self):
        cfg = GoogleCalendarConfig(timezone="")
        tz = _get_local_tz(cfg)
        assert tz.key == "America/New_York"


# ── Disabled calendar ─────────────────────────────────────────────────────────

class TestDisabledCalendar:
    def _check_disabled(self, result: dict) -> None:
        assert isinstance(result, dict)
        assert result.get("ok") is False
        assert "error" in result or "calendar_enabled" in result

    def test_get_today_disabled(self):
        with patch.dict(os.environ, _ENV_DISABLED, clear=False):
            self._check_disabled(calendar_get_today())

    def test_get_tomorrow_disabled(self):
        with patch.dict(os.environ, _ENV_DISABLED, clear=False):
            self._check_disabled(calendar_get_tomorrow())

    def test_next_event_disabled(self):
        with patch.dict(os.environ, _ENV_DISABLED, clear=False):
            self._check_disabled(calendar_next_event())

    def test_list_upcoming_disabled(self):
        with patch.dict(os.environ, _ENV_DISABLED, clear=False):
            self._check_disabled(calendar_list_upcoming())

    def test_get_date_disabled(self):
        with patch.dict(os.environ, _ENV_DISABLED, clear=False):
            self._check_disabled(calendar_get_date("2026-01-01"))

    def test_summarize_day_disabled(self):
        with patch.dict(os.environ, _ENV_DISABLED, clear=False):
            self._check_disabled(calendar_summarize_day())

    def test_find_free_blocks_disabled(self):
        with patch.dict(os.environ, _ENV_DISABLED, clear=False):
            self._check_disabled(calendar_find_free_blocks("2026-01-01"))


# ── Auth failure ──────────────────────────────────────────────────────────────

class TestAuthFailure:
    def test_get_today_auth_failure(self):
        with patch("prometheus.agents.calendar_read_tools.build_google_calendar_service",
                   side_effect=ValueError("Token invalid")), \
             patch.dict(os.environ, _ENV_ENABLED, clear=False):
            r = calendar_get_today()
            assert r.get("ok") is False
            assert "error" in r


# ── calendar_get_today ────────────────────────────────────────────────────────

class TestCalendarGetToday:
    def test_correct_date_returned(self):
        with patch("prometheus.agents.calendar_read_tools.build_google_calendar_service"), \
             patch("prometheus.agents.calendar_read_tools.list_calendar_events", return_value=[]), \
             patch.dict(os.environ, _ENV_ENABLED, clear=False):
            r = calendar_get_today()
            assert r["ok"] is True
            assert r["date"] == date.today().isoformat()

    def test_returns_events_list(self):
        events = [_timed_event("Standup", 9, 10), _timed_event("Lunch", 12, 13)]
        with patch("prometheus.agents.calendar_read_tools.build_google_calendar_service"), \
             patch("prometheus.agents.calendar_read_tools.list_calendar_events", return_value=events), \
             patch.dict(os.environ, _ENV_ENABLED, clear=False):
            r = calendar_get_today()
            assert r["count"] == 2
            assert len(r["events"]) == 2

    def test_output_is_json_serializable(self):
        events = [_timed_event("Meeting", 10, 11)]
        with patch("prometheus.agents.calendar_read_tools.build_google_calendar_service"), \
             patch("prometheus.agents.calendar_read_tools.list_calendar_events", return_value=events), \
             patch.dict(os.environ, _ENV_ENABLED, clear=False):
            r = calendar_get_today()
            json.dumps(r)  # must not raise

    def test_list_calendar_events_called_with_today_window(self):
        with patch("prometheus.agents.calendar_read_tools.build_google_calendar_service"), \
             patch("prometheus.agents.calendar_read_tools.list_calendar_events", return_value=[]) as mock_list, \
             patch.dict(os.environ, _ENV_ENABLED, clear=False):
            calendar_get_today()
            assert mock_list.called
            _, kwargs = mock_list.call_args
            time_min = kwargs.get("time_min") or mock_list.call_args[0][2] if len(mock_list.call_args[0]) > 2 else ""
            # time_min should start with today's date
            assert date.today().isoformat()[:7] in str(mock_list.call_args)


# ── calendar_get_tomorrow ─────────────────────────────────────────────────────

class TestCalendarGetTomorrow:
    def test_correct_date_returned(self):
        with patch("prometheus.agents.calendar_read_tools.build_google_calendar_service"), \
             patch("prometheus.agents.calendar_read_tools.list_calendar_events", return_value=[]), \
             patch.dict(os.environ, _ENV_ENABLED, clear=False):
            r = calendar_get_tomorrow()
            expected = (date.today() + timedelta(days=1)).isoformat()
            assert r["ok"] is True
            assert r["date"] == expected

    def test_output_is_json_serializable(self):
        with patch("prometheus.agents.calendar_read_tools.build_google_calendar_service"), \
             patch("prometheus.agents.calendar_read_tools.list_calendar_events", return_value=[]), \
             patch.dict(os.environ, _ENV_ENABLED, clear=False):
            r = calendar_get_tomorrow()
            json.dumps(r)

    def test_returns_empty_events_when_none(self):
        with patch("prometheus.agents.calendar_read_tools.build_google_calendar_service"), \
             patch("prometheus.agents.calendar_read_tools.list_calendar_events", return_value=[]), \
             patch.dict(os.environ, _ENV_ENABLED, clear=False):
            r = calendar_get_tomorrow()
            assert r["count"] == 0
            assert r["events"] == []


# ── calendar_get_date ─────────────────────────────────────────────────────────

class TestCalendarGetDate:
    def test_valid_date_returns_ok(self):
        with patch("prometheus.agents.calendar_read_tools.build_google_calendar_service"), \
             patch("prometheus.agents.calendar_read_tools.list_calendar_events", return_value=[]), \
             patch.dict(os.environ, _ENV_ENABLED, clear=False):
            r = calendar_get_date("2026-06-15")
            assert r["ok"] is True
            assert r["date"] == "2026-06-15"

    def test_invalid_format_returns_error(self):
        r = calendar_get_date("June 15 2026")
        assert r["ok"] is False
        assert "error" in r

    def test_nonsense_string_returns_error(self):
        r = calendar_get_date("not-a-date")
        assert r["ok"] is False

    def test_empty_string_returns_error(self):
        r = calendar_get_date("")
        assert r["ok"] is False

    def test_none_type_returns_error(self):
        r = calendar_get_date(None)  # type: ignore[arg-type]
        assert r["ok"] is False

    def test_output_is_json_serializable(self):
        with patch("prometheus.agents.calendar_read_tools.build_google_calendar_service"), \
             patch("prometheus.agents.calendar_read_tools.list_calendar_events", return_value=[]), \
             patch.dict(os.environ, _ENV_ENABLED, clear=False):
            r = calendar_get_date("2026-06-15")
            json.dumps(r)


# ── calendar_next_event ───────────────────────────────────────────────────────

class TestCalendarNextEvent:
    def test_returns_first_timed_event(self):
        events = [_timed_event("Morning Meeting", 9, 10), _timed_event("Lunch", 12, 13)]
        with patch("prometheus.agents.calendar_read_tools.build_google_calendar_service"), \
             patch("prometheus.agents.calendar_read_tools.list_calendar_events", return_value=events), \
             patch.dict(os.environ, _ENV_ENABLED, clear=False):
            r = calendar_next_event()
            assert r["ok"] is True
            assert r["has_next_timed"] is True
            assert r["next_timed_event"]["title"] == "Morning Meeting"

    def test_all_day_events_not_treated_as_next_timed(self):
        events = [
            _all_day_event("Holiday"),
            _timed_event("Dentist", 14, 15),
        ]
        with patch("prometheus.agents.calendar_read_tools.build_google_calendar_service"), \
             patch("prometheus.agents.calendar_read_tools.list_calendar_events", return_value=events), \
             patch.dict(os.environ, _ENV_ENABLED, clear=False):
            r = calendar_next_event()
            assert r["has_next_timed"] is True
            assert r["next_timed_event"]["title"] == "Dentist"

    def test_all_day_only_returns_no_timed(self):
        events = [_all_day_event("Holiday")]
        with patch("prometheus.agents.calendar_read_tools.build_google_calendar_service"), \
             patch("prometheus.agents.calendar_read_tools.list_calendar_events", return_value=events), \
             patch.dict(os.environ, _ENV_ENABLED, clear=False):
            r = calendar_next_event()
            assert r["has_next_timed"] is False
            assert r["next_timed_event"] is None

    def test_no_events_returns_gracefully(self):
        with patch("prometheus.agents.calendar_read_tools.build_google_calendar_service"), \
             patch("prometheus.agents.calendar_read_tools.list_calendar_events", return_value=[]), \
             patch.dict(os.environ, _ENV_ENABLED, clear=False):
            r = calendar_next_event()
            assert r["ok"] is True
            assert r["has_next_timed"] is False
            assert r["next_timed_event"] is None

    def test_todays_all_day_events_included(self):
        today = date.today()
        events = [
            _all_day_event("Holiday", today),
            _timed_event("Meeting", 14, 15, today),
        ]
        with patch("prometheus.agents.calendar_read_tools.build_google_calendar_service"), \
             patch("prometheus.agents.calendar_read_tools.list_calendar_events", return_value=events), \
             patch.dict(os.environ, _ENV_ENABLED, clear=False):
            r = calendar_next_event()
            assert any(e["title"] == "Holiday" for e in r["todays_all_day_events"])

    def test_output_is_json_serializable(self):
        events = [_timed_event("Meeting", 10, 11)]
        with patch("prometheus.agents.calendar_read_tools.build_google_calendar_service"), \
             patch("prometheus.agents.calendar_read_tools.list_calendar_events", return_value=events), \
             patch.dict(os.environ, _ENV_ENABLED, clear=False):
            r = calendar_next_event()
            json.dumps(r)


# ── calendar_summarize_day ────────────────────────────────────────────────────

class TestCalendarSummarizeDay:
    def _run_with_events(self, events: list, d: str | None = None) -> dict:
        with patch("prometheus.agents.calendar_read_tools.build_google_calendar_service"), \
             patch("prometheus.agents.calendar_read_tools.list_calendar_events", return_value=events), \
             patch.dict(os.environ, _ENV_ENABLED, clear=False):
            return calendar_summarize_day(d)

    def test_has_all_required_fields(self):
        r = self._run_with_events([])
        required = {"ok", "date", "event_count", "all_day_events", "timed_events",
                    "first_timed_event", "last_timed_event", "summary"}
        assert required.issubset(r.keys()), f"Missing keys: {required - set(r.keys())}"

    def test_correct_date_default(self):
        r = self._run_with_events([])
        assert r["date"] == date.today().isoformat()

    def test_correct_date_explicit(self):
        r = self._run_with_events([], "2026-07-04")
        assert r["date"] == "2026-07-04"

    def test_invalid_date_returns_error(self):
        r = self._run_with_events([], "not-a-date")
        assert r["ok"] is False

    def test_empty_day(self):
        r = self._run_with_events([])
        assert r["event_count"] == 0
        assert r["first_timed_event"] is None
        assert r["last_timed_event"] is None
        assert "no events" in r["summary"].lower()

    def test_single_timed_event(self):
        events = [_timed_event("Dentist", 10, 11)]
        r = self._run_with_events(events)
        assert r["event_count"] == 1
        assert r["first_timed_event"]["title"] == "Dentist"
        assert r["last_timed_event"]["title"] == "Dentist"

    def test_multiple_timed_events_first_last(self):
        events = [
            _timed_event("Meeting", 9, 10),
            _timed_event("Lunch", 12, 13),
            _timed_event("Review", 15, 16),
        ]
        r = self._run_with_events(events)
        assert r["first_timed_event"]["title"] == "Meeting"
        assert r["last_timed_event"]["title"] == "Review"

    def test_all_day_counted_separately(self):
        events = [_all_day_event("Holiday"), _timed_event("Call", 10, 11)]
        r = self._run_with_events(events)
        assert len(r["all_day_events"]) == 1
        assert len(r["timed_events"]) == 1

    def test_summary_is_deterministic_string(self):
        events = [_timed_event("Call", 10, 11)]
        r1 = self._run_with_events(events)
        r2 = self._run_with_events(events)
        assert r1["summary"] == r2["summary"]

    def test_output_is_json_serializable(self):
        events = [_timed_event("Call", 10, 11), _all_day_event("Holiday")]
        r = self._run_with_events(events)
        json.dumps(r)


# ── calendar_find_free_blocks ─────────────────────────────────────────────────

class TestCalendarFindFreeBlocks:
    def _run(self, events: list, min_min: int = 60) -> dict:
        today = date.today().isoformat()
        with patch("prometheus.agents.calendar_read_tools.build_google_calendar_service"), \
             patch("prometheus.agents.calendar_read_tools.list_calendar_events", return_value=events), \
             patch.dict(os.environ, _ENV_ENABLED, clear=False):
            return calendar_find_free_blocks(today, minimum_minutes=min_min)

    def test_invalid_date_returns_error(self):
        r = calendar_find_free_blocks("not-a-date")
        assert r["ok"] is False

    def test_empty_date_returns_error(self):
        r = calendar_find_free_blocks("")
        assert r["ok"] is False

    def test_no_events_returns_full_day_as_free(self):
        r = self._run([])
        assert r["ok"] is True
        assert r["free_block_count"] >= 1
        total_free = sum(b["duration_minutes"] for b in r["free_blocks"])
        assert total_free >= 60  # at least 1 hour free in 08:00–22:00 window

    def test_all_day_events_ignored_for_free_blocks(self):
        events = [_all_day_event("Holiday")]
        r = self._run(events)
        assert r["ok"] is True
        assert r["busy_event_count"] == 0

    def test_finds_gaps_between_events(self):
        events = [
            _timed_event("Morning", 9, 10),
            _timed_event("Afternoon", 14, 15),
        ]
        r = self._run(events)
        assert r["free_block_count"] >= 1
        # Gap between 10:00 and 14:00 is 240 minutes — should appear
        long_gap = any(b["duration_minutes"] >= 60 for b in r["free_blocks"])
        assert long_gap

    def test_no_free_blocks_when_fully_booked(self):
        today = date.today()
        events = [
            _timed_event("Block1", 8, 12, today),
            _timed_event("Block2", 12, 18, today),
            _timed_event("Block3", 18, 22, today),
        ]
        r = self._run(events, min_min=60)
        assert r["ok"] is True
        assert r["free_block_count"] == 0

    def test_minimum_minutes_filters_short_blocks(self):
        events = [
            _timed_event("MeetingA", 8, 10),
            _timed_event("MeetingB", 10, 30, date.today()),
        ]
        r = self._run(events, min_min=120)
        for block in r["free_blocks"]:
            assert block["duration_minutes"] >= 120

    def test_output_has_required_keys(self):
        r = self._run([])
        assert "ok" in r
        assert "date" in r
        assert "minimum_minutes" in r
        assert "free_block_count" in r
        assert "free_blocks" in r

    def test_output_is_json_serializable(self):
        events = [_timed_event("Meeting", 10, 11)]
        r = self._run(events)
        json.dumps(r)

    def test_free_block_start_end_present(self):
        r = self._run([])
        for block in r["free_blocks"]:
            assert "start" in block
            assert "end" in block
            assert "duration_minutes" in block


# ── No write API calls ────────────────────────────────────────────────────────

class TestNoWriteCalls:
    """Verify that no create/update/delete calendar API calls are made."""

    def test_no_create_calls(self):
        import prometheus.agents.calendar_read_tools as _m
        import inspect
        src = inspect.getsource(_m)
        assert "create_calendar_event" not in src
        assert "service.events().insert" not in src

    def test_no_update_calls(self):
        import prometheus.agents.calendar_read_tools as _m
        import inspect
        src = inspect.getsource(_m)
        assert "update_calendar_event" not in src
        assert "service.events().patch" not in src

    def test_no_delete_calls(self):
        import prometheus.agents.calendar_read_tools as _m
        import inspect
        src = inspect.getsource(_m)
        assert "delete_calendar_event" not in src
        assert "service.events().delete" not in src


# ── No Home Assistant calls ───────────────────────────────────────────────────

class TestNoHomeAssistantCalls:
    def test_no_ha_references_in_module(self):
        import prometheus.agents.calendar_read_tools as _m
        import inspect
        src = inspect.getsource(_m)
        assert "HOME_ASSISTANT" not in src
        assert "run_ha_script" not in src
        assert "homeassistant" not in src.lower()


# ── No subprocess/shell ───────────────────────────────────────────────────────

class TestNoSubprocess:
    def test_no_subprocess_in_module(self):
        import prometheus.agents.calendar_read_tools as _m
        import inspect
        src = inspect.getsource(_m)
        assert "import subprocess" not in src
        assert "os.system(" not in src
        assert "import shlex" not in src


# ── Intent override routing ───────────────────────────────────────────────────

class TestCalendarIntentOverrides:
    def setup_method(self):
        from prometheus.core.intent_overrides import resolve_direct_intent
        self.resolve = resolve_direct_intent

    def _action(self, phrase: str) -> str | None:
        result = self.resolve(phrase)
        if result is None:
            return None
        return result.get("payload", {}).get("action")

    def test_today_phrases_route_to_get_today(self):
        phrases = [
            "what's on my calendar today",
            "what do i have today",
            "my schedule today",
        ]
        for phrase in phrases:
            action = self._action(phrase)
            assert action == "calendar_get_today", f"'{phrase}' → {action}"

    def test_tomorrow_phrases_route_to_get_tomorrow(self):
        phrases = [
            "what do i have tomorrow",
            "what's on my calendar tomorrow",
            "tomorrow's schedule",
        ]
        for phrase in phrases:
            action = self._action(phrase)
            assert action == "calendar_get_tomorrow", f"'{phrase}' → {action}"

    def test_next_event_phrases_route_to_next_event(self):
        phrases = [
            "what's my next event",
            "next meeting",
        ]
        for phrase in phrases:
            action = self._action(phrase)
            assert action == "calendar_next_event", f"'{phrase}' → {action}"

    def test_summarize_phrases_route_to_summarize(self):
        phrases = [
            "summarize my day",
            "how does my day look",
        ]
        for phrase in phrases:
            action = self._action(phrase)
            assert action == "calendar_summarize_day", f"'{phrase}' → {action}"

    def test_free_block_phrases_route_to_find_free_blocks(self):
        phrases = [
            "do i have a free hour",
            "when am i free",
        ]
        for phrase in phrases:
            action = self._action(phrase)
            assert action == "calendar_find_free_blocks", f"'{phrase}' → {action}"

    def test_free_block_override_includes_date(self):
        result = self.resolve("do i have a free hour")
        assert result is not None
        payload = result.get("payload", {})
        assert "date" in payload
        assert payload["date"] == date.today().isoformat()


# ── ToolRegistry dispatch ─────────────────────────────────────────────────────

class TestToolRegistryDispatch:
    def _make_registry(self):
        sys.path.insert(0, str(ROOT))
        from prometheus.execution.tools import ToolRegistry
        return ToolRegistry()

    def test_calendar_get_today_dispatched(self):
        reg = self._make_registry()
        with patch("prometheus.agents.calendar_read_tools.build_google_calendar_service"), \
             patch("prometheus.agents.calendar_read_tools.list_calendar_events", return_value=[]), \
             patch.dict(os.environ, _ENV_ENABLED, clear=False):
            r = reg._execute_one_inner({"action": "calendar_get_today"})
            assert r.ok is True

    def test_calendar_get_tomorrow_dispatched(self):
        reg = self._make_registry()
        with patch("prometheus.agents.calendar_read_tools.build_google_calendar_service"), \
             patch("prometheus.agents.calendar_read_tools.list_calendar_events", return_value=[]), \
             patch.dict(os.environ, _ENV_ENABLED, clear=False):
            r = reg._execute_one_inner({"action": "calendar_get_tomorrow"})
            assert r.ok is True

    def test_calendar_next_event_dispatched(self):
        reg = self._make_registry()
        with patch("prometheus.agents.calendar_read_tools.build_google_calendar_service"), \
             patch("prometheus.agents.calendar_read_tools.list_calendar_events", return_value=[]), \
             patch.dict(os.environ, _ENV_ENABLED, clear=False):
            r = reg._execute_one_inner({"action": "calendar_next_event"})
            assert r.ok is True

    def test_calendar_summarize_day_dispatched(self):
        reg = self._make_registry()
        with patch("prometheus.agents.calendar_read_tools.build_google_calendar_service"), \
             patch("prometheus.agents.calendar_read_tools.list_calendar_events", return_value=[]), \
             patch.dict(os.environ, _ENV_ENABLED, clear=False):
            r = reg._execute_one_inner({"action": "calendar_summarize_day"})
            assert r.ok is True

    def test_calendar_find_free_blocks_dispatched(self):
        reg = self._make_registry()
        with patch("prometheus.agents.calendar_read_tools.build_google_calendar_service"), \
             patch("prometheus.agents.calendar_read_tools.list_calendar_events", return_value=[]), \
             patch.dict(os.environ, _ENV_ENABLED, clear=False):
            r = reg._execute_one_inner({
                "action": "calendar_find_free_blocks",
                "date": date.today().isoformat(),
                "minimum_minutes": 60,
            })
            assert r.ok is True

    def test_calendar_get_date_missing_date_param(self):
        reg = self._make_registry()
        with patch.dict(os.environ, _ENV_ENABLED, clear=False):
            r = reg._execute_one_inner({"action": "calendar_get_date"})
            assert r.ok is False
            assert "date" in r.message.lower()

    def test_calendar_find_free_blocks_missing_date_param(self):
        reg = self._make_registry()
        with patch.dict(os.environ, _ENV_ENABLED, clear=False):
            r = reg._execute_one_inner({"action": "calendar_find_free_blocks"})
            assert r.ok is False
            assert "date" in r.message.lower()

    def test_disabled_calendar_returns_clean_error(self):
        reg = self._make_registry()
        with patch.dict(os.environ, _ENV_DISABLED, clear=False):
            r = reg._execute_one_inner({"action": "calendar_get_today"})
            assert r.ok is False
            assert "disabled" in r.message.lower()

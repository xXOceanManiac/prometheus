"""
test_response_synthesizer.py — Unit tests for prometheus/execution/response_synthesizer.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from prometheus.execution.response_synthesizer import (
    synthesize_tool_response,
    is_calendar_action,
    _CALENDAR_ACTIONS,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _result(ok: bool = True, message: str = "ok", data: dict | None = None):
    return SimpleNamespace(ok=ok, message=message, data=data or {})


def _event(summary: str, start: str, all_day: bool = False) -> dict:
    return {"summary": summary, "start": start, "all_day": all_day}


# ── is_calendar_action ────────────────────────────────────────────────────────

class TestIsCalendarAction:
    def test_today_is_calendar(self):
        assert is_calendar_action("calendar_get_today") is True

    def test_tomorrow_is_calendar(self):
        assert is_calendar_action("calendar_get_tomorrow") is True

    def test_upcoming_is_calendar(self):
        assert is_calendar_action("calendar_list_upcoming") is True

    def test_next_event_is_calendar(self):
        assert is_calendar_action("calendar_next_event") is True

    def test_summarize_is_calendar(self):
        assert is_calendar_action("calendar_summarize_day") is True

    def test_free_blocks_is_calendar(self):
        assert is_calendar_action("calendar_find_free_blocks") is True

    def test_date_is_calendar(self):
        assert is_calendar_action("calendar_get_date") is True

    def test_web_search_is_not_calendar(self):
        assert is_calendar_action("web_search") is False

    def test_all_seven_covered(self):
        assert len(_CALENDAR_ACTIONS) == 7


# ── Failed results ────────────────────────────────────────────────────────────

class TestFailedResult:
    def test_failed_result_mentions_action(self):
        r = _result(ok=False, message="calendar disabled")
        out = synthesize_tool_response("calendar_get_today", r)
        assert "calendar_get_today" in out
        assert "disabled" in out

    def test_failed_result_is_string(self):
        r = _result(ok=False, message="auth error")
        assert isinstance(synthesize_tool_response("calendar_next_event", r), str)


# ── calendar_get_today / get_tomorrow / get_date ──────────────────────────────

class TestEventListActions:
    def test_today_no_events(self):
        r = _result(data={"events": []})
        out = synthesize_tool_response("calendar_get_today", r)
        assert "nothing" in out.lower()
        assert "today" in out.lower()

    def test_tomorrow_no_events(self):
        r = _result(data={"events": []})
        out = synthesize_tool_response("calendar_get_tomorrow", r)
        assert "nothing" in out.lower()
        assert "tomorrow" in out.lower()

    def test_today_with_timed_events(self):
        events = [
            _event("Morning standup", "2026-05-14T09:00:00"),
            _event("Lunch with team", "2026-05-14T12:00:00"),
        ]
        r = _result(data={"events": events})
        out = synthesize_tool_response("calendar_get_today", r)
        assert "2 event" in out
        assert "09:00" in out
        assert "Morning standup" in out

    def test_today_with_all_day_event(self):
        events = [_event("Review", "2026-05-14", all_day=True)]
        r = _result(data={"events": events})
        out = synthesize_tool_response("calendar_get_today", r)
        assert "all day" in out.lower()
        assert "Review" in out

    def test_date_label_in_output(self):
        r = _result(data={"date": "2026-05-20", "events": []})
        out = synthesize_tool_response("calendar_get_date", r)
        assert "2026-05-20" in out

    def test_caps_at_ten_events(self):
        events = [_event(f"Event {i}", f"2026-05-14T{9+i:02d}:00:00") for i in range(15)]
        r = _result(data={"events": events})
        out = synthesize_tool_response("calendar_get_today", r)
        # Should say "15 event(s)" but only list up to 10
        assert "15 event" in out
        assert out.count("- Event") == 10

    def test_returns_string(self):
        r = _result(data={"events": []})
        assert isinstance(synthesize_tool_response("calendar_get_today", r), str)


# ── calendar_list_upcoming ────────────────────────────────────────────────────

class TestUpcoming:
    def test_no_events(self):
        r = _result(data={"events": [], "days": 7})
        out = synthesize_tool_response("calendar_list_upcoming", r)
        assert "no upcoming" in out.lower()
        assert "7" in out

    def test_with_events(self):
        events = [
            _event("Dentist", "2026-05-15T10:00:00"),
            _event("Flight", "2026-05-18T14:30:00"),
        ]
        r = _result(data={"events": events, "days": 14})
        out = synthesize_tool_response("calendar_list_upcoming", r)
        assert "Dentist" in out
        assert "2026-05-15" in out
        assert "14" in out

    def test_all_day_label(self):
        events = [_event("Holiday", "2026-05-19")]
        r = _result(data={"events": events, "days": 14})
        out = synthesize_tool_response("calendar_list_upcoming", r)
        assert "all day" in out.lower()


# ── calendar_next_event ───────────────────────────────────────────────────────

class TestNextEvent:
    def test_no_events(self):
        r = _result(data={})
        out = synthesize_tool_response("calendar_next_event", r)
        assert "no upcoming" in out.lower()

    def test_timed_event(self):
        r = _result(data={
            "next_timed_event": {"summary": "Call", "start": "2026-05-14T15:00:00"},
        })
        out = synthesize_tool_response("calendar_next_event", r)
        assert "Call" in out
        assert "15:00" in out

    def test_all_day_event(self):
        r = _result(data={
            "next_all_day_event": {"summary": "Holiday", "start": "2026-05-15"},
        })
        out = synthesize_tool_response("calendar_next_event", r)
        assert "Holiday" in out
        assert "2026-05-15" in out

    def test_both_timed_and_all_day(self):
        r = _result(data={
            "next_timed_event": {"summary": "Meeting", "start": "2026-05-14T16:00:00"},
            "next_all_day_event": {"summary": "Review", "start": "2026-05-15"},
        })
        out = synthesize_tool_response("calendar_next_event", r)
        assert "Meeting" in out
        assert "Review" in out


# ── calendar_summarize_day ────────────────────────────────────────────────────

class TestSummarizeDay:
    def test_no_events(self):
        r = _result(data={"date": "2026-05-14", "event_count": 0})
        out = synthesize_tool_response("calendar_summarize_day", r)
        assert "nothing" in out.lower()
        assert "2026-05-14" in out

    def test_with_events(self):
        r = _result(data={
            "date": "2026-05-14",
            "event_count": 3,
            "first_timed_event": {"summary": "Standup", "start": "2026-05-14T09:00:00"},
            "last_timed_event": {"summary": "Run Club", "start": "2026-05-14T18:30:00"},
        })
        out = synthesize_tool_response("calendar_summarize_day", r)
        assert "3 event" in out
        assert "Standup" in out
        assert "09:00" in out
        assert "Run Club" in out

    def test_single_event_no_last(self):
        r = _result(data={
            "date": "2026-05-14",
            "event_count": 1,
            "first_timed_event": {"summary": "Call", "start": "2026-05-14T10:00:00"},
            "last_timed_event": {"summary": "Call", "start": "2026-05-14T10:00:00"},
        })
        out = synthesize_tool_response("calendar_summarize_day", r)
        assert "Call" in out
        # Should not repeat "Call" as last event when first == last
        assert out.count("Call") == 1


# ── calendar_find_free_blocks ─────────────────────────────────────────────────

class TestFreeBlocks:
    def test_no_blocks(self):
        r = _result(data={"free_blocks": [], "date": "2026-05-14", "minimum_minutes": 60})
        out = synthesize_tool_response("calendar_find_free_blocks", r)
        assert "no free" in out.lower()
        assert "60" in out
        assert "2026-05-14" in out

    def test_with_blocks(self):
        blocks = [
            {"start": "08:00", "end": "16:00", "duration_minutes": 480},
            {"start": "17:00", "end": "18:30", "duration_minutes": 90},
        ]
        r = _result(data={"free_blocks": blocks, "date": "2026-05-14", "minimum_minutes": 60})
        out = synthesize_tool_response("calendar_find_free_blocks", r)
        assert "08:00" in out
        assert "480" in out
        assert "90" in out

    def test_caps_at_five_blocks(self):
        blocks = [
            {"start": f"{8+i}:00", "end": f"{9+i}:00", "duration_minutes": 60}
            for i in range(8)
        ]
        r = _result(data={"free_blocks": blocks, "date": "2026-05-14", "minimum_minutes": 30})
        out = synthesize_tool_response("calendar_find_free_blocks", r)
        assert out.count("- ") == 5


# ── Unknown action fallback ───────────────────────────────────────────────────

class TestUnknownAction:
    def test_unknown_action_returns_string(self):
        r = _result()
        out = synthesize_tool_response("some_future_tool", r)
        assert isinstance(out, str)
        assert len(out) > 0

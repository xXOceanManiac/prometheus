"""
test_lumen_calendar_context.py — Tests for the Google Calendar → Lumen event conversion module.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from prometheus.integrations.google_calendar import GoogleCalendarEvent
from prometheus.calendar.lumen_context import (
    google_event_to_lumen_event_dict,
    google_events_to_lumen_event_dicts,
    build_calendar_context_summary,
)


def _make_event(
    event_id="evt-001",
    calendar_id="primary",
    title="Test Event",
    start_time="2026-05-15T10:00:00-04:00",
    end_time="2026-05-15T11:00:00-04:00",
    location=None,
    description=None,
    html_link=None,
    raw=None,
) -> GoogleCalendarEvent:
    return GoogleCalendarEvent(
        event_id=event_id,
        calendar_id=calendar_id,
        title=title,
        start_time=start_time,
        end_time=end_time,
        location=location,
        description=description,
        html_link=html_link,
        raw=raw,
    )


class TestGoogleEventToLumenEventDict:
    def test_basic_conversion(self):
        event = _make_event()
        result = google_event_to_lumen_event_dict(event)
        assert result["event_id"] == "evt-001"
        assert result["calendar_id"] == "primary"
        assert result["title"] == "Test Event"
        assert result["start_time"] == "2026-05-15T10:00:00-04:00"
        assert result["end_time"] == "2026-05-15T11:00:00-04:00"

    def test_all_fields_present(self):
        result = google_event_to_lumen_event_dict(_make_event())
        assert "event_id" in result
        assert "calendar_id" in result
        assert "title" in result
        assert "start_time" in result
        assert "end_time" in result
        assert "location" in result
        assert "description" in result
        assert "html_link" in result

    def test_none_fields_preserved(self):
        event = _make_event(location=None, description=None, html_link=None)
        result = google_event_to_lumen_event_dict(event)
        assert result["location"] is None
        assert result["description"] is None
        assert result["html_link"] is None

    def test_optional_fields_set(self):
        event = _make_event(
            location="Conference Room A",
            description="Sprint planning",
            html_link="https://calendar.google.com/event?id=evt-001",
        )
        result = google_event_to_lumen_event_dict(event)
        assert result["location"] == "Conference Room A"
        assert result["description"] == "Sprint planning"
        assert result["html_link"] == "https://calendar.google.com/event?id=evt-001"

    def test_no_raw_in_output(self):
        event = _make_event(raw={"summary": "Test Event"})
        result = google_event_to_lumen_event_dict(event)
        assert "raw" not in result

    def test_returns_dict(self):
        result = google_event_to_lumen_event_dict(_make_event())
        assert isinstance(result, dict)

    def test_no_event_id(self):
        event = _make_event(event_id=None)
        result = google_event_to_lumen_event_dict(event)
        assert result["event_id"] is None

    def test_no_end_time(self):
        event = _make_event(end_time=None)
        result = google_event_to_lumen_event_dict(event)
        assert result["end_time"] is None

    def test_custom_calendar_id(self):
        event = _make_event(calendar_id="work@company.com")
        result = google_event_to_lumen_event_dict(event)
        assert result["calendar_id"] == "work@company.com"


class TestGoogleEventsToLumenEventDicts:
    def test_empty_list(self):
        result = google_events_to_lumen_event_dicts([])
        assert result == []

    def test_single_event(self):
        events = [_make_event()]
        result = google_events_to_lumen_event_dicts(events)
        assert len(result) == 1
        assert isinstance(result[0], dict)

    def test_multiple_events(self):
        events = [
            _make_event(event_id="e1", title="Meeting A"),
            _make_event(event_id="e2", title="Meeting B"),
            _make_event(event_id="e3", title="Meeting C"),
        ]
        result = google_events_to_lumen_event_dicts(events)
        assert len(result) == 3
        assert result[0]["title"] == "Meeting A"
        assert result[1]["title"] == "Meeting B"
        assert result[2]["title"] == "Meeting C"

    def test_preserves_order(self):
        events = [_make_event(event_id=f"e{i}") for i in range(5)]
        result = google_events_to_lumen_event_dicts(events)
        for i, item in enumerate(result):
            assert item["event_id"] == f"e{i}"

    def test_returns_list_of_dicts(self):
        events = [_make_event(), _make_event(event_id="e2")]
        result = google_events_to_lumen_event_dicts(events)
        assert isinstance(result, list)
        assert all(isinstance(r, dict) for r in result)


class TestBuildCalendarContextSummary:
    def test_empty_events(self):
        result = build_calendar_context_summary([])
        assert result["event_count"] == 0
        assert result["events"] == []
        assert result["earliest_start"] is None
        assert result["latest_end"] is None

    def test_single_event(self):
        events = [_make_event(start_time="2026-05-15T10:00:00", end_time="2026-05-15T11:00:00")]
        result = build_calendar_context_summary(events)
        assert result["event_count"] == 1
        assert len(result["events"]) == 1
        assert result["earliest_start"] == "2026-05-15T10:00:00"
        assert result["latest_end"] == "2026-05-15T11:00:00"

    def test_multiple_events_finds_bounds(self):
        events = [
            _make_event(event_id="e1", start_time="2026-05-15T10:00:00", end_time="2026-05-15T11:00:00"),
            _make_event(event_id="e2", start_time="2026-05-15T08:00:00", end_time="2026-05-15T09:00:00"),
            _make_event(event_id="e3", start_time="2026-05-15T14:00:00", end_time="2026-05-15T15:30:00"),
        ]
        result = build_calendar_context_summary(events)
        assert result["event_count"] == 3
        assert result["earliest_start"] == "2026-05-15T08:00:00"
        assert result["latest_end"] == "2026-05-15T15:30:00"

    def test_events_embedded_in_result(self):
        events = [_make_event(title="Morning Standup")]
        result = build_calendar_context_summary(events)
        assert len(result["events"]) == 1
        assert result["events"][0]["title"] == "Morning Standup"

    def test_events_without_end_time(self):
        events = [_make_event(start_time="2026-05-15T10:00:00", end_time=None)]
        result = build_calendar_context_summary(events)
        assert result["earliest_start"] == "2026-05-15T10:00:00"
        assert result["latest_end"] is None

    def test_required_keys_present(self):
        result = build_calendar_context_summary([])
        assert "event_count" in result
        assert "events" in result
        assert "earliest_start" in result
        assert "latest_end" in result

    def test_returns_dict(self):
        result = build_calendar_context_summary([_make_event()])
        assert isinstance(result, dict)

    def test_no_network_calls(self):
        # Pure conversion — should always succeed regardless of env
        events = [_make_event() for _ in range(10)]
        result = build_calendar_context_summary(events)
        assert result["event_count"] == 10

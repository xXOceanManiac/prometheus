"""
prometheus/agents/lumen_calendar_context.py — Google Calendar → Lumen event conversion.

Converts GoogleCalendarEvent objects into Lumen-compatible event dicts and
builds calendar context summaries for feeding into Lumen's scheduler.

No network calls, no Google API calls, no filesystem writes.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from prometheus.integrations.google_calendar import GoogleCalendarEvent


def google_event_to_lumen_event_dict(event: "GoogleCalendarEvent") -> dict:
    """Convert a GoogleCalendarEvent to a Lumen-compatible event dict."""
    return {
        "event_id": event.event_id,
        "calendar_id": event.calendar_id,
        "title": event.title,
        "start_time": event.start_time,
        "end_time": event.end_time,
        "location": event.location,
        "description": event.description,
        "html_link": event.html_link,
    }


def google_events_to_lumen_event_dicts(events: list) -> list[dict]:
    """Convert a list of GoogleCalendarEvents to Lumen-compatible event dicts."""
    return [google_event_to_lumen_event_dict(e) for e in events]


def build_calendar_context_summary(events: list) -> dict:
    """
    Build a calendar context summary dict from a list of GoogleCalendarEvents.

    Produces a structure Lumen's scheduler can consume to reason about
    availability and schedule conflicts.
    """
    if not events:
        return {
            "event_count": 0,
            "events": [],
            "earliest_start": None,
            "latest_end": None,
        }

    dicts = google_events_to_lumen_event_dicts(events)
    start_times = [d["start_time"] for d in dicts if d["start_time"]]
    end_times = [d["end_time"] for d in dicts if d["end_time"]]

    return {
        "event_count": len(events),
        "events": dicts,
        "earliest_start": min(start_times) if start_times else None,
        "latest_end": max(end_times) if end_times else None,
    }

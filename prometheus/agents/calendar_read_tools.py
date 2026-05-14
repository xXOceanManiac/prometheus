"""
prometheus/agents/calendar_read_tools.py — Read-only Google Calendar tool functions.

Exposes deterministic calendar reads to Prometheus's tool/action layer.
No event mutation. No insert/update/delete. No Home Assistant calls.
No subprocess/shell execution. No passive scheduler.

All outputs are JSON-serializable dicts.
Errors are surfaced as {"ok": False, "error": "..."} — never raise to callers.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from prometheus.integrations.google_calendar import (
    GoogleCalendarConfig,
    GoogleCalendarEvent,
    build_google_calendar_service,
    list_calendar_events,
    load_google_calendar_config,
)


# ── Timezone helpers ──────────────────────────────────────────────────────────

def _get_local_tz(config: GoogleCalendarConfig) -> ZoneInfo:
    tz_name = (config.timezone or "America/New_York").strip()
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        return ZoneInfo("America/New_York")


def _local_midnight(d: date, tz: ZoneInfo) -> datetime:
    return datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=tz)


def _to_rfc3339(dt: datetime) -> str:
    return dt.isoformat()


# ── Event serialization ───────────────────────────────────────────────────────

def _event_to_dict(event: GoogleCalendarEvent) -> dict:
    return {
        "event_id": event.event_id,
        "title": event.title,
        "start_time": event.start_time,
        "end_time": event.end_time,
        "location": event.location,
        "description": event.description,
        "calendar_id": event.calendar_id,
        "is_all_day": _is_all_day(event),
    }


def _is_all_day(event: GoogleCalendarEvent) -> bool:
    start = event.start_time or ""
    return len(start) == 10 and "T" not in start


def _events_to_list(events: list[GoogleCalendarEvent]) -> list[dict]:
    return [_event_to_dict(e) for e in events]


# ── Service builder (cached per-call; no module-level state) ──────────────────

def _build_service(config: GoogleCalendarConfig):
    return build_google_calendar_service(config, allow_interactive_auth=False)


def _disabled_error(config: GoogleCalendarConfig) -> dict:
    return {
        "ok": False,
        "error": "Google Calendar is disabled. Set GOOGLE_CALENDAR_ENABLED=true to enable.",
        "calendar_enabled": False,
    }


def _auth_error(exc: Exception) -> dict:
    return {
        "ok": False,
        "error": f"Google Calendar authentication failed: {str(exc)[:200]}",
        "calendar_enabled": True,
    }


# ── Read-only tool functions ──────────────────────────────────────────────────

def calendar_list_upcoming(max_results: int = 10, days: int = 14) -> dict:
    """List upcoming events from now through the next `days` days."""
    config = load_google_calendar_config()
    if not config.enabled:
        return _disabled_error(config)
    try:
        service = _build_service(config)
    except Exception as exc:
        return _auth_error(exc)

    tz = _get_local_tz(config)
    now = datetime.now(tz)
    time_max = now + timedelta(days=max(1, days))

    try:
        events = list_calendar_events(
            service=service,
            config=config,
            time_min=_to_rfc3339(now),
            time_max=_to_rfc3339(time_max),
            max_results=max(1, min(max_results, 50)),
        )
    except Exception as exc:
        return {"ok": False, "error": f"Failed to list events: {str(exc)[:200]}"}

    return {
        "ok": True,
        "count": len(events),
        "days": days,
        "events": _events_to_list(events),
    }


def calendar_get_today() -> dict:
    """List all events for local today."""
    config = load_google_calendar_config()
    if not config.enabled:
        return _disabled_error(config)
    try:
        service = _build_service(config)
    except Exception as exc:
        return _auth_error(exc)

    tz = _get_local_tz(config)
    today = datetime.now(tz).date()
    day_start = _local_midnight(today, tz)
    day_end = _local_midnight(today + timedelta(days=1), tz)

    try:
        events = list_calendar_events(
            service=service,
            config=config,
            time_min=_to_rfc3339(day_start),
            time_max=_to_rfc3339(day_end),
            max_results=50,
        )
    except Exception as exc:
        return {"ok": False, "error": f"Failed to list today's events: {str(exc)[:200]}"}

    return {
        "ok": True,
        "date": today.isoformat(),
        "count": len(events),
        "events": _events_to_list(events),
    }


def calendar_get_tomorrow() -> dict:
    """List all events for local tomorrow."""
    config = load_google_calendar_config()
    if not config.enabled:
        return _disabled_error(config)
    try:
        service = _build_service(config)
    except Exception as exc:
        return _auth_error(exc)

    tz = _get_local_tz(config)
    tomorrow = datetime.now(tz).date() + timedelta(days=1)
    day_start = _local_midnight(tomorrow, tz)
    day_end = _local_midnight(tomorrow + timedelta(days=1), tz)

    try:
        events = list_calendar_events(
            service=service,
            config=config,
            time_min=_to_rfc3339(day_start),
            time_max=_to_rfc3339(day_end),
            max_results=50,
        )
    except Exception as exc:
        return {"ok": False, "error": f"Failed to list tomorrow's events: {str(exc)[:200]}"}

    return {
        "ok": True,
        "date": tomorrow.isoformat(),
        "count": len(events),
        "events": _events_to_list(events),
    }


def calendar_get_date(date_str: str) -> dict:
    """List all events for a specific YYYY-MM-DD date."""
    if not date_str or not isinstance(date_str, str):
        return {"ok": False, "error": "date must be a non-empty string in YYYY-MM-DD format"}

    date_str = date_str.strip()
    try:
        parsed_date = date.fromisoformat(date_str)
    except ValueError:
        return {"ok": False, "error": f"Invalid date format: {date_str!r}. Expected YYYY-MM-DD."}

    config = load_google_calendar_config()
    if not config.enabled:
        return _disabled_error(config)
    try:
        service = _build_service(config)
    except Exception as exc:
        return _auth_error(exc)

    tz = _get_local_tz(config)
    day_start = _local_midnight(parsed_date, tz)
    day_end = _local_midnight(parsed_date + timedelta(days=1), tz)

    try:
        events = list_calendar_events(
            service=service,
            config=config,
            time_min=_to_rfc3339(day_start),
            time_max=_to_rfc3339(day_end),
            max_results=50,
        )
    except Exception as exc:
        return {"ok": False, "error": f"Failed to list events for {date_str}: {str(exc)[:200]}"}

    return {
        "ok": True,
        "date": date_str,
        "count": len(events),
        "events": _events_to_list(events),
    }


def calendar_next_event() -> dict:
    """
    Return the next upcoming timed event, plus any all-day events for today.
    All-day events are surfaced separately — they are not considered the next timed commitment.
    """
    config = load_google_calendar_config()
    if not config.enabled:
        return _disabled_error(config)
    try:
        service = _build_service(config)
    except Exception as exc:
        return _auth_error(exc)

    tz = _get_local_tz(config)
    now = datetime.now(tz)
    time_max = now + timedelta(days=14)

    try:
        events = list_calendar_events(
            service=service,
            config=config,
            time_min=_to_rfc3339(now),
            time_max=_to_rfc3339(time_max),
            max_results=20,
        )
    except Exception as exc:
        return {"ok": False, "error": f"Failed to look up next event: {str(exc)[:200]}"}

    timed = [e for e in events if not _is_all_day(e)]
    all_day = [e for e in events if _is_all_day(e)]
    today_str = now.date().isoformat()
    todays_all_day = [e for e in all_day if (e.start_time or "").startswith(today_str)]

    next_timed = _event_to_dict(timed[0]) if timed else None

    return {
        "ok": True,
        "next_timed_event": next_timed,
        "todays_all_day_events": _events_to_list(todays_all_day),
        "has_next_timed": next_timed is not None,
    }


def calendar_summarize_day(date_str: Optional[str] = None) -> dict:
    """
    Return a deterministic day summary: event count, all-day events, timed events,
    first/last timed event, and a plain-text summary.

    date_str: YYYY-MM-DD, or None for today.
    """
    config = load_google_calendar_config()
    if not config.enabled:
        return _disabled_error(config)
    try:
        service = _build_service(config)
    except Exception as exc:
        return _auth_error(exc)

    tz = _get_local_tz(config)

    if date_str is None:
        target_date = datetime.now(tz).date()
    else:
        date_str = date_str.strip()
        try:
            target_date = date.fromisoformat(date_str)
        except ValueError:
            return {"ok": False, "error": f"Invalid date: {date_str!r}. Expected YYYY-MM-DD."}

    day_start = _local_midnight(target_date, tz)
    day_end = _local_midnight(target_date + timedelta(days=1), tz)

    try:
        events = list_calendar_events(
            service=service,
            config=config,
            time_min=_to_rfc3339(day_start),
            time_max=_to_rfc3339(day_end),
            max_results=50,
        )
    except Exception as exc:
        return {"ok": False, "error": f"Failed to summarize day: {str(exc)[:200]}"}

    all_day_events = [_event_to_dict(e) for e in events if _is_all_day(e)]
    timed_events = [_event_to_dict(e) for e in events if not _is_all_day(e)]

    first_timed = timed_events[0] if timed_events else None
    last_timed = timed_events[-1] if timed_events else None

    day_label = target_date.isoformat()
    if timed_events:
        if len(timed_events) == 1:
            summary = f"1 event on {day_label}: {timed_events[0]['title']} at {timed_events[0]['start_time'][:16]}."
        else:
            first_title = timed_events[0]["title"]
            first_time = timed_events[0]["start_time"][:16]
            summary = (
                f"{len(timed_events)} events on {day_label}. "
                f"First: {first_title} at {first_time}."
            )
        if all_day_events:
            all_day_titles = ", ".join(e["title"] for e in all_day_events[:3])
            summary += f" All-day: {all_day_titles}."
    elif all_day_events:
        titles = ", ".join(e["title"] for e in all_day_events[:3])
        summary = f"No timed events on {day_label}. All-day: {titles}."
    else:
        summary = f"No events on {day_label}."

    return {
        "ok": True,
        "date": day_label,
        "event_count": len(events),
        "all_day_events": all_day_events,
        "timed_events": timed_events,
        "first_timed_event": first_timed,
        "last_timed_event": last_timed,
        "summary": summary,
    }


def calendar_find_free_blocks(
    date_str: str,
    minimum_minutes: int = 60,
    day_start_hour: int = 8,
    day_end_hour: int = 22,
) -> dict:
    """
    Find free time blocks on a given date between day_start_hour and day_end_hour (local).
    Ignores all-day events (they don't block calendar time).
    Returns blocks >= minimum_minutes long.
    """
    if not date_str or not isinstance(date_str, str):
        return {"ok": False, "error": "date_str must be a non-empty YYYY-MM-DD string"}

    date_str = date_str.strip()
    try:
        target_date = date.fromisoformat(date_str)
    except ValueError:
        return {"ok": False, "error": f"Invalid date: {date_str!r}. Expected YYYY-MM-DD."}

    if not isinstance(minimum_minutes, int) or minimum_minutes < 1:
        minimum_minutes = 60

    config = load_google_calendar_config()
    if not config.enabled:
        return _disabled_error(config)
    try:
        service = _build_service(config)
    except Exception as exc:
        return _auth_error(exc)

    tz = _get_local_tz(config)
    day_start = _local_midnight(target_date, tz).replace(hour=day_start_hour)
    day_end = _local_midnight(target_date, tz).replace(hour=day_end_hour)

    try:
        events = list_calendar_events(
            service=service,
            config=config,
            time_min=_to_rfc3339(day_start),
            time_max=_to_rfc3339(day_end),
            max_results=50,
        )
    except Exception as exc:
        return {"ok": False, "error": f"Failed to load events for free-block calc: {str(exc)[:200]}"}

    # Only consider timed events for blocking
    timed = [e for e in events if not _is_all_day(e)]

    # Build sorted list of (start, end) datetimes
    busy: list[tuple[datetime, datetime]] = []
    for e in timed:
        try:
            start_dt = datetime.fromisoformat(e.start_time).astimezone(tz)
            end_str = e.end_time or e.start_time
            end_dt = datetime.fromisoformat(end_str).astimezone(tz)
            if start_dt < end_dt:
                busy.append((start_dt, end_dt))
        except Exception:
            continue

    busy.sort(key=lambda x: x[0])

    # Merge overlapping busy blocks
    merged: list[tuple[datetime, datetime]] = []
    for start, end in busy:
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    # Find gaps >= minimum_minutes
    free_blocks: list[dict] = []
    cursor = day_start
    for busy_start, busy_end in merged:
        if cursor < busy_start:
            gap_minutes = int((busy_start - cursor).total_seconds() / 60)
            if gap_minutes >= minimum_minutes:
                free_blocks.append({
                    "start": cursor.isoformat(),
                    "end": busy_start.isoformat(),
                    "duration_minutes": gap_minutes,
                })
        cursor = max(cursor, busy_end)
    if cursor < day_end:
        gap_minutes = int((day_end - cursor).total_seconds() / 60)
        if gap_minutes >= minimum_minutes:
            free_blocks.append({
                "start": cursor.isoformat(),
                "end": day_end.isoformat(),
                "duration_minutes": gap_minutes,
            })

    return {
        "ok": True,
        "date": date_str,
        "minimum_minutes": minimum_minutes,
        "day_window": f"{day_start_hour:02d}:00–{day_end_hour:02d}:00",
        "free_block_count": len(free_blocks),
        "free_blocks": free_blocks,
        "busy_event_count": len(timed),
    }


# ── CLI entry point ───────────────────────────────────────────────────────────

def _load_dotenv() -> None:
    from pathlib import Path as _Path
    try:
        from prometheus.infra.paths import PROJECT_ROOT
        env_path = PROJECT_ROOT / ".env"
    except Exception:
        env_path = _Path(__file__).resolve().parent.parent.parent / ".env"
    if not env_path.is_file():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path, override=False)
        return
    except ImportError:
        pass
    try:
        with env_path.open(encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, raw_val = line.partition("=")
                key = key.strip()
                if not key or key in os.environ:
                    continue
                val = raw_val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                    val = val[1:-1]
                os.environ[key] = val
    except OSError:
        pass


def _main(argv: list[str] | None = None) -> None:
    _load_dotenv()
    args = argv if argv is not None else sys.argv[1:]

    if not args:
        print(
            "Usage: python -m prometheus.agents.calendar_read_tools "
            "--today | --tomorrow | --next | --free-blocks YYYY-MM-DD "
            "| --date YYYY-MM-DD | --upcoming [DAYS]"
        )
        sys.exit(1)

    cmd = args[0]

    if cmd == "--today":
        result = calendar_get_today()
    elif cmd == "--tomorrow":
        result = calendar_get_tomorrow()
    elif cmd == "--next":
        result = calendar_next_event()
    elif cmd == "--summarize":
        date_arg = args[1] if len(args) > 1 else None
        result = calendar_summarize_day(date_arg)
    elif cmd == "--date":
        if len(args) < 2:
            print("--date requires a YYYY-MM-DD argument", file=sys.stderr)
            sys.exit(1)
        result = calendar_get_date(args[1])
    elif cmd == "--free-blocks":
        if len(args) < 2:
            print("--free-blocks requires a YYYY-MM-DD argument", file=sys.stderr)
            sys.exit(1)
        min_min = int(args[2]) if len(args) > 2 else 60
        result = calendar_find_free_blocks(args[1], minimum_minutes=min_min)
    elif cmd == "--upcoming":
        days = int(args[1]) if len(args) > 1 else 14
        result = calendar_list_upcoming(days=days)
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    _main()

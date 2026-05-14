"""
response_synthesizer.py — Converts ToolResult data into natural-language
response_instructions strings for _guarded_response_create.

Keeps calendar-specific formatting out of realtime_client.py.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tools import ToolResult

_CALENDAR_ACTIONS: frozenset[str] = frozenset({
    "calendar_get_today",
    "calendar_get_tomorrow",
    "calendar_get_date",
    "calendar_list_upcoming",
    "calendar_next_event",
    "calendar_summarize_day",
    "calendar_find_free_blocks",
})


def synthesize_tool_response(
    action: str,
    result: "ToolResult",
    original_user_message: str | None = None,
) -> str:
    """Return LLM response_instructions for a completed tool action.

    Returns a plain string (no f-string outer wrapper) that is passed
    directly to response.create as `instructions`.
    """
    if not result.ok:
        return (
            f"The action '{action}' failed: {result.message}. "
            "Tell the user what went wrong in one sentence."
        )

    data = result.data or {}

    if action == "calendar_get_today":
        return _event_list(data, "today")

    if action == "calendar_get_tomorrow":
        return _event_list(data, "tomorrow")

    if action == "calendar_get_date":
        date = str(data.get("date", "")).strip()
        label = f"on {date}" if date else "that day"
        return _event_list(data, label)

    if action == "calendar_list_upcoming":
        return _upcoming(data)

    if action == "calendar_next_event":
        return _next_event(data)

    if action == "calendar_summarize_day":
        return _day_summary(data)

    if action == "calendar_find_free_blocks":
        return _free_blocks(data)

    return (
        "Briefly report the result in one or two sentences. Do not add preamble."
    )


def is_calendar_action(action: str) -> bool:
    return action in _CALENDAR_ACTIONS


# ── Private formatters ────────────────────────────────────────────────────────

def _event_list(data: dict, label: str) -> str:
    events = data.get("events", [])
    if not events:
        return f"Tell the user they have nothing scheduled {label}."
    lines: list[str] = []
    for ev in events[:10]:
        name = ev.get("summary", "Untitled")
        start = str(ev.get("start", ""))
        if ev.get("all_day"):
            lines.append(f"- {name} (all day)")
        elif "T" in start:
            lines.append(f"- {name} at {start[11:16]}")
        else:
            lines.append(f"- {name}")
    return (
        f"The user has {len(events)} event(s) {label}:\n" +
        "\n".join(lines) +
        "\nRead them naturally. Say times clearly. No filler."
    )


def _upcoming(data: dict) -> str:
    events = data.get("events", [])
    days = int(data.get("days", 14))
    if not events:
        return f"Tell the user they have no upcoming events in the next {days} days."
    lines: list[str] = []
    for ev in events[:10]:
        name = ev.get("summary", "Untitled")
        start = str(ev.get("start", ""))
        date_part = start[:10] if len(start) >= 10 else start
        time_part = start[11:16] if "T" in start else "all day"
        lines.append(f"- {name} on {date_part} at {time_part}")
    return (
        f"Upcoming events over the next {days} days:\n" +
        "\n".join(lines) +
        "\nRead them naturally. No filler."
    )


def _next_event(data: dict) -> str:
    timed = data.get("next_timed_event")
    all_day = data.get("next_all_day_event")
    if not timed and not all_day:
        return "Tell the user they have no upcoming events on their calendar."
    parts: list[str] = []
    if timed:
        name = timed.get("summary", "Untitled")
        start = str(timed.get("start", ""))
        time_str = start[11:16] if "T" in start else start[:10]
        parts.append(f"Next event: {name} at {time_str}.")
    if all_day:
        name = all_day.get("summary", "Untitled")
        date_str = str(all_day.get("start", ""))[:10]
        parts.append(f"Also: {name} all day on {date_str}.")
    return " ".join(parts) + " Speak naturally. No filler."


def _day_summary(data: dict) -> str:
    date = str(data.get("date", "")).strip()
    count = int(data.get("event_count", 0))
    if count == 0:
        day_label = f"on {date}" if date else "today"
        return f"Tell the user they have nothing scheduled {day_label}."
    first = data.get("first_timed_event") or {}
    last = data.get("last_timed_event") or {}
    f_name = first.get("summary", "")
    f_start = str(first.get("start", ""))
    f_time = f_start[11:16] if "T" in f_start else ""
    l_name = last.get("summary", "")
    l_start = str(last.get("start", ""))
    l_time = l_start[11:16] if "T" in l_start else ""
    parts = [f"{count} event(s) on {date}."]
    if f_name and f_time:
        parts.append(f"Day starts with {f_name} at {f_time}.")
    if l_name and l_time and l_name != f_name:
        parts.append(f"Last event: {l_name} at {l_time}.")
    return " ".join(parts) + " Speak naturally. No filler."


def _free_blocks(data: dict) -> str:
    blocks = data.get("free_blocks", [])
    date = str(data.get("date", "")).strip()
    minimum = int(data.get("minimum_minutes", 60))
    day_label = f"on {date}" if date else "today"
    if not blocks:
        return (
            f"Tell the user they have no free blocks of at least "
            f"{minimum} minutes {day_label}."
        )
    lines: list[str] = []
    for b in blocks[:5]:
        start = b.get("start", "")
        end = b.get("end", "")
        dur = b.get("duration_minutes", 0)
        lines.append(f"- {start}–{end} ({dur} min)")
    return (
        f"Free blocks of at least {minimum} min {day_label}:\n" +
        "\n".join(lines) +
        "\nRead them naturally. No filler."
    )

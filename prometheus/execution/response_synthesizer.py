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

_EXECUTOR_ACTIONS: frozenset[str] = frozenset({
    "calendar_list_reviewed_requests",
    "calendar_approve_request",
    "calendar_execute_approved_request",
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

    if action == "show_logs":
        return _show_logs(data)

    if action in _EXECUTOR_ACTIONS:
        return _calendar_executor(action, data)

    return (
        "Briefly report the result in one or two sentences. Do not add preamble."
    )


def is_calendar_action(action: str) -> bool:
    return action in _CALENDAR_ACTIONS


def is_synthesized_action(action: str) -> bool:
    """Return True for any action handled by this synthesizer."""
    return action in _CALENDAR_ACTIONS or action in _EXECUTOR_ACTIONS or action == "show_logs"


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


def _show_logs(data: dict) -> str:
    entries = data.get("entries", [])
    latest_file = str(data.get("latest_file") or data.get("source") or "")
    logs_dir = str(data.get("logs_dir", ""))
    count = int(data.get("lines_returned") or data.get("count") or len(entries))

    if not entries or count == 0:
        if logs_dir:
            return f"Tell the user: 'No logs found in {logs_dir}.'"
        return "Tell the user there are no logs available right now."

    recent = entries[-15:] if len(entries) > 15 else entries
    entry_text = "\n".join(str(e) for e in recent)[:800]
    return (
        f"Here are the latest {len(recent)} Prometheus log entries from {latest_file}:\n"
        f"{entry_text}\n"
        "Read the most recent entries naturally. Highlight any errors or warnings. "
        "Be concise — summarize patterns rather than reading every line verbatim. No filler."
    )


def _calendar_executor(action: str, data: dict) -> str:
    if action == "calendar_list_reviewed_requests":
        requests = data.get("requests", [])
        if not requests:
            return "Tell the user there are no reviewed calendar requests waiting for approval."
        count = len(requests)
        ids = [str(r.get("request_id", "?"))[-12:] for r in requests[:5]]
        return (
            f"There are {count} reviewed calendar request(s) waiting for approval. "
            f"Request IDs: {', '.join(ids)}. "
            "Tell the user they can approve one with 'approve calendar request' followed by the ID. "
            "Do not approve automatically."
        )

    if action == "calendar_approve_request":
        request_id = str(data.get("request_id", ""))
        op_count = int(data.get("operation_count", 0))
        approved_by = str(data.get("approved_by", "user"))
        if data.get("approved"):
            return (
                f"Calendar request {request_id} has been approved by {approved_by}. "
                f"It contains {op_count} operation(s). "
                "To execute the write, say 'execute approved calendar request' followed by the ID. "
                "No calendar changes have been made yet."
            )
        return (
            f"Approval for calendar request {request_id} failed: "
            f"{str(data.get('reason', 'unknown reason'))}. "
            "Tell the user what went wrong."
        )

    if action == "calendar_execute_approved_request":
        request_id = str(data.get("request_id", ""))
        success = bool(data.get("success"))
        op_count = int(data.get("operation_count", 0))
        if success:
            return (
                f"Calendar request {request_id} executed successfully. "
                f"{op_count} operation(s) completed on Google Calendar. "
                "Speak this concisely. No filler."
            )
        reason = str(data.get("reason") or data.get("message", "unknown error"))
        return (
            f"Calendar execution for request {request_id} failed: {reason}. "
            "Tell the user what went wrong clearly."
        )

    return "Briefly report the calendar operation result. No filler."

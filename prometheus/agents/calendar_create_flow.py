"""
calendar_create_flow.py — Natural-language calendar creation flow.

Lifecycle (low-friction path — fully-specified, low-risk requests):
  parse_and_propose(user_request)
      → auto-executes via _direct_create_calendar_event()
      → returns status "executed" | "blocked" | "failed" | "conflict"

Lifecycle (confirmation path — ambiguous / high-risk requests):
  parse_and_propose(user_request)
      → pending confirmation file written
      → returns status "pending" with human_summary for user confirmation

  confirm_pending_calendar_confirmation()
      → writes reviewed + approval records
      → calls execute_approved_calendar_request() (existing pipeline)

  cancel_pending_calendar_confirmation()
      → marks pending file as canceled

Hard constraints enforced here:
- No passive scheduling — only writes on explicit user command.
- Window-based requests (morning/afternoon) always ask confirmation after availability search.
- No recurring events.
- No calendar update/delete via NL in this module.
- No Home Assistant calls.
- No direct Google Calendar API calls — routes through approved executor pipeline.
- GOOGLE_CALENDAR_ENABLED and GOOGLE_CALENDAR_DRY_RUN=false enforced by executor.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from prometheus.infra.paths import (
    PENDING_CALENDAR_CONFIRMATIONS_DIR,
    REVIEWED_LUMEN_DIR,
    APPROVED_LUMEN_DIR,
    ensure_lumen_executor_dirs,
    ensure_calendar_confirmation_dir,
)

# Module-level reference so tests can patch it without requiring the lazy import path.
try:
    from prometheus.agents.lumen_calendar_executor import execute_approved_calendar_request
except Exception:
    execute_approved_calendar_request = None  # type: ignore[assignment]

# Module-level reference for conflict checking; tests can patch it directly.
try:
    from prometheus.agents.calendar_read_tools import calendar_get_date as _calendar_get_date_fn
except Exception:
    _calendar_get_date_fn = None  # type: ignore[assignment]

# ── Constants ─────────────────────────────────────────────────────────────────

_WEEKDAY_NAMES = [
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
]

_WINDOW_MAP: dict[str, tuple[int, int]] = {
    "morning": (8, 12),
    "afternoon": (12, 17),
    "evening": (17, 21),
    "tonight": (18, 22),
}

_DURATION_MAP: list[tuple[frozenset[str], int]] = [
    (frozenset({"focus block", "focus session", "deep work", "heads down", "deep focus"}), 90),
    (frozenset({"focus"}), 90),
    (frozenset({"workout", "exercise", "gym", "yoga", "pilates"}), 60),
    (frozenset({"run", "jog", "hike", "walk", "swim"}), 60),
    (frozenset({"standup", "stand-up", "stand up", "check-in", "checkin", "daily scrum"}), 30),
    (frozenset({"quick sync", "quick call", "quick check", "quick meeting"}), 30),
    (frozenset({"1:1", "one on one", "one-on-one"}), 60),
    (frozenset({"lunch", "dinner", "breakfast"}), 60),
    (frozenset({"coffee", "coffee chat", "coffee call"}), 30),
    (frozenset({"interview", "sync", "meeting", "call", "session"}), 60),
]

_DATE_STOP_WORDS = frozenset({
    "today", "tomorrow", "morning", "afternoon", "evening", "tonight",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "next", "this", "at", "for", "in", "on", "from", "until",
})

_CREATE_VERBS = [
    "block off",  # multi-word first
    "set up",
    "schedule",
    "book",
    "create",
    "make",
    "add",
    "put",
    "plan",
    "log",
]


# ── Parsing helpers ───────────────────────────────────────────────────────────

def _default_duration(title: str) -> int:
    """Return default duration in minutes based on keywords in title."""
    lower = title.lower()
    for keywords, minutes in _DURATION_MAP:
        if any(kw in lower for kw in keywords):
            return minutes
    return 60


def _extract_explicit_duration(text: str) -> Optional[int]:
    """Extract an explicit duration stated in text. Returns minutes, or None."""
    if re.search(r'\bhalf\s+(?:an\s+)?hour\b', text):
        return 30
    m = re.search(r'\b(\d+)\s+and\s+a\s+half\s+hours?\b', text)
    if m:
        return int(m.group(1)) * 60 + 30
    m = re.search(r'\b(\d+)\s*hours?\b', text)
    if m:
        return int(m.group(1)) * 60
    m = re.search(r'\b(\d+)\s*min(?:utes?)?\b', text)
    if m:
        return int(m.group(1))
    if re.search(r'\ban\s+hour\b', text):
        return 60
    return None


def _extract_date_hint(text: str) -> str:
    """Extract date hint string from normalized text."""
    # Look for "next <weekday>"
    m = re.search(r'\bnext\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b', text)
    if m:
        return f"next {m.group(1)}"

    # Look for "this <weekday>"
    m = re.search(r'\bthis\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b', text)
    if m:
        return m.group(1)

    # Look for plain weekday names
    for wd in _WEEKDAY_NAMES:
        if f" {wd}" in text or text.startswith(wd):
            return wd

    if "tomorrow" in text:
        return "tomorrow"

    # "this morning/afternoon/evening" → today
    for w in ("this morning", "this afternoon", "this evening"):
        if w in text:
            return "today"

    if "tonight" in text:
        return "today"

    if "today" in text:
        return "today"

    return ""


def _extract_time_hint(text: str) -> tuple[str, str]:
    """Extract (time_hint, window_hint) from normalized text.

    time_hint: e.g. "at 2", "at 4pm", "4pm", "2"  (empty if window-based)
    window_hint: e.g. "afternoon" (empty if explicit time)
    """
    # Explicit time with "at": "at HH:MM am/pm", "at H am/pm", "at H"
    m = re.search(r'\bat\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b', text)
    if m:
        return (m.group(0).strip(), "")

    # Bare time with am/pm but no "at": "4pm", "10am", "2:30pm"
    m = re.search(r'\b(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b', text)
    if m:
        return (m.group(0).strip(), "")

    # Bare hour number immediately after a date word: "tomorrow 2", "friday 3"
    m = re.search(
        r'\b(?:tomorrow|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+(\d{1,2})\b',
        text,
    )
    if m:
        return (m.group(1), "")

    # Window keywords
    for window in ("morning", "afternoon", "evening", "tonight"):
        if window in text:
            return ("", window)

    return ("", "")


def _resolve_date(date_hint: str, now: date) -> Optional[date]:
    """Convert a date hint string to a concrete date."""
    hint = date_hint.strip().lower()
    if not hint:
        return None
    if hint == "today":
        return now
    if hint == "tomorrow":
        return now + timedelta(days=1)

    # "next <weekday>"
    m = re.match(r'^next\s+(\w+)$', hint)
    if m:
        wd_name = m.group(1)
        if wd_name in _WEEKDAY_NAMES:
            target_wd = _WEEKDAY_NAMES.index(wd_name)
            current_wd = now.weekday()
            days_ahead = (target_wd - current_wd) % 7
            if days_ahead == 0:
                days_ahead = 7
            return now + timedelta(days=days_ahead)

    # Plain weekday name → nearest upcoming (not today)
    if hint in _WEEKDAY_NAMES:
        target_wd = _WEEKDAY_NAMES.index(hint)
        current_wd = now.weekday()
        days_ahead = (target_wd - current_wd) % 7
        if days_ahead == 0:
            days_ahead = 7
        return now + timedelta(days=days_ahead)

    return None


def _resolve_time(time_hint: str) -> Optional[tuple[int, int]]:
    """Convert a time hint string to (hour, minute).

    Heuristic for bare hour: 1-7 → PM, 8-12 → AM.
    Returns None if can't parse.
    """
    if not time_hint:
        return None
    m = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)?', time_hint.lower())
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = m.group(3)
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    elif ampm is None:
        if 1 <= hour <= 7:
            hour += 12
        # 8-12 kept as-is (AM)
    return (hour, minute)


def _extract_title(text: str) -> str:
    """Extract event title from normalized NL text."""
    # "called/named X" — stop at time/date keywords
    m = re.search(
        r'(?:called|named|titled)\s+["\']?([\w][\w\s]*?)(?:\s+(?:at|on|this|tomorrow|today|next|monday|tuesday|wednesday|thursday|friday|saturday|sunday|morning|afternoon|evening|tonight)|["\']|$)',
        text,
    )
    if m:
        return m.group(1).strip().title()

    # "block off ... for X" — look for "for <title>" explicitly
    m = re.search(r'\bfor\s+([\w][\w\s]*?)(?:\s+(?:at|on|this|tomorrow|today|next|morning|afternoon|evening|tonight)|$)', text)
    if m and "block off" in text:
        candidate = m.group(1).strip()
        if candidate and candidate.lower() not in _DATE_STOP_WORDS:
            return candidate.title()

    # Verb + (optional article) + title words (stop before date/time keywords)
    for verb in _CREATE_VERBS:
        if verb not in text:
            continue
        idx = text.index(verb) + len(verb)
        rest = text[idx:].lstrip()
        # Strip article
        for article in ("an ", "a ", "the "):
            if rest.startswith(article):
                rest = rest[len(article):]
                break
        # Collect words until a stop word
        words = rest.split()
        title_words = []
        for word in words:
            clean = word.strip(".,?!")
            if clean in _DATE_STOP_WORDS:
                break
            title_words.append(clean)
        if title_words:
            return " ".join(title_words).title()

    return ""


def parse_calendar_create_request(
    user_message: str,
    now: Optional[datetime] = None,
) -> dict:
    """
    Parse a natural-language calendar creation request into a structured draft.

    Returns a dict with keys:
      title, date_hint, date_str, time_hint, window_hint,
      start_time_str, end_time_str, duration_minutes,
      needs_availability_search, missing_fields
    """
    if now is None:
        now = datetime.now()
    today = now.date()

    text = " ".join(user_message.strip().lower().split())

    title = _extract_title(text)
    date_hint = _extract_date_hint(text)
    time_hint, window_hint = _extract_time_hint(text)

    # Resolve date
    resolved_date = _resolve_date(date_hint, today) if date_hint else None
    date_str = resolved_date.isoformat() if resolved_date else ""

    # Resolve time or mark as window-based
    duration_minutes = _default_duration(title) if title else 60
    # Override with explicit duration if stated ("90 minutes", "1 hour", etc.)
    explicit_dur = _extract_explicit_duration(text)
    if explicit_dur is not None:
        duration_minutes = explicit_dur
    start_time_str = ""
    end_time_str = ""
    needs_availability_search = False

    if window_hint:
        needs_availability_search = True
    elif time_hint:
        resolved_time = _resolve_time(time_hint)
        if resolved_time is not None:
            h, m = resolved_time
            start_time_str = f"{h:02d}:{m:02d}:00"
            end_h, end_m = divmod(h * 60 + m + duration_minutes, 60)
            end_h = end_h % 24
            end_time_str = f"{end_h:02d}:{end_m:02d}:00"

    # Determine missing fields
    missing_fields: list[str] = []
    if not title:
        missing_fields.append("title")
    if not date_str:
        missing_fields.append("date")
    if not window_hint and not start_time_str:
        missing_fields.append("time")

    return {
        "title": title,
        "date_hint": date_hint,
        "date_str": date_str,
        "time_hint": time_hint,
        "window_hint": window_hint,
        "start_time_str": start_time_str,
        "end_time_str": end_time_str,
        "duration_minutes": duration_minutes,
        "needs_availability_search": needs_availability_search,
        "missing_fields": missing_fields,
    }


# ── Availability search ───────────────────────────────────────────────────────

def _find_availability_slot(draft: dict) -> Optional[dict]:
    """
    Search for a free slot matching the draft's window and duration.

    Returns dict with {"start_time_str": "HH:MM:SS", "end_time_str": "HH:MM:SS"}
    or None if no slot found or calendar unavailable.
    """
    window_hint = draft.get("window_hint", "")
    date_str = draft.get("date_str", "")
    duration_minutes = int(draft.get("duration_minutes", 60))

    if not date_str or not window_hint:
        return None

    window_hours = _WINDOW_MAP.get(window_hint)
    if not window_hours:
        return None

    day_start_hour, day_end_hour = window_hours

    try:
        from prometheus.agents.calendar_read_tools import calendar_find_free_blocks
        result = calendar_find_free_blocks(
            date_str,
            minimum_minutes=duration_minutes,
            day_start_hour=day_start_hour,
            day_end_hour=day_end_hour,
        )
    except Exception:
        return None

    if not result.get("ok"):
        return None

    free_blocks = result.get("free_blocks", [])
    if not free_blocks:
        return None

    # Use the first available block
    block = free_blocks[0]
    start_iso = block.get("start", "")
    if not start_iso:
        return None

    try:
        start_dt = datetime.fromisoformat(start_iso)
    except ValueError:
        return None

    start_h = start_dt.hour
    start_m = start_dt.minute
    start_time_str = f"{start_h:02d}:{start_m:02d}:00"
    end_h, end_m = divmod(start_h * 60 + start_m + duration_minutes, 60)
    end_h = end_h % 24
    end_time_str = f"{end_h:02d}:{end_m:02d}:00"

    return {"start_time_str": start_time_str, "end_time_str": end_time_str}


# ── Auto-execute classification ───────────────────────────────────────────────

def should_auto_execute_calendar_create(
    draft: dict,
    user_message: str,
) -> tuple[bool, str]:
    """Return (True, "") if the request is safe to direct-create without confirmation.

    Returns (False, reason) if the request should use the pending confirmation flow.
    """
    text = user_message.strip().lower()

    # Window-based requests always need the availability search → pending path
    if draft.get("needs_availability_search"):
        return False, "window-based time requires availability search"

    # Must have all fields resolved
    if draft.get("missing_fields"):
        return False, f"missing fields: {draft['missing_fields']}"

    # Duration safety cap: >4 hours → confirm
    if draft.get("duration_minutes", 60) > 240:
        return False, "event duration over 4 hours"

    # Recurring indicators
    for word in ("every ", "recurring", "weekly", "daily", "repeat", "each "):
        if word in text:
            return False, "recurring event detected"

    # All-day event
    for phrase in ("all day", "all-day", "full day"):
        if phrase in text:
            return False, "all-day event"

    # Multi-day range
    for word in (" through ", " until ", " thru "):
        if word in text:
            return False, "potential multi-day event"

    # Invite attendees — "with " is a proxy; catches "meeting with Jake"
    if " with " in text:
        return False, "event may include attendees"

    # Destructive ops that should never auto-fire
    for word in ("move", "reschedule", "delete", "cancel", "update", "change"):
        if word in text:
            return False, "update/reschedule/delete detected"

    # Sleep hours: before 6 AM or at/after 11 PM
    start_time_str = draft.get("start_time_str", "")
    if start_time_str:
        try:
            h = int(start_time_str[:2])
            if h < 6 or h >= 23:
                return False, "event during sleep hours"
        except (ValueError, IndexError):
            pass

    return True, ""


def _check_conflict(draft: dict) -> Optional[str]:
    """Check the calendar for an overlapping event. Returns conflicting title or None.

    On any failure (API unavailable, import error, parse error), returns None so
    the caller can proceed with the direct create.
    """
    try:
        get_date_fn = _calendar_get_date_fn
        if get_date_fn is None:
            return None
        date_str = draft.get("date_str", "")
        start_str = draft.get("start_time_str", "")
        end_str = draft.get("end_time_str", "")
        if not date_str or not start_str or not end_str:
            return None

        result = get_date_fn(date_str)
        if not result.get("ok"):
            return None

        def to_mins(t: str) -> int:
            return int(t[:2]) * 60 + int(t[3:5])

        new_start = to_mins(start_str)
        new_end = to_mins(end_str)

        for ev in result.get("events", []):
            ev_start_iso = str(ev.get("start", ""))
            ev_end_iso = str(ev.get("end", ev_start_iso))
            if "T" not in ev_start_iso:
                continue  # skip all-day events
            ev_start = to_mins(ev_start_iso[11:19])
            ev_end = to_mins(ev_end_iso[11:19]) if "T" in ev_end_iso else ev_start + 60
            if not (new_end <= ev_start or new_start >= ev_end):
                return str(ev.get("summary", "an existing event"))
    except Exception:
        return None
    return None


def _direct_create_calendar_event(user_request: str, draft: dict) -> dict:
    """Execute a calendar create directly without a pending confirmation file.

    Uses approval_mode 'direct_user_command' — the explicit user statement IS
    the approval. Env gates (GOOGLE_CALENDAR_ENABLED, DRY_RUN) are still
    enforced by the executor.
    """
    operation = _build_operation(draft)
    request_id = f"req-direct-{uuid.uuid4().hex[:12]}"

    ensure_lumen_executor_dirs()

    now_utc = datetime.now(timezone.utc).isoformat()

    # Write reviewed record
    reviewed = {
        "request_id": request_id,
        "reviewed_at": now_utc,
        "all_dry_run": True,
        "approved": False,
        "original_operations": [operation],
        "results": [],
        "no_live_execution": True,
        "source": "nl_calendar_create_direct",
        "approval_mode": "direct_user_command",
    }
    reviewed_path = REVIEWED_LUMEN_DIR / f"reviewed_{request_id}.json"
    reviewed_path.parent.mkdir(parents=True, exist_ok=True)
    reviewed_path.write_text(json.dumps(reviewed, indent=2), encoding="utf-8")

    # Write approval record — user's explicit command is the approval
    approval = {
        "request_id": request_id,
        "approved": True,
        "approved_by": "user_voice",
        "approved_at": now_utc,
        "reviewed_path": str(reviewed_path),
        "operation_count": 1,
        "explicit_user_approval_required": False,
        "approval_mode": "direct_user_command",
        "source": "nl_calendar_create_direct",
    }
    approval_path = APPROVED_LUMEN_DIR / f"approved_{request_id}.json"
    approval_path.parent.mkdir(parents=True, exist_ok=True)
    approval_path.write_text(json.dumps(approval, indent=2), encoding="utf-8")

    # Execute through existing pipeline
    try:
        exec_fn = execute_approved_calendar_request
        if exec_fn is None:
            from prometheus.agents.lumen_calendar_executor import execute_approved_calendar_request as _exec  # noqa: F821
            exec_fn = _exec
        exec_result = exec_fn(request_id)
    except Exception as exc:
        return {
            "status": "failed",
            "error": f"Executor error: {exc}",
            "title": draft.get("title", ""),
            "request_id": request_id,
        }

    success = bool(exec_result.get("success"))
    reason = exec_result.get("reason") or exec_result.get("message", "")

    blocked = (
        not success
        and reason is not None
        and "GOOGLE_CALENDAR_DRY_RUN" in str(reason)
    )

    if blocked:
        return {
            "status": "blocked",
            "reason": reason,
            "title": draft.get("title", ""),
            "request_id": request_id,
        }

    if not success:
        return {
            "status": "failed",
            "reason": reason,
            "title": draft.get("title", ""),
            "request_id": request_id,
        }

    return {
        "status": "executed",
        "title": draft.get("title", ""),
        "start_time": operation.get("start_time", ""),
        "end_time": operation.get("end_time", ""),
        "request_id": request_id,
        "date_hint": draft.get("date_hint", ""),
        "date_str": draft.get("date_str", ""),
    }


# ── Summary building ──────────────────────────────────────────────────────────

def _format_time_range(start_time_str: str, end_time_str: str) -> str:
    """Format 'HH:MM:SS' strings to human-readable time range."""
    def fmt(t: str) -> str:
        try:
            h, m = int(t[:2]), int(t[3:5])
            suffix = "AM" if h < 12 else "PM"
            display_h = h if h <= 12 else h - 12
            if display_h == 0:
                display_h = 12
            if m:
                return f"{display_h}:{m:02d} {suffix}"
            return f"{display_h} {suffix}"
        except (ValueError, IndexError):
            return t

    return f"{fmt(start_time_str)}–{fmt(end_time_str)}"


def _human_summary(draft: dict) -> str:
    """Build a natural-language summary for user confirmation."""
    title = draft.get("title", "the event")
    date_str = draft.get("date_str", "")
    date_hint = draft.get("date_hint", "")
    start_time_str = draft.get("start_time_str", "")
    end_time_str = draft.get("end_time_str", "")

    date_label = date_hint if date_hint else (date_str or "that day")
    if date_label not in ("today", "tomorrow") and date_str:
        # Use YYYY-MM-DD date label for non-relative dates
        date_label = date_str

    if start_time_str and end_time_str:
        time_range = _format_time_range(start_time_str, end_time_str)
        return f"I can add '{title}' {date_label} from {time_range}. Confirm?"
    elif start_time_str:
        h, m = int(start_time_str[:2]), int(start_time_str[3:5])
        suffix = "AM" if h < 12 else "PM"
        display_h = h if h <= 12 else h - 12
        if display_h == 0:
            display_h = 12
        time_label = f"{display_h}:{m:02d} {suffix}" if m else f"{display_h} {suffix}"
        return f"I can add '{title}' {date_label} at {time_label}. Confirm?"
    return f"I can add '{title}' on {date_label}. Confirm?"


def _missing_fields_prompt(draft: dict) -> str:
    """Build a clarifying question for missing fields."""
    missing = draft.get("missing_fields", [])
    title = draft.get("title", "")
    if "title" in missing:
        return "What should I call the event?"
    if "date" in missing and "time" in missing:
        event_label = f"the {title}" if title else "it"
        return f"When should I schedule {event_label}? I need a date and time."
    if "date" in missing:
        event_label = f"the {title}" if title else "it"
        return f"What date should I put {event_label} on?"
    if "time" in missing:
        event_label = f"the {title}" if title else "it"
        return f"What time should I schedule {event_label}?"
    return "I need a bit more detail. What date and time?"


# ── Operation building ────────────────────────────────────────────────────────

def _build_operation(draft: dict) -> dict:
    """Build a Lumen-format calendar operation from a parsed draft."""
    date_str = draft["date_str"]
    start_time_str = draft["start_time_str"]
    end_time_str = draft["end_time_str"]

    start_time = f"{date_str}T{start_time_str}"
    end_time = f"{date_str}T{end_time_str}"

    return {
        "operation_type": "create_event",
        "title": draft["title"],
        "start_time": start_time,
        "end_time": end_time,
        "calendar_id": "primary",
        "requires_prometheus_approval": True,
        "dry_run": True,
    }


# ── Pending confirmation file management ──────────────────────────────────────

def _confirmation_path(confirmation_id: str) -> Path:
    return PENDING_CALENDAR_CONFIRMATIONS_DIR / f"pending_cal_confirm_{confirmation_id}.json"


def _write_pending_confirmation(
    user_request: str,
    draft: dict,
    operation: dict,
    human_summary: str,
) -> dict:
    """Write a pending confirmation file. Returns the confirmation dict."""
    ensure_calendar_confirmation_dir()
    confirmation_id = uuid.uuid4().hex[:16]
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(hours=24)).isoformat()

    record = {
        "confirmation_id": confirmation_id,
        "created_at": now.isoformat(),
        "expires_at": expires_at,
        "user_request": user_request,
        "draft": draft,
        "proposed_operation": operation,
        "human_summary": human_summary,
        "status": "pending",
    }

    _confirmation_path(confirmation_id).write_text(
        json.dumps(record, indent=2), encoding="utf-8"
    )
    return record


def has_pending_calendar_confirmation() -> bool:
    """Return True if there is at least one non-expired, non-canceled pending confirmation."""
    return get_most_recent_pending_confirmation() is not None


def get_most_recent_pending_confirmation() -> Optional[dict]:
    """Return the most recent unexpired pending confirmation, or None."""
    if not PENDING_CALENDAR_CONFIRMATIONS_DIR.exists():
        return None

    now = datetime.now(timezone.utc)
    candidates: list[tuple[str, dict]] = []

    for p in PENDING_CALENDAR_CONFIRMATIONS_DIR.glob("pending_cal_confirm_*.json"):
        try:
            record = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if record.get("status") != "pending":
            continue
        expires_str = record.get("expires_at", "")
        if expires_str:
            try:
                expires = datetime.fromisoformat(expires_str)
                if now > expires:
                    continue
            except ValueError:
                pass
        candidates.append((record.get("created_at", ""), record))

    if not candidates:
        return None

    # Most recent by created_at
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _mark_confirmation_status(confirmation_id: str, status: str) -> None:
    """Update status field in a pending confirmation file."""
    path = _confirmation_path(confirmation_id)
    if not path.exists():
        return
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        record["status"] = status
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        pass


# ── Main flow functions ───────────────────────────────────────────────────────

def parse_and_propose(user_request: str) -> dict:
    """
    Parse a NL calendar create request and either auto-execute or ask for confirmation.

    Returns dict with status:
      "executed"       — direct create succeeded (low-risk, fully-specified request)
      "blocked"        — direct create blocked by DRY_RUN env gate
      "failed"         — direct create failed (executor error)
      "conflict"       — overlapping event detected; pending confirmation written
      "pending"        — confirmation file written; awaiting user yes/no
      "needs_input"    — missing date, time, or title; ask a clarifying question
      "no_availability"— window-based search found no free slot
    """
    now = datetime.now()
    draft = parse_calendar_create_request(user_request, now)

    if draft["missing_fields"]:
        return {
            "status": "needs_input",
            "confirmation_id": None,
            "human_summary": _missing_fields_prompt(draft),
            "missing_fields": draft["missing_fields"],
            "draft": draft,
        }

    if draft["needs_availability_search"]:
        slot = _find_availability_slot(draft)
        if slot is None:
            window = draft.get("window_hint", "requested time")
            return {
                "status": "no_availability",
                "confirmation_id": None,
                "human_summary": (
                    f"I checked your calendar but couldn't find a free "
                    f"{draft['duration_minutes']}-minute block in the {window} "
                    f"on {draft.get('date_hint') or draft.get('date_str', 'that day')}."
                ),
                "missing_fields": [],
                "draft": draft,
            }
        draft["start_time_str"] = slot["start_time_str"]
        draft["end_time_str"] = slot["end_time_str"]
        draft["needs_availability_search"] = False
        # Window-based: propose with confirmation even after finding a slot
        operation = _build_operation(draft)
        human_summary = _human_summary(draft)
        pending_record = _write_pending_confirmation(user_request, draft, operation, human_summary)
        return {
            "status": "pending",
            "confirmation_id": pending_record["confirmation_id"],
            "human_summary": human_summary,
            "missing_fields": [],
            "draft": draft,
        }

    # Exact time specified — check if safe to auto-execute
    auto_ok, _reason = should_auto_execute_calendar_create(draft, user_request)
    if auto_ok:
        conflict_event = _check_conflict(draft)
        if conflict_event:
            # Overlapping event — write pending so user can still confirm or cancel
            operation = _build_operation(draft)
            human_summary = _human_summary(draft)
            pending_record = _write_pending_confirmation(user_request, draft, operation, human_summary)
            return {
                "status": "conflict",
                "confirmation_id": pending_record["confirmation_id"],
                "conflict_event": conflict_event,
                "human_summary": human_summary,
                "missing_fields": [],
                "draft": draft,
            }
        return _direct_create_calendar_event(user_request, draft)

    # High-risk or ambiguous → ask for confirmation
    operation = _build_operation(draft)
    human_summary = _human_summary(draft)
    pending_record = _write_pending_confirmation(user_request, draft, operation, human_summary)
    return {
        "status": "pending",
        "confirmation_id": pending_record["confirmation_id"],
        "human_summary": human_summary,
        "missing_fields": [],
        "draft": draft,
    }


def confirm_pending_calendar_confirmation(
    confirmation_id: Optional[str] = None,
) -> dict:
    """
    Execute the most recent (or specific) pending calendar confirmation.

    Writes reviewed + approval records, then calls execute_approved_calendar_request().
    Returns result dict with: success, blocked, title, start_time, request_id.
    """
    if confirmation_id:
        record = None
        path = _confirmation_path(confirmation_id)
        if path.exists():
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
    else:
        record = get_most_recent_pending_confirmation()

    if record is None:
        return {"success": False, "no_pending": True, "blocked": False}

    conf_id = record["confirmation_id"]
    operation = record.get("proposed_operation", {})
    draft = record.get("draft", {})

    if not operation or operation.get("operation_type") != "create_event":
        return {
            "success": False,
            "no_pending": False,
            "blocked": False,
            "error": "Pending confirmation has no valid create_event operation.",
        }

    # Build request_id
    request_id = f"req-nlcal-{conf_id}"

    ensure_lumen_executor_dirs()
    ensure_calendar_confirmation_dir()

    now_utc = datetime.now(timezone.utc).isoformat()

    # Write reviewed record
    reviewed = {
        "request_id": request_id,
        "reviewed_at": now_utc,
        "all_dry_run": True,
        "approved": False,
        "original_operations": [operation],
        "results": [],
        "no_live_execution": True,
        "source": "nl_calendar_create",
    }
    reviewed_path = REVIEWED_LUMEN_DIR / f"reviewed_{request_id}.json"
    reviewed_path.parent.mkdir(parents=True, exist_ok=True)
    reviewed_path.write_text(json.dumps(reviewed, indent=2), encoding="utf-8")

    # Write approval record
    approval = {
        "request_id": request_id,
        "approved": True,
        "approved_by": "user_voice",
        "approved_at": now_utc,
        "reviewed_path": str(reviewed_path),
        "operation_count": 1,
        "explicit_user_approval_required": True,
        "source": "nl_calendar_create",
    }
    approval_path = APPROVED_LUMEN_DIR / f"approved_{request_id}.json"
    approval_path.parent.mkdir(parents=True, exist_ok=True)
    approval_path.write_text(json.dumps(approval, indent=2), encoding="utf-8")

    # Execute through existing pipeline
    try:
        exec_fn = execute_approved_calendar_request
        if exec_fn is None:
            from prometheus.agents.lumen_calendar_executor import execute_approved_calendar_request as exec_fn  # noqa: F821
        exec_result = exec_fn(request_id)
    except Exception as exc:
        _mark_confirmation_status(conf_id, "failed")
        return {
            "success": False,
            "no_pending": False,
            "blocked": False,
            "error": f"Executor error: {exc}",
            "title": operation.get("title", ""),
            "request_id": request_id,
        }

    success = bool(exec_result.get("success"))
    reason = exec_result.get("reason") or exec_result.get("message", "")

    blocked = (
        not success
        and reason is not None
        and "GOOGLE_CALENDAR_DRY_RUN" in str(reason)
    )

    _mark_confirmation_status(conf_id, "confirmed" if success else "failed")

    return {
        "success": success,
        "no_pending": False,
        "blocked": blocked,
        "title": operation.get("title", ""),
        "start_time": operation.get("start_time", ""),
        "end_time": operation.get("end_time", ""),
        "request_id": request_id,
        "reason": reason,
        "operation_count": exec_result.get("operation_count", 1),
    }


def cancel_pending_calendar_confirmation(
    confirmation_id: Optional[str] = None,
) -> dict:
    """
    Cancel the most recent (or specific) pending calendar confirmation.

    Returns dict with: canceled, human_summary.
    """
    if confirmation_id:
        record = None
        path = _confirmation_path(confirmation_id)
        if path.exists():
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
    else:
        record = get_most_recent_pending_confirmation()

    if record is None:
        return {"canceled": False, "no_pending": True}

    conf_id = record["confirmation_id"]
    human_summary = record.get("human_summary", "")
    draft = record.get("draft", {})
    title = draft.get("title", "the event")

    _mark_confirmation_status(conf_id, "canceled")

    return {
        "canceled": True,
        "no_pending": False,
        "human_summary": human_summary,
        "title": title,
    }

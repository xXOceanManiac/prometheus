"""
prometheus/routines/calendar_event_triggers.py — Calendar Event Trigger Engine v1.

Polls Google Calendar every POLL_SECONDS for upcoming events.
Schedules asyncio tasks to fire at exact event start times.
Deduplicates via a persistent state file and in-memory set.
Routes known event titles to registered CalendarRoutineRule handlers.
Falls back to a spoken notification for unrecognized events.

Config (env vars):
  PROMETHEUS_CALENDAR_TRIGGER_POLL_SECONDS      — poll interval (default: 30)
  PROMETHEUS_CALENDAR_TRIGGER_LOOKAHEAD_MINUTES — how far ahead to schedule (default: 10)
  PROMETHEUS_CALENDAR_EVENT_GRACE_SECONDS       — late-start grace for default events (default: 120)
  PROMETHEUS_CALENDAR_EVENT_NOTIFICATIONS_ENABLED — default event spoken notifications (default: true)
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from utils import log_event

# ── State file ────────────────────────────────────────────────────────────────

_DEFAULT_STATE_PATH = (
    Path.home() / "Desktop" / "PROMETHEUS" / "state" / "calendar_event_triggers_state.json"
)


# ── Config helpers ────────────────────────────────────────────────────────────

def _cfg_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if raw in ("1", "true", "yes"):
        return True
    if raw in ("0", "false", "no"):
        return False
    return default


def _cfg_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip())
    except (ValueError, TypeError):
        return default


# ── DateTime helpers ──────────────────────────────────────────────────────────

def _parse_dt(dt_str: str) -> datetime:
    """Parse ISO datetime string, handling Z and offset suffixes."""
    if dt_str.endswith("Z"):
        return datetime.fromisoformat(dt_str[:-1] + "+00:00")
    return datetime.fromisoformat(dt_str)


def _event_key(event_id: Optional[str], start_time: str) -> str:
    """Stable dedup key: event_id + start_time. New start_time = new key."""
    return f"{event_id or ''}_{start_time or ''}"


def _normalize_cmp(dt: datetime, reference: datetime) -> tuple[datetime, datetime]:
    """Normalize tzinfo so two datetimes can be compared without TypeError."""
    if dt.tzinfo is not None and reference.tzinfo is None:
        return dt, reference.replace(tzinfo=dt.tzinfo)
    if dt.tzinfo is None and reference.tzinfo is not None:
        return dt.replace(tzinfo=reference.tzinfo), reference
    return dt, reference


# ── CalendarRoutineRule ───────────────────────────────────────────────────────

@dataclass
class CalendarRoutineRule:
    """
    Maps a calendar event title pattern to an async handler.

    match_title_contains: lowercase substrings — any match triggers this rule.
    handler:              async (event) -> None
    allow_late_seconds:   grace window for late-start recovery (seconds).
    """
    name: str
    match_title_contains: list
    handler: Any  # Callable[[Any], Awaitable[None]]
    allow_late_seconds: int = 120


# ── Calendar event adapter ────────────────────────────────────────────────────

class _CalendarEventAdapter:
    """Dict → attribute-accessible event. Compatible with morning_routine.py."""

    __slots__ = ("title", "start_time", "event_id")

    def __init__(self, raw: dict) -> None:
        self.title = raw.get("title", "") or ""
        self.start_time = raw.get("start_time", "") or ""
        self.event_id = raw.get("event_id")


# ── TriggerCalendarReader ─────────────────────────────────────────────────────

class TriggerCalendarReader:
    """
    Returns upcoming timed events for the next 24 hours from Google Calendar.
    Returns _CalendarEventAdapter list. Returns [] on any failure.
    """

    def get_upcoming_events(self) -> list:
        try:
            from prometheus.agents.calendar_read_tools import calendar_list_upcoming
            result = calendar_list_upcoming(max_results=50, days=1)
            if not result.get("ok"):
                log_event("calendar_trigger_reader_error", {
                    "error": str(result.get("error", "unknown"))[:200],
                })
                return []
            events = []
            for e in result.get("events", []):
                start = e.get("start_time", "")
                if "T" not in start:
                    continue  # skip all-day events
                events.append(_CalendarEventAdapter(e))
            return events
        except Exception as exc:
            log_event("calendar_trigger_reader_error", {"error": str(exc)[:200]})
            return []


# ── CalendarEventTriggerEngine ────────────────────────────────────────────────

class CalendarEventTriggerEngine:
    """
    Polls Google Calendar and fires handlers at exact event start times.

    calendar_reader:  synchronous object with get_upcoming_events() -> list
    speaker_fn:       async (text: str) -> None — used for default notifications
    rules:            CalendarRoutineRule list; matched in registration order
    state_path:       path for the fired-events dedup state file
    logger:           callable(event: str, payload: dict) — defaults to log_event
    """

    def __init__(
        self,
        calendar_reader: Any,
        speaker_fn: Any,
        rules: Optional[list] = None,
        state_path: Optional[Path] = None,
        logger: Any = None,
    ) -> None:
        self._calendar = calendar_reader
        self._speaker_fn = speaker_fn
        self._rules: list[CalendarRoutineRule] = list(rules or [])
        self._state_path = state_path or _DEFAULT_STATE_PATH
        self._log = logger or log_event
        self._stopped = False

        # In-memory: keys currently scheduled (prevents double-scheduling in one session)
        self._scheduled: set[str] = set()

        # Persisted: {event_key: fired_at_iso}
        self._fired_state: dict[str, str] = {}
        self._state_loaded = False

    def stop(self) -> None:
        self._stopped = True

    def register_rule(self, rule: CalendarRoutineRule) -> None:
        self._rules.append(rule)

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Main poll loop — runs until stop() is called."""
        poll_seconds = float(_cfg_int("PROMETHEUS_CALENDAR_TRIGGER_POLL_SECONDS", 30))
        self._log("calendar_trigger_engine_started", {
            "poll_seconds": poll_seconds,
            "rules": [r.name for r in self._rules],
        })
        print("[CALTRIG] engine started", flush=True)
        # Poll immediately on startup to catch events that fired while we were offline
        await self._poll()
        while not self._stopped:
            try:
                await asyncio.sleep(poll_seconds)
                if self._stopped:
                    break
                await self._poll()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._log("calendar_trigger_engine_error", {"error": str(exc)[:200]})

    # ── Poll cycle ────────────────────────────────────────────────────────────

    async def _poll(self) -> None:
        """Fetch upcoming events and schedule tasks for those starting soon."""
        lookahead_minutes = _cfg_int("PROMETHEUS_CALENDAR_TRIGGER_LOOKAHEAD_MINUTES", 10)
        lookahead_seconds = lookahead_minutes * 60.0

        self._log("calendar_trigger_poll_started", {"lookahead_minutes": lookahead_minutes})

        loop = asyncio.get_running_loop()
        try:
            events = await loop.run_in_executor(None, self._calendar.get_upcoming_events)
        except Exception as exc:
            self._log("calendar_trigger_poll_error", {"error": str(exc)[:200]})
            return

        self._log("calendar_trigger_events_fetched", {"count": len(events)})

        now = datetime.now()
        for event in events:
            try:
                self._consider_event(event, now, lookahead_seconds)
            except Exception as exc:
                self._log("calendar_trigger_consider_error", {
                    "title": getattr(event, "title", ""),
                    "error": str(exc)[:200],
                })

    def _consider_event(self, event: Any, now: datetime, lookahead_seconds: float) -> None:
        """Decide whether to schedule this event for triggering."""
        start_str = getattr(event, "start_time", "") or ""
        if "T" not in start_str:
            return  # all-day event — ignored

        try:
            start_dt = _parse_dt(start_str)
        except (ValueError, TypeError):
            return

        start_cmp, now_cmp = _normalize_cmp(start_dt, now)
        seconds_until = (start_cmp - now_cmp).total_seconds()

        key = _event_key(getattr(event, "event_id", None), start_str)
        rule = self._match_rule(event)
        default_grace = _cfg_int("PROMETHEUS_CALENDAR_EVENT_GRACE_SECONDS", 120)
        grace = rule.allow_late_seconds if rule else default_grace

        # Too far in the future — don't schedule yet
        if seconds_until > lookahead_seconds:
            return

        # Too far in the past — event is outside the grace window
        if seconds_until < -grace:
            self._log("calendar_trigger_event_skipped", {
                "title": getattr(event, "title", ""),
                "start": start_str,
                "reason": "too_late",
                "seconds_past": int(-seconds_until),
            })
            return

        # Already scheduled this session or already fired
        if key in self._scheduled:
            return
        if self._is_fired(key):
            return

        # Schedule it
        wait = max(0.0, seconds_until)
        self._scheduled.add(key)
        routine_name = rule.name if rule else "default_notification"

        self._log("calendar_trigger_event_scheduled", {
            "title": getattr(event, "title", ""),
            "start": start_str,
            "wait_seconds": round(wait, 1),
            "routine": routine_name,
        })
        print(
            f"[CALTRIG] scheduled {getattr(event, 'title', '')!r} "
            f"in {wait:.0f}s → {routine_name}",
            flush=True,
        )
        asyncio.ensure_future(self._fire_event_delayed(event, rule, wait, key))

    # ── Fire logic ────────────────────────────────────────────────────────────

    async def _fire_event_delayed(
        self,
        event: Any,
        rule: Optional[CalendarRoutineRule],
        wait_seconds: float,
        key: str,
    ) -> None:
        """Sleep wait_seconds, then fire the handler."""
        try:
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)

            if self._stopped:
                return

            # Re-check: already fired by another path?
            if self._is_fired(key):
                self._log("calendar_trigger_event_skipped", {
                    "key": key,
                    "reason": "already_fired",
                })
                return

            # Re-check: still within grace window after sleep?
            start_str = getattr(event, "start_time", "") or ""
            if start_str and "T" in start_str:
                try:
                    start_dt = _parse_dt(start_str)
                    now = datetime.now()
                    start_cmp, now_cmp = _normalize_cmp(start_dt, now)
                    seconds_late = (now_cmp - start_cmp).total_seconds()
                    default_grace = _cfg_int("PROMETHEUS_CALENDAR_EVENT_GRACE_SECONDS", 120)
                    grace = rule.allow_late_seconds if rule else default_grace
                    if seconds_late > grace:
                        self._log("calendar_trigger_event_skipped", {
                            "title": getattr(event, "title", ""),
                            "start": start_str,
                            "reason": "too_late",
                            "seconds_late": round(seconds_late, 1),
                        })
                        return
                except (ValueError, TypeError):
                    pass

            title = getattr(event, "title", "") or ""
            routine_name = rule.name if rule else "default_notification"
            self._log("calendar_trigger_event_fired", {
                "title": title,
                "start": start_str,
                "routine": routine_name,
            })
            print(f"[CALTRIG] fired {title!r} → {routine_name}", flush=True)

            try:
                if rule is not None:
                    await rule.handler(event)
                else:
                    await self._default_notify(event)
            except Exception as exc:
                self._log("calendar_trigger_handler_error", {
                    "title": title,
                    "routine": routine_name,
                    "error": str(exc)[:200],
                })

            self._mark_fired(key)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._log("calendar_trigger_fire_error", {
                "key": key,
                "error": str(exc)[:200],
            })
        finally:
            self._scheduled.discard(key)

    # ── Default spoken notification ───────────────────────────────────────────

    async def _default_notify(self, event: Any) -> None:
        """Speak a calendar event notification if notifications are enabled."""
        if not _cfg_bool("PROMETHEUS_CALENDAR_EVENT_NOTIFICATIONS_ENABLED", True):
            self._log("calendar_trigger_event_skipped", {
                "title": getattr(event, "title", ""),
                "reason": "disabled",
            })
            return
        title = (getattr(event, "title", "") or "").strip()
        message = f"Tate, {title} is starting now."
        try:
            await self._speaker_fn(message)
            self._log("calendar_event_notification_spoken", {
                "title": title,
                "message": message,
            })
        except Exception as exc:
            self._log("calendar_event_notification_failed", {
                "title": title,
                "error": str(exc)[:200],
            })

    # ── Rule matching ─────────────────────────────────────────────────────────

    def _match_rule(self, event: Any) -> Optional[CalendarRoutineRule]:
        """Return the first matching rule, or None for default notification."""
        title = (getattr(event, "title", "") or "").lower().strip()
        for rule in self._rules:
            for kw in rule.match_title_contains:
                if kw.lower() in title:
                    return rule
        return None

    # ── State: persistence ────────────────────────────────────────────────────

    def _load_state(self) -> None:
        if self._state_loaded:
            return
        self._state_loaded = True
        try:
            if self._state_path.exists():
                data = json.loads(self._state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "fired" in data:
                    self._fired_state = {
                        k: v for k, v in data["fired"].items()
                        if isinstance(k, str) and isinstance(v, str)
                    }
        except Exception as exc:
            self._log("calendar_trigger_state_load_error", {"error": str(exc)[:200]})
            self._fired_state = {}

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            data = {"fired": self._fired_state}
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, self._state_path)
        except Exception as exc:
            self._log("calendar_trigger_state_save_error", {"error": str(exc)[:200]})

    def _is_fired(self, key: str) -> bool:
        self._load_state()
        return key in self._fired_state

    def _mark_fired(self, key: str) -> None:
        self._load_state()
        self._fired_state[key] = datetime.now().isoformat(timespec="seconds")
        self._save_state()
        self._cleanup_old_fired()

    def _cleanup_old_fired(self) -> None:
        """Remove fired-event records older than 48 hours to keep the state file small."""
        cutoff_naive = datetime.now() - timedelta(hours=48)
        to_remove = []
        for k, v in list(self._fired_state.items()):
            try:
                dt = _parse_dt(v)
                if dt.tzinfo is not None:
                    dt = dt.replace(tzinfo=None)
                if dt < cutoff_naive:
                    to_remove.append(k)
            except Exception:
                pass
        if to_remove:
            for k in to_remove:
                del self._fired_state[k]
            self._save_state()

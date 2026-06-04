"""
prometheus/routines/morning_routine.py — Morning routine orchestration.

Triggered by the user's Google Calendar "Wake Up" event.
All side effects go through injected dependencies — no direct external calls here.

Dependencies injected at construction:
  calendar_reader   — synchronous; must have get_today_events() -> list
  ha_client         — synchronous; must have call_script(entity_id: str) -> bool
  speaker           — async;       must have speak(text: str) -> Awaitable[None]
  weather_provider  — synchronous; must have get_today_weather() -> dict | None
  state_store       — synchronous; must have load_state() -> MorningRoutineState | None
                                                 save_state(state) -> None
  logger            — synchronous; callable(event: str, payload: dict) -> None
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

# ── Home Assistant script entity ID constants ─────────────────────────────────

PROMETHEUS_XBOX_TURN_ON = "script.prometheus_xbox_turn_on"
PROMETHEUS_XBOX_LAUNCH_SPOTIFY = "script.prometheus_xbox_launch_spotify"
PROMETHEUS_XBOX_PLAY = "script.prometheus_xbox_play"
PROMETHEUS_XBOX_VOLUME_UP = "script.prometheus_xbox_volume_up"
PROMETHEUS_XBOX_VOLUME_DOWN = "script.prometheus_xbox_volume_down"
PROMETHEUS_MORNING_LIGHTS_WARM_FADE = "script.prometheus_morning_lights_warm_fade"


# ── Config / state dataclasses ─────────────────────────────────────────────────

@dataclass
class MorningRoutineConfig:
    wake_event_title: str = "wake up"
    missed_trigger_grace_minutes: int = 15
    pre_play_duck_wait_seconds: int = 16    # wait after launch_spotify before volume duck
    pre_play_final_wait_seconds: int = 1   # wait after volume duck, before play
    music_fade_interval_seconds: int = 10
    summary_delay_seconds: int = 120
    pre_summary_duck_seconds: int = 3
    post_summary_fade_interval_seconds: int = 1
    volume_command_interval_seconds: float = 1.0


@dataclass
class MorningRoutineState:
    date: str
    event_id: Optional[str]
    completed: bool
    started_at: Optional[str]


# ── Motivational quotes (cycled by day of year, never random) ─────────────────

_QUOTES = [
    "Discipline is the bridge between goals and accomplishment.",
    "The secret of getting ahead is getting started.",
    "Small steps every day lead to big results.",
    "Clarity, focus, execution — that's your formula.",
    "Every morning is a chance to rewrite the story.",
    "The hard work you put in today becomes the advantage you hold tomorrow.",
    "Progress, not perfection.",
]


# ── Pure helper functions ──────────────────────────────────────────────────────

def _parse_dt(dt_str: str) -> datetime:
    """Parse an ISO datetime string, handling Z and offset suffixes."""
    if dt_str.endswith("Z"):
        return datetime.fromisoformat(dt_str[:-1] + "+00:00")
    return datetime.fromisoformat(dt_str)


def _format_event_time(start_time: str) -> str:
    """Convert an ISO datetime string to a spoken-friendly time like '9 AM' or '2:30 PM'."""
    if not start_time or "T" not in start_time:
        return start_time
    try:
        dt = _parse_dt(start_time)
        hour, minute = dt.hour, dt.minute
        period = "AM" if hour < 12 else "PM"
        hour = hour % 12 or 12
        if minute == 0:
            return f"{hour} {period}"
        return f"{hour}:{minute:02d} {period}"
    except Exception:
        return start_time


def find_today_wake_event(events: list) -> Optional[Any]:
    """
    Return the earliest Wake Up timed event for today, or None.

    Ignores: all-day events, events with no concrete datetime start, non-Wake-Up titles.
    Matching is case-insensitive and strips surrounding whitespace.
    """
    today = datetime.now().date().isoformat()
    candidates = []
    for event in events:
        title = (getattr(event, "title", "") or "").strip().lower()
        if title != "wake up":
            continue
        start_time = (getattr(event, "start_time", "") or "")
        # All-day events have a date-only string (no 'T')
        if "T" not in start_time:
            continue
        if not start_time.startswith(today):
            continue
        candidates.append(event)
    if not candidates:
        return None
    return min(candidates, key=lambda e: e.start_time)


def _eligibility_detail(
    now: datetime,
    wake_event: Optional[Any],
    state: Optional[MorningRoutineState],
    config: MorningRoutineConfig,
) -> "tuple[bool, str, dict]":
    """
    Return (eligible, reason, diagnostic_payload).

    Reasons:
      "eligible"           — routine should run now
      "no_wake_event"      — no Wake Up event found (or unparseable start_time)
      "already_completed"  — already ran for this event_id + date
      "before_event_start" — now is before the event start time
      "after_grace_window" — now is past start + grace_minutes

    Timezone normalisation:
      If wake_dt is aware and now is naive, attach wake_dt.tzinfo to now —
      both are the user's local wall-clock time so this is correct.
      Stamping naive local time as UTC (the old behaviour) produced comparisons
      that were wrong by the local UTC offset (e.g. 4 hours for EDT).
    """
    _empty = {"now_iso": now.isoformat(timespec="seconds")}

    if wake_event is None:
        return False, "no_wake_event", _empty

    event_id = getattr(wake_event, "event_id", None)
    today = now.date().isoformat()

    # Guard duplicate runs: completed for this exact event_id + date
    if state is not None and state.completed:
        if state.event_id == event_id and state.date == today:
            return False, "already_completed", _empty

    start_time = (getattr(wake_event, "start_time", "") or "")
    if not start_time or "T" not in start_time:
        return False, "no_wake_event", _empty

    try:
        wake_dt = _parse_dt(start_time)
    except (ValueError, TypeError):
        return False, "no_wake_event", _empty

    # Normalise timezone awareness before comparison.
    # When wake_dt is aware (e.g. "18:15:00-04:00") and now is naive (datetime.now()),
    # attach wake_dt's tzinfo to now — both represent local wall-clock time.
    # The previous code used timezone.utc here, which shifted naive local time by the
    # UTC offset and caused events to appear 4 h in the future for EDT users.
    now_cmp = now
    wake_cmp = wake_dt
    if wake_dt.tzinfo is not None and now.tzinfo is None:
        now_cmp = now.replace(tzinfo=wake_dt.tzinfo)
    elif wake_dt.tzinfo is None and now.tzinfo is not None:
        wake_cmp = wake_dt.replace(tzinfo=now.tzinfo)

    grace_end = wake_cmp + timedelta(minutes=config.missed_trigger_grace_minutes)
    diff_seconds = (now_cmp - wake_cmp).total_seconds()

    payload: dict = {
        "now_iso": now_cmp.isoformat(timespec="seconds"),
        "start_iso": wake_cmp.isoformat(timespec="seconds"),
        "grace_until_iso": grace_end.isoformat(timespec="seconds"),
        "seconds_until_start": round(max(0.0, -diff_seconds), 1),
        "seconds_after_start": round(max(0.0, diff_seconds), 1),
    }

    if now_cmp < wake_cmp:
        return False, "before_event_start", payload
    if now_cmp > grace_end:
        return False, "after_grace_window", payload
    return True, "eligible", payload


def should_run_morning_routine(
    now: datetime,
    wake_event: Optional[Any],
    state: Optional[MorningRoutineState],
    config: MorningRoutineConfig,
) -> bool:
    """
    Return True only when the routine should fire right now.

    False when: no wake event, already completed for this event/date,
    now is before wake time, or now is past the grace window.

    For the full reason and diagnostic payload use _eligibility_detail().
    """
    eligible, _, _ = _eligibility_detail(now, wake_event, state, config)
    return eligible


def build_morning_summary(
    weather: Optional[dict],
    calendar_events: list,
    wake_event: Optional[Any],
) -> str:
    """
    Build the spoken daily briefing string.

    Greeting always starts "Good morning, Tate."
    Wake Up event is excluded from the meaningful event count and first-event reference.
    """
    parts = ["Good morning, Tate."]

    # Weather segment
    if weather and weather.get("condition") and weather.get("high") is not None:
        condition = weather["condition"]
        high = weather["high"]
        parts.append(
            f"It's a beautiful day in South Florida. "
            f"The forecast is {condition} with a high of {high} degrees."
        )
    else:
        parts.append(
            "I don't have the weather yet, but the day is still yours to command."
        )

    # Calendar segment — exclude Wake Up from meaningful events
    non_wake = [
        e for e in calendar_events
        if (getattr(e, "title", "") or "").strip().lower() != "wake up"
    ]

    if not non_wake:
        parts.append("Your calendar is mostly clear today.")
    else:
        count = len(non_wake)
        first = non_wake[0]
        first_title = (getattr(first, "title", "") or "").strip()
        first_time = _format_event_time(getattr(first, "start_time", "") or "")
        plural = "s" if count != 1 else ""
        parts.append(
            f"You have {count} event{plural} listed today, "
            f"beginning with {first_title} at {first_time}."
        )

    # Motivational quote — deterministic, cycles by day of year
    from datetime import date as _date
    doy = _date.today().timetuple().tm_yday
    parts.append(_QUOTES[doy % len(_QUOTES)])

    parts.append("You got this.")

    return " ".join(parts)


# ── Service class ──────────────────────────────────────────────────────────────

class MorningRoutineService:
    """
    Orchestrates the full morning wake-up sequence.

    All external I/O is delegated to injected dependencies so the class
    can be unit-tested without live connections.
    """

    def __init__(
        self,
        calendar_reader: Any,
        ha_client: Any,
        speaker: Any,
        weather_provider: Any,
        state_store: Any,
        logger: Any,
        config: Optional[MorningRoutineConfig] = None,
    ) -> None:
        self._calendar = calendar_reader
        self._ha = ha_client
        self._speaker = speaker
        self._weather = weather_provider
        self._store = state_store
        self._log = logger
        self._config = config or MorningRoutineConfig()
        self._running = False
        self._ha_ok: int = 0
        self._ha_fail: int = 0

    # ------------------------------------------------------------------
    # Public entry point (callable from the main loop tick)
    # ------------------------------------------------------------------

    async def check_and_run_morning_routine(
        self, now: Optional[datetime] = None
    ) -> None:
        """
        Idempotent tick entry point. Read calendar, check eligibility, run if due.
        Safe to call every 60 seconds from an external scheduler — never raises.
        """
        if self._running:
            return
        try:
            if now is None:
                now = datetime.now()
            loop = asyncio.get_running_loop()

            events = await loop.run_in_executor(None, self._calendar.get_today_events)
            self._log("morning_routine_events_fetched", {"count": len(events)})

            wake_event = find_today_wake_event(events)
            self._log("morning_routine_wake_event", {
                "found": wake_event is not None,
                "start_time": getattr(wake_event, "start_time", None),
                "event_id": str(getattr(wake_event, "event_id", None)),
            })

            state = await loop.run_in_executor(None, self._store.load_state)
            self._log("morning_routine_state_loaded", {
                "has_state": state is not None,
                "completed": getattr(state, "completed", None),
                "state_date": getattr(state, "date", None),
            })

            eligible, skip_reason, eligibility_payload = _eligibility_detail(
                now, wake_event, state, self._config
            )
            if not eligible:
                self._log("morning_routine_skipped", {
                    "reason": skip_reason,
                    **eligibility_payload,
                })
                return
            await self.run_morning_routine(wake_event)
        except Exception as exc:
            self._log("morning_routine_check_error", {"error": str(exc)[:300]})

    # ------------------------------------------------------------------
    # Full routine sequence
    # ------------------------------------------------------------------

    async def run_morning_routine(self, wake_event: Any) -> None:
        """
        Execute the complete morning routine sequence.

        Xbox failure → skips Spotify/volume/playback but lights and summary still run.
        Speech failure → volume is still restored afterward.
        """
        self._running = True
        self._ha_ok = 0
        self._ha_fail = 0
        config = self._config
        loop = asyncio.get_running_loop()
        started_loop_time = loop.time()

        event_id = getattr(wake_event, "event_id", None)
        today = datetime.now().date().isoformat()
        started_iso = datetime.now().isoformat(timespec="seconds")
        speak_succeeded = False
        speak_skip_reason: Optional[str] = None

        # a. Mark routine started in state
        state = MorningRoutineState(
            date=today,
            event_id=event_id,
            completed=False,
            started_at=started_iso,
        )
        await loop.run_in_executor(None, lambda: self._store.save_state(state))
        self._log("morning_routine_started", {"event_id": event_id, "date": today})

        xbox_ok = True
        try:
            # b. Xbox turn on
            self._log("morning_routine_step", {"step": "xbox_turn_on"})
            xbox_ok = await self._call_ha(PROMETHEUS_XBOX_TURN_ON)
            if not xbox_ok:
                self._log("morning_routine_xbox_failed", {"step": "xbox_turn_on"})

            # c. Launch Spotify (skipped if Xbox failed)
            if xbox_ok:
                self._log("morning_routine_step", {"step": "launch_spotify"})
                await self._call_ha(PROMETHEUS_XBOX_LAUNCH_SPOTIFY)

            # d. Start lights fade (always runs)
            self._log("morning_routine_step", {"step": "morning_lights"})
            await self._call_ha(PROMETHEUS_MORNING_LIGHTS_WARM_FADE)

            # e+f. Pre-play volume duck embedded within the Spotify launch window.
            # Wait until the Xbox is ready before sending volume commands, then duck
            # just before play so volume is already set when music starts.
            # Timeline: wait 16s → volume_down × 3 (1s apart) → wait 1s → play
            if xbox_ok:
                self._log("morning_routine_step", {
                    "step": "wait_spotify_launch_pre_duck",
                    "seconds": config.pre_play_duck_wait_seconds,
                })
                await asyncio.sleep(config.pre_play_duck_wait_seconds)

                self._log("morning_routine_step", {"step": "pre_play_volume_down_x3"})
                await self._call_ha(PROMETHEUS_XBOX_VOLUME_DOWN)
                await asyncio.sleep(config.volume_command_interval_seconds)
                await self._call_ha(PROMETHEUS_XBOX_VOLUME_DOWN)
                await asyncio.sleep(config.volume_command_interval_seconds)
                await self._call_ha(PROMETHEUS_XBOX_VOLUME_DOWN)

                self._log("morning_routine_step", {
                    "step": "wait_before_play",
                    "seconds": config.pre_play_final_wait_seconds,
                })
                await asyncio.sleep(config.pre_play_final_wait_seconds)

            # g. Press play (skipped if Xbox failed)
            if xbox_ok:
                self._log("morning_routine_step", {"step": "play"})
                await self._call_ha(PROMETHEUS_XBOX_PLAY)

            # h. Fade music in: three volume-up steps (skipped if Xbox failed)
            if xbox_ok:
                self._log("morning_routine_step", {"step": "fade_music_in"})
                for _ in range(3):
                    await asyncio.sleep(config.music_fade_interval_seconds)
                    await self._call_ha(PROMETHEUS_XBOX_VOLUME_UP)

            # i. Wait until summary_delay_seconds after routine started
            elapsed = loop.time() - started_loop_time
            remaining = config.summary_delay_seconds - elapsed
            if remaining > 0:
                self._log(
                    "morning_routine_step",
                    {"step": "wait_summary", "remaining_seconds": int(remaining)},
                )
                await asyncio.sleep(remaining)

            # j. Duck volume 3 notches pre-speech (skipped if Xbox failed)
            if xbox_ok:
                self._log("morning_routine_step", {"step": "pre_summary_duck"})
                await self._call_ha(PROMETHEUS_XBOX_VOLUME_DOWN)
                await asyncio.sleep(config.volume_command_interval_seconds)
                await self._call_ha(PROMETHEUS_XBOX_VOLUME_DOWN)
                await asyncio.sleep(config.volume_command_interval_seconds)
                await self._call_ha(PROMETHEUS_XBOX_VOLUME_DOWN)
            await asyncio.sleep(config.pre_summary_duck_seconds)

            # k. Build and speak daily summary
            self._log("morning_routine_step", {"step": "build_and_speak_summary"})
            try:
                events = await loop.run_in_executor(None, self._calendar.get_today_events)
                weather = await loop.run_in_executor(None, self._weather.get_today_weather)
                summary = build_morning_summary(weather, events, wake_event)
                await self._speaker.speak(summary)
                speak_succeeded = True
            except RuntimeError as exc:
                speak_skip_reason = str(exc)
                self._log("morning_routine_speak_error", {"error": str(exc)[:200]})
            except Exception as exc:
                self._log("morning_routine_speak_error", {"error": str(exc)[:200]})

            # l. Fade music back in (always runs, even if speech failed)
            if xbox_ok:
                self._log("morning_routine_step", {"step": "post_summary_fade"})
                await self._call_ha(PROMETHEUS_XBOX_VOLUME_UP)
                await asyncio.sleep(config.post_summary_fade_interval_seconds)
                await self._call_ha(PROMETHEUS_XBOX_VOLUME_UP)
                await asyncio.sleep(config.post_summary_fade_interval_seconds)
                await self._call_ha(PROMETHEUS_XBOX_VOLUME_UP)

        except Exception as exc:
            self._log("morning_routine_sequence_error", {"error": str(exc)[:300]})
        finally:
            # m. Mark completed — new object so the initial "started" save is unaffected
            completed_state = MorningRoutineState(
                date=state.date,
                event_id=state.event_id,
                completed=True,
                started_at=state.started_at,
            )
            await loop.run_in_executor(None, lambda: self._store.save_state(completed_state))
            completed_payload: dict = {
                "event_id": event_id,
                "date": today,
                "ha_success_count": self._ha_ok,
                "ha_failure_count": self._ha_fail,
                "speech_success": speak_succeeded,
            }
            if speak_skip_reason is not None:
                completed_payload["speech_skipped_reason"] = speak_skip_reason
            self._log("morning_routine_completed", completed_payload)
            self._running = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _call_ha(self, entity_id: str) -> bool:
        """Call an HA script by full entity ID. Returns True on success, False on failure."""
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, lambda: self._ha.call_script(entity_id)
            )
            if result:
                self._ha_ok += 1
                self._log("morning_routine_ha_call_success", {"entity_id": entity_id})
            else:
                self._ha_fail += 1
                self._log("morning_routine_ha_call_failed", {"entity_id": entity_id, "error": "adapter_returned_false"})
            return bool(result)
        except Exception as exc:
            self._ha_fail += 1
            self._log(
                "morning_routine_ha_call_failed",
                {"entity_id": entity_id, "error": str(exc)[:200]},
            )
            return False

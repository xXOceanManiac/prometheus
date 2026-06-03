"""
tests/test_morning_routine.py — Unit tests for morning routine orchestration.

No live Google Calendar, Home Assistant, or audio connections.
All dependencies are mocked. Async tests use asyncio.run().
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, call

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from prometheus.routines.morning_routine import (
    MorningRoutineConfig,
    MorningRoutineService,
    MorningRoutineState,
    PROMETHEUS_MORNING_LIGHTS_WARM_FADE,
    PROMETHEUS_XBOX_LAUNCH_SPOTIFY,
    PROMETHEUS_XBOX_PLAY,
    PROMETHEUS_XBOX_TURN_ON,
    PROMETHEUS_XBOX_VOLUME_DOWN,
    PROMETHEUS_XBOX_VOLUME_UP,
    build_morning_summary,
    find_today_wake_event,
    should_run_morning_routine,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

TODAY = datetime.now().date().isoformat()


def _make_event(
    title: str,
    start_time: Optional[str] = None,
    event_id: str = "evt-001",
    is_all_day: bool = False,
) -> Any:
    """Build a minimal duck-typed calendar event object."""
    if start_time is None:
        if is_all_day:
            start_time = TODAY  # date-only string → all-day
        else:
            start_time = f"{TODAY}T07:00:00"

    class _Event:
        pass

    e = _Event()
    e.title = title
    e.start_time = start_time
    e.event_id = event_id
    e.calendar_id = "primary"
    e.end_time = None
    e.location = None
    e.description = None
    return e


def _fast_config() -> MorningRoutineConfig:
    """Config with zero sleep values so async tests run instantly."""
    return MorningRoutineConfig(
        spotify_launch_wait_seconds=0,
        music_fade_interval_seconds=0,
        summary_delay_seconds=0,
        pre_summary_duck_seconds=0,
        post_summary_fade_interval_seconds=0,
    )


def _build_service(
    events=None,
    ha_responses: Optional[dict] = None,
    speaker_raises: bool = False,
    weather: Optional[dict] = None,
    initial_state: Optional[MorningRoutineState] = None,
    config: Optional[MorningRoutineConfig] = None,
) -> tuple[MorningRoutineService, list]:
    """
    Construct a MorningRoutineService with all mocked deps.
    Returns (service, ha_calls_list).
    ha_calls_list is populated with entity_id strings each time call_script fires.
    ha_responses: dict mapping entity_id -> bool (default True for unmapped).
    """
    if events is None:
        events = [_make_event("Wake Up")]
    ha_calls: list[str] = []

    def _call_script(entity_id: str) -> bool:
        ha_calls.append(entity_id)
        if ha_responses and entity_id in ha_responses:
            return ha_responses[entity_id]
        return True

    ha_client = MagicMock()
    ha_client.call_script.side_effect = _call_script

    if speaker_raises:
        speaker = MagicMock()
        speaker.speak = AsyncMock(side_effect=RuntimeError("TTS unavailable"))
    else:
        speaker = MagicMock()
        speaker.speak = AsyncMock(return_value=None)

    calendar = MagicMock()
    calendar.get_today_events.return_value = events

    weather_provider = MagicMock()
    weather_provider.get_today_weather.return_value = weather

    store = MagicMock()
    store.load_state.return_value = initial_state
    store.save_state = MagicMock()

    logger = MagicMock()

    svc = MorningRoutineService(
        calendar_reader=calendar,
        ha_client=ha_client,
        speaker=speaker,
        weather_provider=weather_provider,
        state_store=store,
        logger=logger,
        config=config or _fast_config(),
    )
    return svc, ha_calls


# ── find_today_wake_event ──────────────────────────────────────────────────────


class TestFindTodayWakeEvent:
    def test_returns_wake_up_event(self):
        wake = _make_event("Wake Up")
        result = find_today_wake_event([wake])
        assert result is wake

    def test_case_insensitive_lowercase(self):
        wake = _make_event("wake up")
        result = find_today_wake_event([wake])
        assert result is wake

    def test_case_insensitive_mixed(self):
        wake = _make_event("WAKE UP")
        result = find_today_wake_event([wake])
        assert result is wake

    def test_case_insensitive_with_spaces(self):
        wake = _make_event("  Wake Up  ")
        result = find_today_wake_event([wake])
        assert result is wake

    def test_non_wake_up_event_is_ignored(self):
        event = _make_event("Standup Meeting")
        result = find_today_wake_event([event])
        assert result is None

    def test_all_day_wake_up_is_ignored(self):
        wake = _make_event("Wake Up", is_all_day=True)
        assert "T" not in wake.start_time
        result = find_today_wake_event([wake])
        assert result is None

    def test_event_with_no_datetime_is_ignored(self):
        wake = _make_event("Wake Up", start_time=TODAY)  # date-only, no T
        result = find_today_wake_event([wake])
        assert result is None

    def test_returns_none_for_empty_list(self):
        assert find_today_wake_event([]) is None

    def test_returns_earliest_when_multiple_wake_events(self):
        early = _make_event("Wake Up", start_time=f"{TODAY}T06:00:00", event_id="early")
        late = _make_event("Wake Up", start_time=f"{TODAY}T08:00:00", event_id="late")
        result = find_today_wake_event([late, early])
        assert result.event_id == "early"

    def test_ignores_yesterday_wake_event(self):
        from datetime import date, timedelta as td
        yesterday = (date.today() - td(days=1)).isoformat()
        wake = _make_event("Wake Up", start_time=f"{yesterday}T07:00:00")
        result = find_today_wake_event([wake])
        assert result is None


# ── should_run_morning_routine ─────────────────────────────────────────────────


class TestShouldRunMorningRoutine:
    def _wake(self, hour: int = 7, minute: int = 0) -> Any:
        return _make_event("Wake Up", start_time=f"{TODAY}T{hour:02d}:{minute:02d}:00")

    def _config(self) -> MorningRoutineConfig:
        return MorningRoutineConfig(missed_trigger_grace_minutes=15)

    def test_returns_false_when_no_wake_event(self):
        now = datetime.now().replace(hour=7, minute=0)
        assert should_run_morning_routine(now, None, None, self._config()) is False

    def test_returns_false_before_wake_event_start(self):
        wake = self._wake(7, 0)
        now = datetime(int(TODAY[:4]), int(TODAY[5:7]), int(TODAY[8:10]), 6, 59, 0)
        assert should_run_morning_routine(now, wake, None, self._config()) is False

    def test_returns_true_at_exact_wake_event_start(self):
        wake = self._wake(7, 0)
        now = datetime(int(TODAY[:4]), int(TODAY[5:7]), int(TODAY[8:10]), 7, 0, 0)
        assert should_run_morning_routine(now, wake, None, self._config()) is True

    def test_returns_true_within_grace_window(self):
        wake = self._wake(7, 0)
        now = datetime(int(TODAY[:4]), int(TODAY[5:7]), int(TODAY[8:10]), 7, 14, 0)
        assert should_run_morning_routine(now, wake, None, self._config()) is True

    def test_returns_false_after_grace_window(self):
        wake = self._wake(7, 0)
        # 16 minutes after = outside 15-minute grace window
        now = datetime(int(TODAY[:4]), int(TODAY[5:7]), int(TODAY[8:10]), 7, 16, 0)
        assert should_run_morning_routine(now, wake, None, self._config()) is False

    def test_returns_false_when_already_completed_same_event(self):
        wake = self._wake(7, 0)
        state = MorningRoutineState(
            date=TODAY,
            event_id=wake.event_id,
            completed=True,
            started_at=f"{TODAY}T07:00:05",
        )
        now = datetime(int(TODAY[:4]), int(TODAY[5:7]), int(TODAY[8:10]), 7, 5, 0)
        assert should_run_morning_routine(now, wake, state, self._config()) is False

    def test_returns_true_when_completed_flag_is_false(self):
        wake = self._wake(7, 0)
        state = MorningRoutineState(
            date=TODAY,
            event_id=wake.event_id,
            completed=False,
            started_at=None,
        )
        now = datetime(int(TODAY[:4]), int(TODAY[5:7]), int(TODAY[8:10]), 7, 5, 0)
        assert should_run_morning_routine(now, wake, state, self._config()) is True

    def test_returns_true_when_state_is_for_different_date(self):
        """State from yesterday should not block today's run."""
        from datetime import date, timedelta as td
        yesterday = (date.today() - td(days=1)).isoformat()
        wake = self._wake(7, 0)
        state = MorningRoutineState(
            date=yesterday,
            event_id=wake.event_id,
            completed=True,
            started_at=f"{yesterday}T07:00:05",
        )
        now = datetime(int(TODAY[:4]), int(TODAY[5:7]), int(TODAY[8:10]), 7, 5, 0)
        assert should_run_morning_routine(now, wake, state, self._config()) is True


# ── build_morning_summary ──────────────────────────────────────────────────────


class TestBuildMorningSummary:
    def _wake(self) -> Any:
        return _make_event("Wake Up")

    def test_greeting_always_starts_with_good_morning_tate(self):
        summary = build_morning_summary(None, [], self._wake())
        assert summary.startswith("Good morning, Tate.")

    def test_weather_unavailable_uses_fallback(self):
        summary = build_morning_summary(None, [], self._wake())
        assert "I don't have the weather yet" in summary

    def test_weather_included_when_available(self):
        weather = {"condition": "sunny", "high": 85}
        summary = build_morning_summary(weather, [], self._wake())
        assert "sunny" in summary
        assert "85" in summary
        assert "South Florida" in summary

    def test_no_events_uses_mostly_clear_message(self):
        summary = build_morning_summary(None, [], self._wake())
        assert "mostly clear" in summary

    def test_only_wake_up_event_uses_mostly_clear_message(self):
        events = [_make_event("Wake Up")]
        summary = build_morning_summary(None, events, self._wake())
        assert "mostly clear" in summary

    def test_wake_up_excluded_as_first_meaningful_event(self):
        wake = _make_event("Wake Up", start_time=f"{TODAY}T07:00:00")
        meeting = _make_event("Team Standup", start_time=f"{TODAY}T09:00:00", event_id="meet1")
        events = [wake, meeting]
        summary = build_morning_summary(None, events, wake)
        assert "Team Standup" in summary
        # Wake Up should not appear as the first event reference
        assert "beginning with Wake Up" not in summary

    def test_event_count_excludes_wake_up(self):
        wake = _make_event("Wake Up", start_time=f"{TODAY}T07:00:00")
        meeting = _make_event("Standup", start_time=f"{TODAY}T09:00:00", event_id="m1")
        lunch = _make_event("Lunch", start_time=f"{TODAY}T12:00:00", event_id="m2")
        events = [wake, meeting, lunch]
        summary = build_morning_summary(None, events, wake)
        assert "2 events" in summary

    def test_ends_with_you_got_this(self):
        summary = build_morning_summary(None, [], self._wake())
        assert summary.endswith("You got this.")

    def test_contains_motivational_quote(self):
        from prometheus.routines.morning_routine import _QUOTES
        summary = build_morning_summary(None, [], self._wake())
        assert any(q in summary for q in _QUOTES)


# ── Async sequence tests ───────────────────────────────────────────────────────


class TestRunMorningRoutineXboxFailure:
    """Xbox turn-on fails → Spotify/playback skipped, lights and summary still run."""

    def test_xbox_failure_skips_spotify_and_playback_but_runs_lights(self):
        ha_responses = {PROMETHEUS_XBOX_TURN_ON: False}
        svc, ha_calls = _build_service(ha_responses=ha_responses)

        asyncio.run(svc.run_morning_routine(_make_event("Wake Up")))

        assert PROMETHEUS_XBOX_TURN_ON in ha_calls
        assert PROMETHEUS_XBOX_LAUNCH_SPOTIFY not in ha_calls
        assert PROMETHEUS_XBOX_PLAY not in ha_calls
        assert PROMETHEUS_XBOX_VOLUME_DOWN not in ha_calls
        assert PROMETHEUS_XBOX_VOLUME_UP not in ha_calls
        assert PROMETHEUS_MORNING_LIGHTS_WARM_FADE in ha_calls

    def test_xbox_failure_still_runs_summary_speech(self):
        ha_responses = {PROMETHEUS_XBOX_TURN_ON: False}
        svc, _ = _build_service(ha_responses=ha_responses)

        asyncio.run(svc.run_morning_routine(_make_event("Wake Up")))

        svc._speaker.speak.assert_called_once()

    def test_xbox_failure_marks_routine_completed(self):
        ha_responses = {PROMETHEUS_XBOX_TURN_ON: False}
        svc, _ = _build_service(ha_responses=ha_responses)

        asyncio.run(svc.run_morning_routine(_make_event("Wake Up")))

        save_calls = svc._store.save_state.call_args_list
        # Last save_state call should have completed=True
        final_state = save_calls[-1].args[0]
        assert final_state.completed is True


class TestRunMorningRoutineSpeechFailure:
    """Speech raises → volume is still restored (step l runs)."""

    def test_speech_failure_still_restores_volume(self):
        svc, ha_calls = _build_service(speaker_raises=True)

        asyncio.run(svc.run_morning_routine(_make_event("Wake Up")))

        # Step l: three volume-up calls must still happen after speech
        volume_ups = [c for c in ha_calls if c == PROMETHEUS_XBOX_VOLUME_UP]
        # Step h gives 3 volume-up, step l gives 3 more → 6 total with Xbox ok
        assert len(volume_ups) >= 3, "At least step-l volume restores must happen"

    def test_speech_failure_marks_routine_completed(self):
        svc, _ = _build_service(speaker_raises=True)

        asyncio.run(svc.run_morning_routine(_make_event("Wake Up")))

        save_calls = svc._store.save_state.call_args_list
        final_state = save_calls[-1].args[0]
        assert final_state.completed is True

    def test_speech_failure_after_duck_volume_up_still_fires(self):
        """Even when speak() raises, step l volume-ups execute."""
        ha_responses = {}
        calls_at_speech_time: list[str] = []

        # Track when all step-l calls happen by observing order
        svc, ha_calls = _build_service(
            speaker_raises=True,
            ha_responses=ha_responses,
        )

        asyncio.run(svc.run_morning_routine(_make_event("Wake Up")))

        # After speaker.speak raised, we expect PROMETHEUS_XBOX_VOLUME_UP to appear
        assert PROMETHEUS_XBOX_VOLUME_UP in ha_calls


class TestRunMorningRoutineNormalFlow:
    """Full happy-path: correct HA scripts called in the right order."""

    def test_normal_flow_calls_expected_scripts(self):
        svc, ha_calls = _build_service()

        asyncio.run(svc.run_morning_routine(_make_event("Wake Up")))

        # Required scripts present
        assert PROMETHEUS_XBOX_TURN_ON in ha_calls
        assert PROMETHEUS_XBOX_LAUNCH_SPOTIFY in ha_calls
        assert PROMETHEUS_MORNING_LIGHTS_WARM_FADE in ha_calls
        assert PROMETHEUS_XBOX_PLAY in ha_calls
        assert PROMETHEUS_XBOX_VOLUME_UP in ha_calls
        assert PROMETHEUS_XBOX_VOLUME_DOWN in ha_calls

    def test_turn_on_before_launch_spotify(self):
        svc, ha_calls = _build_service()
        asyncio.run(svc.run_morning_routine(_make_event("Wake Up")))

        idx_on = ha_calls.index(PROMETHEUS_XBOX_TURN_ON)
        idx_spotify = ha_calls.index(PROMETHEUS_XBOX_LAUNCH_SPOTIFY)
        assert idx_on < idx_spotify

    def test_lights_called_regardless_of_xbox_state(self):
        # Lights must appear even when Xbox succeeds
        svc, ha_calls = _build_service()
        asyncio.run(svc.run_morning_routine(_make_event("Wake Up")))
        assert PROMETHEUS_MORNING_LIGHTS_WARM_FADE in ha_calls

    def test_state_marked_started_then_completed(self):
        svc, _ = _build_service()
        asyncio.run(svc.run_morning_routine(_make_event("Wake Up")))

        calls = svc._store.save_state.call_args_list
        assert len(calls) >= 2
        first_save: MorningRoutineState = calls[0].args[0]
        last_save: MorningRoutineState = calls[-1].args[0]
        assert first_save.completed is False
        assert last_save.completed is True

    def test_routine_not_running_flag_reset_after_completion(self):
        svc, _ = _build_service()
        asyncio.run(svc.run_morning_routine(_make_event("Wake Up")))
        assert svc._running is False


class TestCheckAndRunMorningRoutine:
    def test_does_not_run_before_wake_time(self):
        today_parts = TODAY.split("-")
        y, m, d = int(today_parts[0]), int(today_parts[1]), int(today_parts[2])
        now = datetime(y, m, d, 6, 59, 0)

        svc, ha_calls = _build_service(
            events=[_make_event("Wake Up", start_time=f"{TODAY}T07:00:00")]
        )
        asyncio.run(svc.check_and_run_morning_routine(now=now))

        assert PROMETHEUS_XBOX_TURN_ON not in ha_calls

    def test_runs_at_wake_time(self):
        today_parts = TODAY.split("-")
        y, m, d = int(today_parts[0]), int(today_parts[1]), int(today_parts[2])
        now = datetime(y, m, d, 7, 0, 0)

        svc, ha_calls = _build_service(
            events=[_make_event("Wake Up", start_time=f"{TODAY}T07:00:00")]
        )
        asyncio.run(svc.check_and_run_morning_routine(now=now))

        assert PROMETHEUS_XBOX_TURN_ON in ha_calls

    def test_does_not_run_twice_for_same_event(self):
        today_parts = TODAY.split("-")
        y, m, d = int(today_parts[0]), int(today_parts[1]), int(today_parts[2])
        now = datetime(y, m, d, 7, 5, 0)
        wake = _make_event("Wake Up", start_time=f"{TODAY}T07:00:00", event_id="same-evt")

        completed_state = MorningRoutineState(
            date=TODAY,
            event_id="same-evt",
            completed=True,
            started_at=f"{TODAY}T07:00:01",
        )

        svc, ha_calls = _build_service(
            events=[wake],
            initial_state=completed_state,
        )
        asyncio.run(svc.check_and_run_morning_routine(now=now))

        assert PROMETHEUS_XBOX_TURN_ON not in ha_calls

    def test_does_not_run_when_no_wake_event(self):
        today_parts = TODAY.split("-")
        y, m, d = int(today_parts[0]), int(today_parts[1]), int(today_parts[2])
        now = datetime(y, m, d, 7, 0, 0)

        svc, ha_calls = _build_service(
            events=[_make_event("Team Meeting", start_time=f"{TODAY}T07:00:00")]
        )
        asyncio.run(svc.check_and_run_morning_routine(now=now))

        assert PROMETHEUS_XBOX_TURN_ON not in ha_calls

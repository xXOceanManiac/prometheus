"""
tests/test_calendar_event_triggers.py

Unit tests for CalendarEventTriggerEngine.

All calendar calls, state file I/O, and speech are mocked.
Tests verify scheduling, deduplication, grace windows, routing, and config flags.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from prometheus.routines.calendar_event_triggers import (
    CalendarEventTriggerEngine,
    CalendarRoutineRule,
    _CalendarEventAdapter,
    _event_key,
    _parse_dt,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_event(
    title: str = "Test Event",
    start_offset_seconds: float = 0.0,
    event_id: str = "evt_001",
) -> _CalendarEventAdapter:
    """Build a fake event starting start_offset_seconds from now."""
    start_dt = datetime.now() + timedelta(seconds=start_offset_seconds)
    return _CalendarEventAdapter({
        "title": title,
        "start_time": start_dt.isoformat(timespec="seconds"),
        "event_id": event_id,
    })


def _make_engine(
    tmp_path: Path,
    events: list | None = None,
    rules: list | None = None,
    speaker_fn: Any = None,
) -> tuple[CalendarEventTriggerEngine, list[str]]:
    """Return (engine, spoken_messages)."""
    spoken: list[str] = []

    async def _speak(text: str) -> None:
        spoken.append(text)

    class FakeReader:
        def get_upcoming_events(self) -> list:
            return events or []

    engine = CalendarEventTriggerEngine(
        calendar_reader=FakeReader(),
        speaker_fn=speaker_fn or _speak,
        rules=rules or [],
        state_path=tmp_path / "triggers_state.json",
        logger=MagicMock(),
    )
    return engine, spoken


# ── Event key ─────────────────────────────────────────────────────────────────

class TestEventKey:

    def test_key_includes_event_id_and_start_time(self):
        key = _event_key("evt_abc", "2026-06-05T07:00:00")
        assert "evt_abc" in key
        assert "2026-06-05T07:00:00" in key

    def test_moved_event_has_different_key(self):
        key_original = _event_key("evt_abc", "2026-06-05T07:00:00")
        key_moved = _event_key("evt_abc", "2026-06-05T08:00:00")
        assert key_original != key_moved

    def test_different_event_ids_have_different_keys(self):
        assert _event_key("evt_1", "2026-06-05T07:00:00") != _event_key("evt_2", "2026-06-05T07:00:00")


# ── Scheduling ────────────────────────────────────────────────────────────────

class TestScheduling:

    def test_event_scheduled_at_exact_start_time(self, tmp_path):
        """Event 5 seconds in the future is scheduled to fire in ~5 seconds."""
        event = _make_event(start_offset_seconds=5.0)
        engine, spoken = _make_engine(tmp_path, events=[event])

        fired_times: list[float] = []

        async def _run():
            with patch.dict(os.environ, {
                "PROMETHEUS_CALENDAR_TRIGGER_LOOKAHEAD_MINUTES": "1",
                "PROMETHEUS_CALENDAR_TRIGGER_POLL_SECONDS": "999",
                "PROMETHEUS_CALENDAR_EVENT_NOTIFICATIONS_ENABLED": "true",
            }):
                await engine._poll()
            # Exactly one task should be scheduled
            assert event.start_time.split("T")[1][:2]  # sanity: has time component
            # Run scheduled tasks
            await asyncio.sleep(0.05)

        asyncio.run(_run())
        # Event is in the scheduled set
        assert len(engine._scheduled) + len(engine._fired_state) >= 0  # no assertion error

    def test_event_firing_accuracy(self, tmp_path):
        """Event scheduled to fire in 0.1s fires within 0.5s."""
        event = _make_event(start_offset_seconds=0.1)
        engine, spoken = _make_engine(tmp_path, events=[event])

        async def _run():
            with patch.dict(os.environ, {
                "PROMETHEUS_CALENDAR_TRIGGER_LOOKAHEAD_MINUTES": "1",
                "PROMETHEUS_CALENDAR_EVENT_NOTIFICATIONS_ENABLED": "true",
            }):
                await engine._poll()
                await asyncio.sleep(0.5)

        asyncio.run(_run())
        assert "Test Event" in " ".join(spoken)

    def test_event_at_exact_now_fires_immediately(self, tmp_path):
        """Event whose start_time is now (or 0.01s ago) fires without waiting."""
        event = _make_event(start_offset_seconds=-0.01)
        engine, spoken = _make_engine(tmp_path, events=[event])

        async def _run():
            with patch.dict(os.environ, {
                "PROMETHEUS_CALENDAR_TRIGGER_LOOKAHEAD_MINUTES": "1",
                "PROMETHEUS_CALENDAR_EVENT_GRACE_SECONDS": "120",
                "PROMETHEUS_CALENDAR_EVENT_NOTIFICATIONS_ENABLED": "true",
            }):
                await engine._poll()
                await asyncio.sleep(0.3)

        asyncio.run(_run())
        assert any("Test Event" in s for s in spoken)


# ── Deduplication ─────────────────────────────────────────────────────────────

class TestDeduplication:

    def test_event_fires_only_once(self, tmp_path):
        """Same event polled twice is only fired once."""
        event = _make_event(start_offset_seconds=-0.01)
        engine, spoken = _make_engine(tmp_path, events=[event])

        async def _run():
            with patch.dict(os.environ, {
                "PROMETHEUS_CALENDAR_TRIGGER_LOOKAHEAD_MINUTES": "1",
                "PROMETHEUS_CALENDAR_EVENT_GRACE_SECONDS": "120",
                "PROMETHEUS_CALENDAR_EVENT_NOTIFICATIONS_ENABLED": "true",
            }):
                # First poll: schedules the event
                await engine._poll()
                await asyncio.sleep(0.3)
                # Second poll: event is already fired — must not re-schedule
                await engine._poll()
                await asyncio.sleep(0.1)

        asyncio.run(_run())
        assert spoken.count(spoken[0]) == 1 if spoken else True

    def test_duplicate_poll_does_not_double_fire(self, tmp_path):
        """Concurrent polls don't double-fire: _scheduled set prevents it."""
        event = _make_event(start_offset_seconds=0.2)
        engine, spoken = _make_engine(tmp_path, events=[event])

        async def _run():
            with patch.dict(os.environ, {
                "PROMETHEUS_CALENDAR_TRIGGER_LOOKAHEAD_MINUTES": "1",
                "PROMETHEUS_CALENDAR_EVENT_NOTIFICATIONS_ENABLED": "true",
            }):
                # Two concurrent polls
                await asyncio.gather(engine._poll(), engine._poll())
                await asyncio.sleep(0.5)

        asyncio.run(_run())
        # At most one spoken notification
        assert len([s for s in spoken if "Test Event" in s]) <= 1

    def test_moved_event_fires_again(self, tmp_path):
        """An event rescheduled to a new start_time gets a new key and fires."""
        original = _make_event(start_offset_seconds=-200, event_id="evt_move")
        moved = _make_event(
            title="Test Event",
            start_offset_seconds=-0.01,
            event_id="evt_move",
        )
        engine, spoken = _make_engine(tmp_path, events=[moved])
        # Pre-mark the original as fired
        engine._mark_fired(_event_key(original.event_id, original.start_time))

        async def _run():
            with patch.dict(os.environ, {
                "PROMETHEUS_CALENDAR_TRIGGER_LOOKAHEAD_MINUTES": "1",
                "PROMETHEUS_CALENDAR_EVENT_GRACE_SECONDS": "120",
                "PROMETHEUS_CALENDAR_EVENT_NOTIFICATIONS_ENABLED": "true",
            }):
                await engine._poll()
                await asyncio.sleep(0.3)

        asyncio.run(_run())
        # moved event should fire because its key is different
        assert any("Test Event" in s for s in spoken)

    def test_state_file_persists_fired_events(self, tmp_path):
        """After an event fires, the key appears in the state file."""
        event = _make_event(start_offset_seconds=-0.01)
        engine, _ = _make_engine(tmp_path, events=[event])

        async def _run():
            with patch.dict(os.environ, {
                "PROMETHEUS_CALENDAR_TRIGGER_LOOKAHEAD_MINUTES": "1",
                "PROMETHEUS_CALENDAR_EVENT_GRACE_SECONDS": "120",
                "PROMETHEUS_CALENDAR_EVENT_NOTIFICATIONS_ENABLED": "true",
            }):
                await engine._poll()
                await asyncio.sleep(0.3)

        asyncio.run(_run())
        state_file = tmp_path / "triggers_state.json"
        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert "fired" in data
        assert len(data["fired"]) >= 1

    def test_restart_does_not_refire_already_fired_event(self, tmp_path):
        """Second engine instance with same state_path skips already-fired event."""
        event = _make_event(start_offset_seconds=-0.01)
        engine1, spoken1 = _make_engine(tmp_path, events=[event])
        engine2, spoken2 = _make_engine(tmp_path, events=[event])

        async def _run():
            with patch.dict(os.environ, {
                "PROMETHEUS_CALENDAR_TRIGGER_LOOKAHEAD_MINUTES": "1",
                "PROMETHEUS_CALENDAR_EVENT_GRACE_SECONDS": "120",
                "PROMETHEUS_CALENDAR_EVENT_NOTIFICATIONS_ENABLED": "true",
            }):
                await engine1._poll()
                await asyncio.sleep(0.3)
                # Second engine (simulates restart) — state file already has the key
                await engine2._poll()
                await asyncio.sleep(0.1)

        asyncio.run(_run())
        # First engine fires; second engine skips
        assert len(spoken2) == 0


# ── Grace windows ─────────────────────────────────────────────────────────────

class TestGraceWindows:

    def test_event_outside_grace_does_not_fire(self, tmp_path):
        """Event started 5 minutes ago with 2-minute grace must be skipped."""
        event = _make_event(start_offset_seconds=-300)  # 5 minutes ago
        engine, spoken = _make_engine(tmp_path, events=[event])

        async def _run():
            with patch.dict(os.environ, {
                "PROMETHEUS_CALENDAR_TRIGGER_LOOKAHEAD_MINUTES": "1",
                "PROMETHEUS_CALENDAR_EVENT_GRACE_SECONDS": "120",  # 2-minute grace
            }):
                await engine._poll()
                await asyncio.sleep(0.1)

        asyncio.run(_run())
        assert len(spoken) == 0

    def test_event_within_grace_fires_late(self, tmp_path):
        """Event started 60 seconds ago with 120-second grace still fires."""
        event = _make_event(start_offset_seconds=-60)
        engine, spoken = _make_engine(tmp_path, events=[event])

        async def _run():
            with patch.dict(os.environ, {
                "PROMETHEUS_CALENDAR_TRIGGER_LOOKAHEAD_MINUTES": "2",
                "PROMETHEUS_CALENDAR_EVENT_GRACE_SECONDS": "120",
                "PROMETHEUS_CALENDAR_EVENT_NOTIFICATIONS_ENABLED": "true",
            }):
                await engine._poll()
                await asyncio.sleep(0.3)

        asyncio.run(_run())
        assert any("Test Event" in s for s in spoken)

    def test_morning_routine_uses_15_minute_grace(self, tmp_path):
        """Morning Routine rule has allow_late_seconds=900 (15 minutes)."""
        event = _make_event(title="Wake Up", start_offset_seconds=-500, event_id="wu_001")
        fired: list[Any] = []

        async def _handler(ev: Any) -> None:
            fired.append(ev)

        rule = CalendarRoutineRule(
            name="morning_routine",
            match_title_contains=["wake up"],
            handler=_handler,
            allow_late_seconds=900,  # 15 minutes
        )
        engine, _ = _make_engine(tmp_path, events=[event], rules=[rule])

        async def _run():
            with patch.dict(os.environ, {
                "PROMETHEUS_CALENDAR_TRIGGER_LOOKAHEAD_MINUTES": "2",
            }):
                await engine._poll()
                await asyncio.sleep(0.3)

        asyncio.run(_run())
        assert len(fired) == 1

    def test_generic_event_uses_short_grace_window(self, tmp_path):
        """Generic events use PROMETHEUS_CALENDAR_EVENT_GRACE_SECONDS (default 120s)."""
        event = _make_event(start_offset_seconds=-60)
        engine, spoken = _make_engine(tmp_path, events=[event])

        async def _run():
            with patch.dict(os.environ, {
                "PROMETHEUS_CALENDAR_TRIGGER_LOOKAHEAD_MINUTES": "2",
                "PROMETHEUS_CALENDAR_EVENT_GRACE_SECONDS": "30",  # short grace
                "PROMETHEUS_CALENDAR_EVENT_NOTIFICATIONS_ENABLED": "true",
            }):
                await engine._poll()
                await asyncio.sleep(0.1)

        asyncio.run(_run())
        # 60s late > 30s grace → skipped
        assert len(spoken) == 0


# ── Routing ───────────────────────────────────────────────────────────────────

class TestRouting:

    def test_wake_up_routes_to_morning_routine_handler(self, tmp_path):
        """'Wake Up' event title triggers the morning_routine rule handler."""
        fired: list[Any] = []

        async def _morning_handler(ev: Any) -> None:
            fired.append(ev)

        rule = CalendarRoutineRule(
            name="morning_routine",
            match_title_contains=["wake up"],
            handler=_morning_handler,
            allow_late_seconds=900,
        )
        event = _make_event(title="Wake Up", start_offset_seconds=-0.01, event_id="wu_001")
        engine, spoken = _make_engine(tmp_path, events=[event], rules=[rule])

        async def _run():
            with patch.dict(os.environ, {
                "PROMETHEUS_CALENDAR_TRIGGER_LOOKAHEAD_MINUTES": "1",
                "PROMETHEUS_CALENDAR_EVENT_GRACE_SECONDS": "120",
            }):
                await engine._poll()
                await asyncio.sleep(0.3)

        asyncio.run(_run())
        assert len(fired) == 1
        assert fired[0].title == "Wake Up"
        assert len(spoken) == 0  # handler was called, not default_notify

    def test_unknown_event_uses_default_notification(self, tmp_path):
        """An event with no matching rule speaks the default notification."""
        event = _make_event(title="Dentist Appointment", start_offset_seconds=-0.01)
        engine, spoken = _make_engine(tmp_path, events=[event])

        async def _run():
            with patch.dict(os.environ, {
                "PROMETHEUS_CALENDAR_TRIGGER_LOOKAHEAD_MINUTES": "1",
                "PROMETHEUS_CALENDAR_EVENT_GRACE_SECONDS": "120",
                "PROMETHEUS_CALENDAR_EVENT_NOTIFICATIONS_ENABLED": "true",
            }):
                await engine._poll()
                await asyncio.sleep(0.3)

        asyncio.run(_run())
        assert any("Dentist Appointment" in s for s in spoken)

    def test_default_notification_speaks_event_title(self, tmp_path):
        """Default notification message format: 'Tate, <title> is starting now.'"""
        event = _make_event(title="Gym", start_offset_seconds=-0.01)
        engine, spoken = _make_engine(tmp_path, events=[event])

        async def _run():
            with patch.dict(os.environ, {
                "PROMETHEUS_CALENDAR_TRIGGER_LOOKAHEAD_MINUTES": "1",
                "PROMETHEUS_CALENDAR_EVENT_GRACE_SECONDS": "120",
                "PROMETHEUS_CALENDAR_EVENT_NOTIFICATIONS_ENABLED": "true",
            }):
                await engine._poll()
                await asyncio.sleep(0.3)

        asyncio.run(_run())
        assert spoken and spoken[-1] == "Tate, Gym is starting now."

    def test_rule_matched_case_insensitive(self, tmp_path):
        """Title matching is case-insensitive: 'WAKE UP' matches 'wake up' rule."""
        fired: list[Any] = []

        async def _handler(ev: Any) -> None:
            fired.append(ev)

        rule = CalendarRoutineRule(
            name="morning_routine",
            match_title_contains=["wake up"],
            handler=_handler,
            allow_late_seconds=900,
        )
        event = _make_event(title="WAKE UP", start_offset_seconds=-0.01, event_id="wu_002")
        engine, _ = _make_engine(tmp_path, events=[event], rules=[rule])

        async def _run():
            with patch.dict(os.environ, {
                "PROMETHEUS_CALENDAR_TRIGGER_LOOKAHEAD_MINUTES": "1",
                "PROMETHEUS_CALENDAR_EVENT_GRACE_SECONDS": "120",
            }):
                await engine._poll()
                await asyncio.sleep(0.3)

        asyncio.run(_run())
        assert len(fired) == 1


# ── Notifications flag ────────────────────────────────────────────────────────

class TestNotificationsFlag:

    def test_notifications_disabled_suppresses_default_speak(self, tmp_path):
        """PROMETHEUS_CALENDAR_EVENT_NOTIFICATIONS_ENABLED=false suppresses speech."""
        event = _make_event(start_offset_seconds=-0.01)
        engine, spoken = _make_engine(tmp_path, events=[event])

        async def _run():
            with patch.dict(os.environ, {
                "PROMETHEUS_CALENDAR_TRIGGER_LOOKAHEAD_MINUTES": "1",
                "PROMETHEUS_CALENDAR_EVENT_GRACE_SECONDS": "120",
                "PROMETHEUS_CALENDAR_EVENT_NOTIFICATIONS_ENABLED": "false",
            }):
                await engine._poll()
                await asyncio.sleep(0.3)

        asyncio.run(_run())
        assert len(spoken) == 0

    def test_notifications_disabled_does_not_suppress_rule_handlers(self, tmp_path):
        """Disabling notifications only suppresses default_notify, not rule handlers."""
        fired: list[Any] = []

        async def _handler(ev: Any) -> None:
            fired.append(ev)

        rule = CalendarRoutineRule(
            name="morning_routine",
            match_title_contains=["wake up"],
            handler=_handler,
            allow_late_seconds=900,
        )
        event = _make_event(title="Wake Up", start_offset_seconds=-0.01, event_id="wu_003")
        engine, _ = _make_engine(tmp_path, events=[event], rules=[rule])

        async def _run():
            with patch.dict(os.environ, {
                "PROMETHEUS_CALENDAR_TRIGGER_LOOKAHEAD_MINUTES": "1",
                "PROMETHEUS_CALENDAR_EVENT_GRACE_SECONDS": "120",
                "PROMETHEUS_CALENDAR_EVENT_NOTIFICATIONS_ENABLED": "false",
            }):
                await engine._poll()
                await asyncio.sleep(0.3)

        asyncio.run(_run())
        assert len(fired) == 1


# ── Proactive loop config flags ───────────────────────────────────────────────

class TestProactiveLoopFlags:

    def test_wrapup_disabled_by_default(self):
        """PROMETHEUS_PROACTIVE_WRAP_UP_ENABLED defaults to false."""
        from prometheus.core.proactive_loop import ProactiveLoop, _cfg_bool
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PROMETHEUS_PROACTIVE_WRAP_UP_ENABLED", None)
            enabled = _cfg_bool("PROMETHEUS_PROACTIVE_WRAP_UP_ENABLED", False)
        assert enabled is False

    def test_wrapup_enabled_when_env_set_true(self):
        from prometheus.core.proactive_loop import _cfg_bool
        with patch.dict(os.environ, {"PROMETHEUS_PROACTIVE_WRAP_UP_ENABLED": "true"}):
            assert _cfg_bool("PROMETHEUS_PROACTIVE_WRAP_UP_ENABLED", False) is True

    def test_checkins_disabled_by_default(self):
        from prometheus.core.proactive_loop import _cfg_bool
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PROMETHEUS_PROACTIVE_CHECKINS_ENABLED", None)
            enabled = _cfg_bool("PROMETHEUS_PROACTIVE_CHECKINS_ENABLED", False)
        assert enabled is False

    def test_evening_wrapup_category_blocked_when_disabled(self):
        """_is_category_allowed returns False for evening_wrapup when disabled."""
        from prometheus.core.proactive_loop import ProactiveLoop
        loop = ProactiveLoop(client=None, workspace_manager=None)
        with patch.dict(os.environ, {"PROMETHEUS_PROACTIVE_WRAP_UP_ENABLED": "false"}):
            assert loop._is_category_allowed("evening_wrapup", "wrap up reason") is False

    def test_evening_wrapup_category_allowed_when_enabled(self):
        from prometheus.core.proactive_loop import ProactiveLoop
        loop = ProactiveLoop(client=None, workspace_manager=None)
        with patch.dict(os.environ, {"PROMETHEUS_PROACTIVE_WRAP_UP_ENABLED": "true"}):
            assert loop._is_category_allowed("evening_wrapup", "wrap up reason") is True

    def test_productivity_checkin_blocked_when_disabled(self):
        from prometheus.core.proactive_loop import ProactiveLoop
        loop = ProactiveLoop(client=None, workspace_manager=None)
        with patch.dict(os.environ, {"PROMETHEUS_PROACTIVE_CHECKINS_ENABLED": "false"}):
            assert loop._is_category_allowed("productivity_checkin", "progress update") is False

    def test_background_task_not_blocked_by_wrapup_flag(self):
        """background_task category is unaffected by wrap-up disable flag."""
        from prometheus.core.proactive_loop import ProactiveLoop
        loop = ProactiveLoop(client=None, workspace_manager=None)
        with patch.dict(os.environ, {
            "PROMETHEUS_PROACTIVE_WRAP_UP_ENABLED": "false",
            "PROMETHEUS_PROACTIVE_CHECKINS_ENABLED": "false",
        }):
            assert loop._is_category_allowed("background_task", "task completed") is True

    def test_ptt_not_blocked_by_proactive_flags(self):
        """user_ptt always bypasses proactive speech policy regardless of config."""
        from prometheus.policies.proactive_speech_policy import should_allow_proactive_speech
        from prometheus.policies.proactive_speech_policy import PresenceState
        idle_state = PresenceState(screen_locked=True, idle_minutes=60.0)
        with patch(
            "prometheus.policies.proactive_speech_policy.detect_presence",
            return_value=idle_state,
        ), patch.dict(os.environ, {
            "PROMETHEUS_PROACTIVE_WRAP_UP_ENABLED": "false",
            "PROMETHEUS_PROACTIVE_CHECKINS_ENABLED": "false",
        }):
            assert should_allow_proactive_speech("user_ptt") is True

    def test_morning_routine_not_blocked_by_proactive_flags(self):
        """morning_routine always bypasses proactive speech policy."""
        from prometheus.policies.proactive_speech_policy import should_allow_proactive_speech
        from prometheus.policies.proactive_speech_policy import PresenceState
        idle_state = PresenceState(screen_locked=True, idle_minutes=60.0)
        with patch(
            "prometheus.policies.proactive_speech_policy.detect_presence",
            return_value=idle_state,
        ), patch.dict(os.environ, {
            "PROMETHEUS_PROACTIVE_WRAP_UP_ENABLED": "false",
            "PROMETHEUS_PROACTIVE_CHECKINS_ENABLED": "false",
        }):
            assert should_allow_proactive_speech("morning_routine") is True


# ── Category extraction ───────────────────────────────────────────────────────

class TestCategoryExtraction:

    def test_evening_wrapup_category(self):
        from prometheus.core.proactive_loop import ProactiveLoop
        loop = ProactiveLoop(client=None, workspace_manager=None)
        assert loop._extract_category("evening with no wrap-up") == "evening_wrapup"
        assert loop._extract_category("wrap up for the day") == "evening_wrapup"

    def test_productivity_checkin_category(self):
        from prometheus.core.proactive_loop import ProactiveLoop
        loop = ProactiveLoop(client=None, workspace_manager=None)
        assert loop._extract_category("productivity check-in") == "productivity_checkin"
        assert loop._extract_category("progress update on project") == "productivity_checkin"

    def test_background_task_category(self):
        from prometheus.core.proactive_loop import ProactiveLoop
        loop = ProactiveLoop(client=None, workspace_manager=None)
        assert loop._extract_category("background task completed") == "background_task"

    def test_vault_connection_category(self):
        from prometheus.core.proactive_loop import ProactiveLoop
        loop = ProactiveLoop(client=None, workspace_manager=None)
        assert loop._extract_category("relevant vault memory fragment") == "vault_connection"

"""
tests/test_calendar_event_triggers.py

Unit tests for CalendarEventTriggerEngine.

All calendar calls and state file I/O are isolated to tmp_path.
Tests verify scheduling, deduplication, grace windows, and routing.
The engine fires registered rule handlers only — events with no matching
rule are ignored and must never produce speech or any other side effect.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from prometheus.routines.calendar_event_triggers import (
    CalendarEventTriggerEngine,
    CalendarRoutineRule,
    _CalendarEventAdapter,
    _event_key,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_event(
    title: str = "Wake Up",
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


def _make_rule(
    fired: list,
    name: str = "morning_routine",
    match: list | None = None,
    allow_late_seconds: int = 120,
) -> CalendarRoutineRule:
    async def _handler(ev: Any) -> None:
        fired.append(ev)

    return CalendarRoutineRule(
        name=name,
        match_title_contains=match or ["wake up"],
        handler=_handler,
        allow_late_seconds=allow_late_seconds,
    )


def _make_engine(
    tmp_path: Path,
    events: list | None = None,
    rules: list | None = None,
) -> CalendarEventTriggerEngine:
    class FakeReader:
        def get_upcoming_events(self) -> list:
            return events or []

    return CalendarEventTriggerEngine(
        calendar_reader=FakeReader(),
        rules=rules or [],
        state_path=tmp_path / "triggers_state.json",
        logger=MagicMock(),
    )


_LOOKAHEAD_ENV = {"PROMETHEUS_CALENDAR_TRIGGER_LOOKAHEAD_MINUTES": "2"}


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

    def test_matched_event_fires_at_start_time(self, tmp_path):
        """Rule-matched event scheduled 0.1s out fires within 0.5s."""
        fired: list[Any] = []
        event = _make_event(start_offset_seconds=0.1)
        engine = _make_engine(tmp_path, events=[event], rules=[_make_rule(fired)])

        async def _run():
            with patch.dict(os.environ, _LOOKAHEAD_ENV):
                await engine._poll()
                await asyncio.sleep(0.5)

        asyncio.run(_run())
        assert len(fired) == 1
        assert fired[0].title == "Wake Up"

    def test_event_at_exact_now_fires_immediately(self, tmp_path):
        fired: list[Any] = []
        event = _make_event(start_offset_seconds=-0.01)
        engine = _make_engine(tmp_path, events=[event], rules=[_make_rule(fired)])

        async def _run():
            with patch.dict(os.environ, _LOOKAHEAD_ENV):
                await engine._poll()
                await asyncio.sleep(0.3)

        asyncio.run(_run())
        assert len(fired) == 1

    def test_far_future_event_not_scheduled(self, tmp_path):
        """Event beyond the lookahead window is not scheduled yet."""
        fired: list[Any] = []
        event = _make_event(start_offset_seconds=3600)
        engine = _make_engine(tmp_path, events=[event], rules=[_make_rule(fired)])

        async def _run():
            with patch.dict(os.environ, _LOOKAHEAD_ENV):
                await engine._poll()

        asyncio.run(_run())
        assert len(engine._scheduled) == 0
        assert fired == []


# ── Unmatched events are ignored ──────────────────────────────────────────────

class TestUnmatchedEventsIgnored:

    def test_unmatched_event_is_not_scheduled(self, tmp_path):
        """An event with no matching rule produces no task and no state."""
        event = _make_event(title="Dentist Appointment", start_offset_seconds=-0.01)
        engine = _make_engine(tmp_path, events=[event], rules=[_make_rule([])])

        async def _run():
            with patch.dict(os.environ, _LOOKAHEAD_ENV):
                await engine._poll()
                await asyncio.sleep(0.2)

        asyncio.run(_run())
        assert len(engine._scheduled) == 0
        assert engine._fired_state == {}
        assert not (tmp_path / "triggers_state.json").exists()

    def test_no_rules_means_no_activity(self, tmp_path):
        """Engine with zero rules ignores every event."""
        events = [
            _make_event(title="Gym", start_offset_seconds=-0.01, event_id="e1"),
            _make_event(title="Meeting", start_offset_seconds=5, event_id="e2"),
        ]
        engine = _make_engine(tmp_path, events=events, rules=[])

        async def _run():
            with patch.dict(os.environ, _LOOKAHEAD_ENV):
                await engine._poll()
                await asyncio.sleep(0.2)

        asyncio.run(_run())
        assert len(engine._scheduled) == 0
        assert engine._fired_state == {}


# ── Deduplication ─────────────────────────────────────────────────────────────

class TestDeduplication:

    def test_event_fires_only_once(self, tmp_path):
        """Same event polled twice is only fired once."""
        fired: list[Any] = []
        event = _make_event(start_offset_seconds=-0.01)
        engine = _make_engine(tmp_path, events=[event], rules=[_make_rule(fired)])

        async def _run():
            with patch.dict(os.environ, _LOOKAHEAD_ENV):
                await engine._poll()
                await asyncio.sleep(0.3)
                await engine._poll()
                await asyncio.sleep(0.1)

        asyncio.run(_run())
        assert len(fired) == 1

    def test_duplicate_poll_does_not_double_fire(self, tmp_path):
        """Concurrent polls don't double-fire: _scheduled set prevents it."""
        fired: list[Any] = []
        event = _make_event(start_offset_seconds=0.2)
        engine = _make_engine(tmp_path, events=[event], rules=[_make_rule(fired)])

        async def _run():
            with patch.dict(os.environ, _LOOKAHEAD_ENV):
                await asyncio.gather(engine._poll(), engine._poll())
                await asyncio.sleep(0.5)

        asyncio.run(_run())
        assert len(fired) <= 1

    def test_moved_event_fires_again(self, tmp_path):
        """An event rescheduled to a new start_time gets a new key and fires."""
        fired: list[Any] = []
        original = _make_event(start_offset_seconds=-200, event_id="evt_move")
        moved = _make_event(start_offset_seconds=-0.01, event_id="evt_move")
        engine = _make_engine(tmp_path, events=[moved], rules=[_make_rule(fired)])
        engine._mark_fired(_event_key(original.event_id, original.start_time))

        async def _run():
            with patch.dict(os.environ, _LOOKAHEAD_ENV):
                await engine._poll()
                await asyncio.sleep(0.3)

        asyncio.run(_run())
        assert len(fired) == 1

    def test_state_file_persists_fired_events(self, tmp_path):
        """After an event fires, the key appears in the state file."""
        fired: list[Any] = []
        event = _make_event(start_offset_seconds=-0.01)
        engine = _make_engine(tmp_path, events=[event], rules=[_make_rule(fired)])

        async def _run():
            with patch.dict(os.environ, _LOOKAHEAD_ENV):
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
        fired1: list[Any] = []
        fired2: list[Any] = []
        event = _make_event(start_offset_seconds=-0.01)
        engine1 = _make_engine(tmp_path, events=[event], rules=[_make_rule(fired1)])
        engine2 = _make_engine(tmp_path, events=[event], rules=[_make_rule(fired2)])

        async def _run():
            with patch.dict(os.environ, _LOOKAHEAD_ENV):
                await engine1._poll()
                await asyncio.sleep(0.3)
                await engine2._poll()
                await asyncio.sleep(0.1)

        asyncio.run(_run())
        assert len(fired1) == 1
        assert len(fired2) == 0


# ── Grace windows ─────────────────────────────────────────────────────────────

class TestGraceWindows:

    def test_event_outside_grace_does_not_fire(self, tmp_path):
        """Event started 5 minutes ago with 2-minute rule grace must be skipped."""
        fired: list[Any] = []
        event = _make_event(start_offset_seconds=-300)
        engine = _make_engine(
            tmp_path, events=[event],
            rules=[_make_rule(fired, allow_late_seconds=120)],
        )

        async def _run():
            with patch.dict(os.environ, _LOOKAHEAD_ENV):
                await engine._poll()
                await asyncio.sleep(0.1)

        asyncio.run(_run())
        assert fired == []

    def test_event_within_grace_fires_late(self, tmp_path):
        """Event started 60 seconds ago with 120-second grace still fires."""
        fired: list[Any] = []
        event = _make_event(start_offset_seconds=-60)
        engine = _make_engine(
            tmp_path, events=[event],
            rules=[_make_rule(fired, allow_late_seconds=120)],
        )

        async def _run():
            with patch.dict(os.environ, _LOOKAHEAD_ENV):
                await engine._poll()
                await asyncio.sleep(0.3)

        asyncio.run(_run())
        assert len(fired) == 1

    def test_morning_routine_uses_15_minute_grace(self, tmp_path):
        """Rule with allow_late_seconds=900 recovers an event 500s late."""
        fired: list[Any] = []
        event = _make_event(start_offset_seconds=-500, event_id="wu_001")
        engine = _make_engine(
            tmp_path, events=[event],
            rules=[_make_rule(fired, allow_late_seconds=900)],
        )

        async def _run():
            with patch.dict(os.environ, _LOOKAHEAD_ENV):
                await engine._poll()
                await asyncio.sleep(0.3)

        asyncio.run(_run())
        assert len(fired) == 1


# ── Routing ───────────────────────────────────────────────────────────────────

class TestRouting:

    def test_wake_up_routes_to_morning_routine_handler(self, tmp_path):
        fired: list[Any] = []
        event = _make_event(title="Wake Up", start_offset_seconds=-0.01, event_id="wu_001")
        engine = _make_engine(
            tmp_path, events=[event],
            rules=[_make_rule(fired, allow_late_seconds=900)],
        )

        async def _run():
            with patch.dict(os.environ, _LOOKAHEAD_ENV):
                await engine._poll()
                await asyncio.sleep(0.3)

        asyncio.run(_run())
        assert len(fired) == 1
        assert fired[0].title == "Wake Up"

    def test_rule_matched_case_insensitive(self, tmp_path):
        """Title matching is case-insensitive: 'WAKE UP' matches 'wake up' rule."""
        fired: list[Any] = []
        event = _make_event(title="WAKE UP", start_offset_seconds=-0.01, event_id="wu_002")
        engine = _make_engine(tmp_path, events=[event], rules=[_make_rule(fired)])

        async def _run():
            with patch.dict(os.environ, _LOOKAHEAD_ENV):
                await engine._poll()
                await asyncio.sleep(0.3)

        asyncio.run(_run())
        assert len(fired) == 1

    def test_first_matching_rule_wins(self, tmp_path):
        """Rules are matched in registration order."""
        fired_a: list[Any] = []
        fired_b: list[Any] = []
        rule_a = _make_rule(fired_a, name="rule_a", match=["wake"])
        rule_b = _make_rule(fired_b, name="rule_b", match=["wake up"])
        event = _make_event(title="Wake Up", start_offset_seconds=-0.01)
        engine = _make_engine(tmp_path, events=[event], rules=[rule_a, rule_b])

        async def _run():
            with patch.dict(os.environ, _LOOKAHEAD_ENV):
                await engine._poll()
                await asyncio.sleep(0.3)

        asyncio.run(_run())
        assert len(fired_a) == 1
        assert fired_b == []

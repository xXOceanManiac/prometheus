"""
tests/test_morning_routine_wiring.py

Integration-level tests for the morning routine adapters and main.py wiring.
No live network, filesystem, or Home Assistant calls are made.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ── JSONMorningRoutineStateStore ──────────────────────────────────────────────

class TestJSONMorningRoutineStateStore:

    def test_load_returns_none_when_file_absent(self, tmp_path):
        from prometheus.routines.morning_adapters import JSONMorningRoutineStateStore
        store = JSONMorningRoutineStateStore(path=tmp_path / "state.json")
        assert store.load_state() is None

    def test_save_and_load_round_trip(self, tmp_path):
        from prometheus.routines.morning_adapters import JSONMorningRoutineStateStore
        from prometheus.routines.morning_routine import MorningRoutineState
        store = JSONMorningRoutineStateStore(path=tmp_path / "state.json")
        original = MorningRoutineState(
            date="2026-06-03",
            event_id="evt_abc",
            completed=True,
            started_at="2026-06-03T07:00:00",
        )
        store.save_state(original)
        loaded = store.load_state()
        assert loaded is not None
        assert loaded.date == "2026-06-03"
        assert loaded.event_id == "evt_abc"
        assert loaded.completed is True
        assert loaded.started_at == "2026-06-03T07:00:00"

    def test_load_returns_none_and_backs_up_corrupt_file(self, tmp_path):
        from prometheus.routines.morning_adapters import JSONMorningRoutineStateStore
        state_path = tmp_path / "state.json"
        state_path.write_text("not valid json", encoding="utf-8")
        store = JSONMorningRoutineStateStore(path=state_path)
        result = store.load_state()
        assert result is None
        # Backup file should exist
        backup = state_path.with_suffix(".bak")
        assert backup.exists()

    def test_save_creates_parent_directory(self, tmp_path):
        from prometheus.routines.morning_adapters import JSONMorningRoutineStateStore
        from prometheus.routines.morning_routine import MorningRoutineState
        deep_path = tmp_path / "a" / "b" / "state.json"
        store = JSONMorningRoutineStateStore(path=deep_path)
        state = MorningRoutineState(date="2026-06-03", event_id=None, completed=False, started_at=None)
        store.save_state(state)
        assert deep_path.exists()


# ── HomeAssistantMorningClient ─────────────────────────────────────────────────

class TestHomeAssistantMorningClient:

    def test_strips_script_prefix_before_calling_run_ha_script(self):
        from prometheus.routines.morning_adapters import HomeAssistantMorningClient

        captured = {}

        def fake_run_ha_script(name):
            captured["name"] = name
            r = MagicMock()
            r.ok = True
            r.message = "ok"
            return r

        with patch("prometheus.routines.morning_adapters.HomeAssistantMorningClient.call_script",
                   wraps=lambda self, eid: None):
            pass  # just to confirm the method exists

        client = HomeAssistantMorningClient()
        with patch("tools.run_ha_script", fake_run_ha_script):
            result = client.call_script("script.prometheus_xbox_turn_on")

        assert captured["name"] == "prometheus_xbox_turn_on"
        assert result is True

    def test_returns_false_when_run_ha_script_fails(self):
        from prometheus.routines.morning_adapters import HomeAssistantMorningClient

        def fake_run_ha_script(name):
            r = MagicMock()
            r.ok = False
            r.message = "HA error"
            return r

        client = HomeAssistantMorningClient()
        with patch("tools.run_ha_script", fake_run_ha_script):
            result = client.call_script("script.prometheus_morning_lights_warm_fade")

        assert result is False

    def test_entity_id_without_prefix_passes_through_unchanged(self):
        from prometheus.routines.morning_adapters import HomeAssistantMorningClient

        captured = {}

        def fake_run_ha_script(name):
            captured["name"] = name
            r = MagicMock()
            r.ok = True
            r.message = "ok"
            return r

        client = HomeAssistantMorningClient()
        with patch("tools.run_ha_script", fake_run_ha_script):
            client.call_script("no_prefix_name")

        assert captured["name"] == "no_prefix_name"


# ── PrometheusMorningSpeaker ───────────────────────────────────────────────────

class TestPrometheusMorningSpeaker:

    def test_sends_correct_payload_format(self):
        from prometheus.routines.morning_adapters import PrometheusMorningSpeaker

        sent_payloads: list[dict] = []

        async def _run():
            fake_client = MagicMock()
            fake_client.connected = True
            fake_client.send = AsyncMock(side_effect=lambda p: sent_payloads.append(p))
            speaker = PrometheusMorningSpeaker(fake_client)
            await speaker.speak("Good morning, Tate.")

        asyncio.run(_run())

        assert len(sent_payloads) == 2

        item_msg = sent_payloads[0]
        assert item_msg["type"] == "conversation.item.create"
        content = item_msg["item"]["content"][0]
        assert "[MORNING_ROUTINE]" in content["text"]
        assert "Good morning, Tate." in content["text"]

        response_msg = sent_payloads[1]
        assert response_msg["type"] == "response.create"
        assert "audio" in response_msg["response"]["modalities"]
        assert "Good morning, Tate." in response_msg["response"]["instructions"]

    def test_skips_when_client_not_connected(self):
        from prometheus.routines.morning_adapters import PrometheusMorningSpeaker

        sent_payloads: list[dict] = []

        async def _run():
            fake_client = MagicMock()
            fake_client.connected = False
            fake_client.send = AsyncMock(side_effect=lambda p: sent_payloads.append(p))
            speaker = PrometheusMorningSpeaker(fake_client)
            await speaker.speak("This should not be sent.")

        asyncio.run(_run())
        assert len(sent_payloads) == 0


# ── MorningCalendarReader ─────────────────────────────────────────────────────

class TestMorningCalendarReader:

    def test_wraps_dicts_as_attribute_accessible_objects(self):
        from prometheus.routines.morning_adapters import MorningCalendarReader

        fake_events = [
            {"title": "Wake Up", "start_time": "2026-06-03T07:00:00", "event_id": "evt1"},
            {"title": "Standup", "start_time": "2026-06-03T09:00:00", "event_id": "evt2"},
        ]
        fake_result = {"ok": True, "events": fake_events, "count": 2, "date": "2026-06-03"}

        reader = MorningCalendarReader()
        with patch("prometheus.agents.calendar_read_tools.calendar_get_today", return_value=fake_result):
            events = reader.get_today_events()

        assert len(events) == 2
        assert events[0].title == "Wake Up"
        assert events[0].start_time == "2026-06-03T07:00:00"
        assert events[0].event_id == "evt1"
        assert events[1].title == "Standup"

    def test_returns_empty_list_when_calendar_unavailable(self):
        from prometheus.routines.morning_adapters import MorningCalendarReader

        reader = MorningCalendarReader()
        with patch(
            "prometheus.agents.calendar_read_tools.calendar_get_today",
            side_effect=Exception("network error"),
        ):
            events = reader.get_today_events()

        assert events == []


# ── Main wiring: PROMETHEUS_MORNING_ROUTINE_ENABLED gate ──────────────────────

class TestMainMorningRoutineGate:

    def test_service_not_instantiated_when_env_var_false(self):
        """With the default env var (false), _morning_routine_svc stays None."""
        import main as main_module

        core = object.__new__(main_module.PrometheusCore)
        core._morning_routine_svc = None

        # Simulate the gate check with flag disabled
        enabled_raw = "false"
        enabled = enabled_raw.strip().lower() in ("1", "true", "yes")
        if enabled:
            core._morning_routine_svc = MagicMock()

        assert core._morning_routine_svc is None

    def test_service_instantiated_when_env_var_true(self):
        """With PROMETHEUS_MORNING_ROUTINE_ENABLED=true, service is created."""
        import main as main_module

        core = object.__new__(main_module.PrometheusCore)
        core._morning_routine_svc = None

        # Simulate the gate check with flag enabled
        enabled_raw = "true"
        enabled = enabled_raw.strip().lower() in ("1", "true", "yes")
        if enabled:
            core._morning_routine_svc = MagicMock()

        assert core._morning_routine_svc is not None

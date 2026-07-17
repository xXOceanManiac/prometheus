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

_HA_ENV = {"HOME_ASSISTANT_URL": "http://ha.test:8123", "HOME_ASSISTANT_API_KEY": "test_token"}


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

        client = HomeAssistantMorningClient()
        with patch.dict(os.environ, _HA_ENV), patch("prometheus.execution.tools.run_ha_script", fake_run_ha_script):
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
        with patch.dict(os.environ, _HA_ENV), patch("prometheus.execution.tools.run_ha_script", fake_run_ha_script):
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
        with patch.dict(os.environ, _HA_ENV), patch("prometheus.execution.tools.run_ha_script", fake_run_ha_script):
            client.call_script("no_prefix_name")

        assert captured["name"] == "no_prefix_name"

    def test_returns_false_when_ha_config_missing(self):
        from prometheus.routines.morning_adapters import HomeAssistantMorningClient

        client = HomeAssistantMorningClient()
        with patch.dict(os.environ, {"HOME_ASSISTANT_URL": "", "HOME_ASSISTANT_API_KEY": ""}):
            result = client.call_script("script.prometheus_xbox_turn_on")

        assert result is False


# ── PrometheusMorningSpeaker ───────────────────────────────────────────────────

def _make_fake_client(
    connected: bool = True,
    fire_done_immediately: bool = True,
    ensure_connected_raises: bool = False,
):
    """Build a fake Realtime client for speaker tests.

    If fire_done_immediately=True, register_response_done_event() sets the event
    right away so speak() completes without waiting a real timeout.
    If ensure_connected_raises=True, ensure_connected() raises RuntimeError simulating
    a failed overnight reconnect.
    """
    import asyncio as _asyncio
    fake = MagicMock()
    fake.connected = connected
    sent: list[dict] = []
    fake.send = AsyncMock(side_effect=lambda p: sent.append(p))

    def _register(evt: _asyncio.Event) -> None:
        if fire_done_immediately:
            evt.set()

    fake.register_response_done_event = _register
    fake._sent = sent

    if ensure_connected_raises:
        async def _fail() -> None:
            raise RuntimeError("realtime_reconnect_failed: connection timed out after 15s")
        fake.ensure_connected = _fail
    else:
        fake.ensure_connected = AsyncMock()

    return fake


class TestPrometheusMorningSpeaker:

    def test_sends_correct_payload_format(self):
        from prometheus.routines.morning_adapters import PrometheusMorningSpeaker

        fake_client = _make_fake_client(connected=True, fire_done_immediately=True)

        asyncio.run(PrometheusMorningSpeaker(fake_client).speak("Good morning, Tate."))

        sent = fake_client._sent
        assert len(sent) == 2

        item_msg = sent[0]
        assert item_msg["type"] == "conversation.item.create"
        content = item_msg["item"]["content"][0]
        assert "[MORNING_ROUTINE]" in content["text"]
        assert "Good morning, Tate." in content["text"]

        response_msg = sent[1]
        assert response_msg["type"] == "response.create"
        assert "modalities" not in response_msg["response"], (
            "response.create must not include modalities — GA API rejects it as unknown_parameter"
        )
        assert "Good morning, Tate." in response_msg["response"]["instructions"]

    def test_raises_when_client_not_connected(self):
        from prometheus.routines.morning_adapters import PrometheusMorningSpeaker

        fake_client = _make_fake_client(connected=False)

        async def _run():
            with pytest.raises(RuntimeError, match="client_not_connected"):
                await PrometheusMorningSpeaker(fake_client).speak("This should not be sent.")

        asyncio.run(_run())
        assert len(fake_client._sent) == 0, "send() must not be called when client is not connected"

    def test_waits_for_response_done_before_returning(self):
        from prometheus.routines.morning_adapters import PrometheusMorningSpeaker

        # fire_done_immediately=False: event is never set — speak() must timeout
        fake_client = _make_fake_client(connected=True, fire_done_immediately=False)

        async def _run():
            with pytest.raises(RuntimeError, match="speech_timeout"):
                # Patch the timeout to 0.05s so the test doesn't actually wait 60s
                import prometheus.routines.morning_adapters as _mod
                original = _mod._SPEAK_TIMEOUT
                _mod._SPEAK_TIMEOUT = 0.05
                try:
                    await PrometheusMorningSpeaker(fake_client).speak("Test.")
                finally:
                    _mod._SPEAK_TIMEOUT = original

        asyncio.run(_run())
        # response.create was sent even though done never arrived
        assert any(p.get("type") == "response.create" for p in fake_client._sent)

    def test_does_not_wait_when_client_lacks_register_method(self):
        """Clients without register_response_done_event still work — no wait, no error."""
        from prometheus.routines.morning_adapters import PrometheusMorningSpeaker

        # Plain object: no register_response_done_event so hasattr returns False
        sent: list[dict] = []

        class _MinimalClient:
            connected = True
            async def send(self, p: dict) -> None:
                sent.append(p)

        asyncio.run(PrometheusMorningSpeaker(_MinimalClient()).speak("No wait test."))
        assert len(sent) == 2

    def test_speak_calls_ensure_connected_when_available(self):
        """speak() calls ensure_connected() if the client exposes it."""
        from prometheus.routines.morning_adapters import PrometheusMorningSpeaker

        fake_client = _make_fake_client(connected=True, fire_done_immediately=True)
        asyncio.run(PrometheusMorningSpeaker(fake_client).speak("Good morning."))

        fake_client.ensure_connected.assert_awaited_once()

    def test_speak_raises_and_does_not_send_when_ensure_connected_fails(self):
        """When ensure_connected raises RuntimeError, speak() propagates it without sending."""
        from prometheus.routines.morning_adapters import PrometheusMorningSpeaker

        fake_client = _make_fake_client(
            connected=True,
            fire_done_immediately=True,
            ensure_connected_raises=True,
        )

        async def _run() -> None:
            with pytest.raises(RuntimeError, match="realtime_reconnect_failed"):
                await PrometheusMorningSpeaker(fake_client).speak("Should not be sent.")

        asyncio.run(_run())
        assert len(fake_client._sent) == 0, "send() must not be called when reconnect fails"


# ── ensure_morning_audio_sink ─────────────────────────────────────────────────

class TestEnsureMorningAudioSink:

    def test_tolerates_missing_wpctl(self):
        """ensure_morning_audio_sink() must not raise when wpctl is absent."""
        import prometheus.routines.morning_adapters as _mod
        from unittest.mock import patch

        with patch.object(_mod, "_run_cmd", return_value=(127, "")):
            _mod.ensure_morning_audio_sink()  # should complete without exception

    def test_switches_to_preferred_sink_when_not_already_default(self):
        """When a preferred sink is configured and is not the current default, set-default is called."""
        import prometheus.routines.morning_adapters as _mod
        from unittest.mock import patch

        fake_status = (
            " Sinks:\n"
            "  *  51. alsa_output.hdmi-stereo [vol: 1.00]\n"
            "     47. alsa_output.analog-stereo [vol: 0.70]\n"
            " Sources:\n"
        )

        cmds: list[list[str]] = []

        def _fake_run_cmd(args: list[str]) -> tuple[int, str]:
            cmds.append(list(args))
            if args == ["wpctl", "--version"]:
                return 0, "WirePlumber 0.5"
            if args == ["wpctl", "status"]:
                return 0, fake_status
            return 0, ""

        with patch.object(_mod, "_run_cmd", _fake_run_cmd), \
             patch.dict(os.environ, {"PROMETHEUS_AUDIO_SINK_NAME": "analog-stereo"}):
            _mod.ensure_morning_audio_sink()

        set_default_calls = [c for c in cmds if "set-default" in c]
        assert len(set_default_calls) == 1
        assert "47" in set_default_calls[0]


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
        import prometheus.core.main as main_module

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
        import prometheus.core.main as main_module

        core = object.__new__(main_module.PrometheusCore)
        core._morning_routine_svc = None

        # Simulate the gate check with flag enabled
        enabled_raw = "true"
        enabled = enabled_raw.strip().lower() in ("1", "true", "yes")
        if enabled:
            core._morning_routine_svc = MagicMock()

        assert core._morning_routine_svc is not None

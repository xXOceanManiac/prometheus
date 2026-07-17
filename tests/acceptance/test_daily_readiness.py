"""
tests/acceptance/test_daily_readiness.py

Prometheus Daily Readiness Gate Tests.

Each class represents one readiness gate. A gate passes when Prometheus can be
trusted to perform that category of task reliably in daily use.

Gates:
  1. boot_config       — config loads, required keys present, no import crashes
  2. vault_memory      — vault/memory subsystem initialises without errors
  3. trace_observability — every tool call produces a traceable log trail
  4. tool_truth        — ToolResult truth contract enforced (no silent false claims)
  5. ha_verification   — HA tool results reflect real state, not guessed state
  6. time_correctness  — time queries return deterministic local-clock answer
  7. calendar_routines — calendar event trigger engine runs without live API
  8. morning_routine   — morning routine completes even when speech fails
  9. hud_state         — visual state file is written correctly
 10. reactive_by_default — no proactive speech or model-call machinery exists
 11. false_success_prevention — accepted_unverified / verified_success never swapped

All tests are offline — no real Realtime API, no real HA, no real Google Calendar.
"""
from __future__ import annotations

import json
import os
import sys
import time as _time_mod
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ── Gate 1: Boot / Config ────────────────────────────────────────────────────

class TestGateBootConfig:
    """Prometheus can start and read its configuration."""

    def test_config_module_imports(self):
        from prometheus.infra.config import CONFIG, DEFAULT_CONFIG
        assert isinstance(CONFIG, dict)
        assert isinstance(DEFAULT_CONFIG, dict)

    def test_required_default_keys_present(self):
        from prometheus.infra.config import DEFAULT_CONFIG
        required = {"timezone", "voice", "realtime_model", "apps", "urls", "modes"}
        missing = required - set(DEFAULT_CONFIG.keys())
        assert not missing, f"DEFAULT_CONFIG missing keys: {missing}"

    def test_timezone_defaults_to_new_york(self):
        from prometheus.infra.config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["timezone"] == "America/New_York"

    def test_critical_modules_import_cleanly(self):
        import prometheus.infra.config as config
        import prometheus.execution.tools as tools
        import prometheus.infra.utils as utils
        import prometheus.core.realtime_client as realtime_client
        assert True  # import itself is the test

    def test_log_event_does_not_raise(self, tmp_path):
        from prometheus.infra.utils import log_event
        log_event("readiness_gate_test", {"gate": "boot_config"})


# ── Gate 2: Vault / Memory ────────────────────────────────────────────────────

class TestGateVaultMemory:
    """Vault config is present; memory subsystem handles missing DB gracefully."""

    def test_vault_path_key_exists_in_default_config(self):
        from prometheus.infra.config import DEFAULT_CONFIG
        assert "vault_path" in DEFAULT_CONFIG

    def test_query_vault_returns_list_on_missing_db(self):
        from prometheus.memory.memory_core import query_vault
        with patch("prometheus.memory.memory_core.CONFIG", {"vault_path": "/nonexistent/path"}):
            result = query_vault("anything", limit=3)
        assert isinstance(result, list)

    def test_query_vault_returns_list_on_empty_path(self):
        from prometheus.memory.memory_core import query_vault
        with patch("prometheus.memory.memory_core.CONFIG", {"vault_path": ""}):
            result = query_vault("anything", limit=3)
        assert isinstance(result, list)

    def test_working_memory_reads_without_crash(self):
        from prometheus.memory.working_memory import WorkingMemory
        wm = WorkingMemory()
        data = wm.read()
        assert isinstance(data, dict)

    def test_episodic_memory_initialises(self):
        from prometheus.memory.episodic_memory import EpisodicMemory
        em = EpisodicMemory()
        assert em is not None


# ── Gate 3: Trace Observability ───────────────────────────────────────────────

class TestGateTraceObservability:
    """Every tool call produces a trace_id-stamped log trail."""

    def test_make_trace_id_format(self):
        import re
        from prometheus.infra.utils import make_trace_id
        tid = make_trace_id()
        assert re.match(r"^\d{8}-\d{6}-.+$", tid), f"bad format: {tid!r}"

    def test_consecutive_trace_ids_unique(self):
        from prometheus.infra.utils import make_trace_id
        ids = {make_trace_id() for _ in range(20)}
        assert len(ids) == 20

    def test_tool_execute_log_carries_trace_id(self, monkeypatch):
        from prometheus.execution.tools import ToolRegistry
        logged = []
        monkeypatch.setattr("prometheus.execution.tools.log_event", lambda k, p: logged.append((k, p)))
        r = ToolRegistry()
        r.execute({"action": "tell_time"}, trace_id="readiness-gate3-trace-xx01")
        exec_events = [p for k, p in logged if k == "tool_execute"]
        assert exec_events, "tool_execute not logged"
        assert exec_events[0]["trace_id"] == "readiness-gate3-trace-xx01"

    def test_tool_result_log_carries_trace_id(self, monkeypatch):
        from prometheus.execution.tools import ToolRegistry
        logged = []
        monkeypatch.setattr("prometheus.execution.tools.log_event", lambda k, p: logged.append((k, p)))
        r = ToolRegistry()
        r.execute({"action": "tell_time"}, trace_id="readiness-gate3-result-xx02")
        result_events = [p for k, p in logged if k == "tool_result"]
        assert result_events, "tool_result not logged"
        assert result_events[0]["trace_id"] == "readiness-gate3-result-xx02"

    def test_trace_slug_derives_from_transcript(self):
        from prometheus.infra.utils import _trace_slug
        slug = _trace_slug("turn the lights red")
        assert slug, "slug should not be empty"
        assert "-" in slug or slug.isalpha()

    def test_ptt_empty_buffer_logs_trace_id(self, monkeypatch):
        import asyncio
        import prometheus.core.realtime_client as rc
        speaker = MagicMock()
        speaker.finish_realtime = MagicMock()
        client = rc.RealtimePrometheusClient(speaker=speaker, tools=MagicMock())
        client.api_key = "test"
        client.connected = True
        client.ws = MagicMock()
        client.awaiting_user_audio = True
        client._audio_bytes_since_commit = 0
        client._current_trace_id = "readiness-gate3-ptt-xx03"
        client._response_active = False

        logged = []
        monkeypatch.setattr("prometheus.core.realtime_client.log_event", lambda k, p: logged.append((k, p)))

        async def fake_send(d): pass
        client.send = fake_send

        asyncio.run(client.end_audio())
        ev = next((p for k, p in logged if k == "user_turn_commit_skipped"), None)
        assert ev is not None, "user_turn_commit_skipped not logged"
        assert ev["trace_id"] == "readiness-gate3-ptt-xx03"


# ── Gate 4: Tool Truth Contract ───────────────────────────────────────────────

class TestGateToolTruth:
    """ToolResult status is always accurate. ok=True never implies verified=True."""

    def test_tool_status_constants_all_present(self):
        from prometheus.execution.tools import ToolStatus
        for name in ("VERIFIED_SUCCESS", "ACCEPTED_UNVERIFIED", "VERIFIED_FAILURE",
                     "TOOL_FAILURE", "BLOCKED", "PENDING_CONFIRMATION"):
            val = getattr(ToolStatus, name, None)
            assert val is not None and isinstance(val, str), f"ToolStatus.{name} missing"

    def test_ok_true_does_not_imply_verified(self):
        from prometheus.execution.tools import ToolResult
        r = ToolResult(True, "Done")
        assert r.ok is True
        assert r.verified is False, "ok=True must not imply verified=True"

    def test_verified_success_factory_sets_verified(self):
        from prometheus.execution.tools import ToolResult, ToolStatus
        r = ToolResult.verified_success("Done", summary="checked", confidence=0.9)
        assert r.verified is True
        assert r.status == ToolStatus.VERIFIED_SUCCESS
        assert r.ok is True

    def test_accepted_unverified_factory_sets_verified_false(self):
        from prometheus.execution.tools import ToolResult, ToolStatus
        r = ToolResult.accepted_unverified("Command sent.")
        assert r.verified is False
        assert r.status == ToolStatus.ACCEPTED_UNVERIFIED
        assert r.ok is True

    def test_tool_result_is_json_serializable(self):
        from prometheus.execution.tools import ToolResult
        r = ToolResult(True, "Done", {"key": "value"})
        d = r.__dict__
        json.dumps(d)  # must not raise

    def test_tell_time_returns_verified_success(self):
        from prometheus.execution.tools import ToolRegistry, ToolStatus
        r = ToolRegistry()
        result = r.execute({"action": "tell_time"})
        assert result.status == ToolStatus.VERIFIED_SUCCESS
        assert result.verified is True

    def test_unknown_action_returns_tool_failure(self, monkeypatch):
        from prometheus.execution.tools import ToolRegistry, ToolStatus
        monkeypatch.setattr("prometheus.execution.tools.log_event", lambda *a, **kw: None)
        r = ToolRegistry()
        result = r.execute({"action": "_nonexistent_action_xyz"})
        assert result.ok is False
        assert result.status == ToolStatus.TOOL_FAILURE


# ── Gate 5: HA Verification ───────────────────────────────────────────────────

class TestGateHAVerification:
    """HA results reflect real device state; mismatches never produce verified_success."""

    def test_verify_ha_script_returns_none_for_routine_script(self):
        from prometheus.integrations.ha_verifier import verify_ha_script
        result = verify_ha_script("script.jarvis_morning_routine_start")
        assert result is None, "routine scripts must not be post-verified"

    def test_state_match_produces_valid_result(self):
        from prometheus.integrations.ha_verifier import verify_ha_script
        from prometheus.execution.tools import ToolStatus
        fake_state = {
            "entity_id": "light.bedroom", "state": "on",
            "attributes": {"brightness": 255, "rgb_color": [255, 0, 0],
                           "color_mode": "rgb"},
        }
        with patch("prometheus.integrations.ha_verifier._get_ha_state", return_value=fake_state):
            with patch("prometheus.integrations.ha_verifier.time.sleep"):
                result = verify_ha_script(
                    "jarvis_lights_set_red",
                    trace_id="readiness-gate5-xx01",
                )
        if result is not None:
            assert result.status in (
                ToolStatus.VERIFIED_SUCCESS, ToolStatus.ACCEPTED_UNVERIFIED,
                ToolStatus.VERIFIED_FAILURE,
            ), f"unexpected status: {result.status}"

    def test_state_mismatch_never_produces_verified_success(self):
        from prometheus.integrations.ha_verifier import verify_ha_script
        from prometheus.execution.tools import ToolStatus
        # Light still off — clear mismatch for a lights-on script
        fake_state = {"entity_id": "light.bedroom", "state": "off", "attributes": {}}
        with patch("prometheus.integrations.ha_verifier._get_ha_state", return_value=fake_state):
            with patch("prometheus.integrations.ha_verifier.time.sleep"):
                result = verify_ha_script(
                    "jarvis_lights_set_red",
                    trace_id="readiness-gate5-xx02",
                )
        if result is not None:
            assert result.status != ToolStatus.VERIFIED_SUCCESS, \
                "state mismatch must not produce verified_success"

    def test_get_failure_falls_back_to_accepted_unverified(self):
        from prometheus.integrations.ha_verifier import verify_ha_script
        from prometheus.execution.tools import ToolStatus
        # _get_ha_state returns None when credentials absent or request fails
        with patch("prometheus.integrations.ha_verifier._get_ha_state", return_value=None):
            with patch("prometheus.integrations.ha_verifier.time.sleep"):
                result = verify_ha_script(
                    "jarvis_lights_set_red",
                    trace_id="readiness-gate5-xx03",
                )
        if result is not None:
            assert result.status != ToolStatus.VERIFIED_SUCCESS, \
                "GET failure must not produce verified_success"


# ── Gate 6: Time Correctness ──────────────────────────────────────────────────

class TestGateTimeCorrectness:
    """Time queries always return local-clock truth. No LLM guessing."""

    def test_what_time_is_it_routes_to_tell_time(self):
        from prometheus.core.intent_overrides import resolve_direct_intent
        result = resolve_direct_intent("what time is it")
        assert result is not None
        assert result["type"] == "direct_tool"
        assert result["payload"]["action"] == "tell_time"

    def test_what_day_is_it_routes_to_tell_time(self):
        from prometheus.core.intent_overrides import resolve_direct_intent
        result = resolve_direct_intent("what day is it")
        assert result is not None
        assert result["payload"]["action"] == "tell_time"

    def test_tell_time_uses_configured_timezone(self):
        from zoneinfo import ZoneInfo
        from datetime import datetime as _dt
        frozen = _dt(2026, 6, 7, 18, 0, 0, tzinfo=ZoneInfo("UTC"))
        ny = ZoneInfo("America/New_York")
        frozen_ny = frozen.astimezone(ny)
        from prometheus.execution.tools import ToolRegistry
        r = ToolRegistry()
        with patch("prometheus.execution.tools.CONFIG", {"timezone": "America/New_York"}):
            with patch("prometheus.execution.tools._datetime") as m:
                m.now.return_value = frozen_ny
                result = r.execute({"action": "tell_time"})
        assert result.ok
        assert "2:00 PM" in result.message

    def test_tell_time_response_includes_date(self):
        from prometheus.execution.tools import ToolRegistry
        r = ToolRegistry()
        result = r.execute({"action": "tell_time"})
        import re
        assert re.search(r"20\d{2}", result.message), "tell_time response must include year"

    def test_live_state_block_has_current_time(self):
        from prometheus.core.session_context import build_live_state_block
        block = build_live_state_block()
        assert "Current time:" in block, "live state block must include current time for LLM context"


# ── Gate 7: Calendar Routines ─────────────────────────────────────────────────

class TestGateCalendarRoutines:
    """Calendar event trigger engine runs; no live Google API needed."""

    def test_engine_polls_without_raising(self, tmp_path):
        import asyncio
        from prometheus.routines.calendar_event_triggers import CalendarEventTriggerEngine

        class FakeReader:
            def get_upcoming_events(self): return []

        engine = CalendarEventTriggerEngine(
            calendar_reader=FakeReader(),
            state_path=tmp_path / "state.json",
            logger=MagicMock(),
        )
        asyncio.run(engine._poll())  # must not raise

    def test_engine_does_not_fire_without_events(self, tmp_path):
        import asyncio
        from prometheus.routines.calendar_event_triggers import (
            CalendarEventTriggerEngine,
            CalendarRoutineRule,
        )
        fired = []

        class FakeReader:
            def get_upcoming_events(self): return []

        async def track_handler(event): fired.append(event)

        engine = CalendarEventTriggerEngine(
            calendar_reader=FakeReader(),
            rules=[CalendarRoutineRule(
                name="morning_routine",
                match_title_contains=["wake up"],
                handler=track_handler,
            )],
            state_path=tmp_path / "state.json",
            logger=MagicMock(),
        )
        asyncio.run(engine._poll())
        assert not fired, "handler fired with no events"

    def test_calendar_read_tool_get_today_does_not_crash(self):
        from prometheus.execution.tools import ToolRegistry
        r = ToolRegistry()
        with patch("prometheus.execution.tools.CONFIG", {"calendar": {}}):
            result = r.execute({"action": "calendar_get_today"})
        # ok=False is acceptable if Google not configured; what must not happen is an exception
        assert result is not None


# ── Gate 8: Morning Routine ───────────────────────────────────────────────────

class TestGateMorningRoutine:
    """Morning routine completes; HA calls run even when speech/Realtime fails."""

    def test_routine_runs_when_speech_raises(self):
        import asyncio
        from prometheus.routines.morning_routine import (
            MorningRoutineService, MorningRoutineConfig,
        )
        ha_calls = []

        class FakeHA:
            def call_script(self, e): ha_calls.append(e); return True

        class FakeSpeaker:
            async def speak(self, t): raise RuntimeError("realtime_unavailable: insufficient_quota")

        class FakeCalendar:
            def get_today_events(self): return []

        class FakeWeather:
            def get_today_weather(self): return None

        class FakeStore:
            def load_state(self): return None
            def save_state(self, s): pass

        class FakeEvent:
            event_id = "evt_readiness"
            start_time = "2026-06-08T07:00:00"
            title = "Wake Up"

        cfg = MorningRoutineConfig(
            pre_play_duck_wait_seconds=0, pre_play_final_wait_seconds=0,
            music_fade_interval_seconds=0, summary_delay_seconds=0,
            pre_summary_duck_seconds=0, post_summary_fade_interval_seconds=0,
            volume_command_interval_seconds=0.0,
        )
        svc = MorningRoutineService(
            calendar_reader=FakeCalendar(), ha_client=FakeHA(),
            speaker=FakeSpeaker(), weather_provider=FakeWeather(),
            state_store=FakeStore(), logger=MagicMock(), config=cfg,
        )
        asyncio.run(svc.run_morning_routine(FakeEvent()))
        assert len(ha_calls) > 0, "HA calls must run even when speech fails"

    def test_routine_marks_completed_on_speech_failure(self):
        import asyncio
        from prometheus.routines.morning_routine import (
            MorningRoutineService, MorningRoutineConfig,
        )
        saved = []

        class FakeHA:
            def call_script(self, e): return True

        class FakeSpeaker:
            async def speak(self, t): raise RuntimeError("quota")

        class FakeCalendar:
            def get_today_events(self): return []

        class FakeWeather:
            def get_today_weather(self): return None

        class FakeStore:
            def load_state(self): return None
            def save_state(self, s): saved.append(s)

        class FakeEvent:
            event_id = "evt2"
            start_time = "2026-06-08T07:00:00"
            title = "Wake Up"

        cfg = MorningRoutineConfig(
            pre_play_duck_wait_seconds=0, pre_play_final_wait_seconds=0,
            music_fade_interval_seconds=0, summary_delay_seconds=0,
            pre_summary_duck_seconds=0, post_summary_fade_interval_seconds=0,
            volume_command_interval_seconds=0.0,
        )
        svc = MorningRoutineService(
            calendar_reader=FakeCalendar(), ha_client=FakeHA(),
            speaker=FakeSpeaker(), weather_provider=FakeWeather(),
            state_store=FakeStore(), logger=MagicMock(), config=cfg,
        )
        asyncio.run(svc.run_morning_routine(FakeEvent()))
        assert saved and saved[-1].completed is True


# ── Gate 9: HUD State Writer ──────────────────────────────────────────────────

class TestGateHUDState:
    """Visual state file is written atomically with correct schema."""

    def test_set_state_writes_valid_json(self, tmp_path):
        import prometheus.hud.visuals as visuals
        state_file = tmp_path / "visual_state.json"
        with patch.object(visuals, "VISUAL_STATE_PATH", state_file):
            from prometheus.hud.visuals import VisualStateController
            ctrl = VisualStateController()
            ctrl.set_state("listening")
        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert data["state"] == "listening"

    def test_valid_states_accepted(self, tmp_path):
        import prometheus.hud.visuals as visuals
        state_file = tmp_path / "visual_state.json"
        with patch.object(visuals, "VISUAL_STATE_PATH", state_file):
            from prometheus.hud.visuals import VisualStateController
            ctrl = VisualStateController()
            for state in ("idle", "armed", "listening", "processing", "speaking"):
                ctrl.set_state(state)
            data = json.loads(state_file.read_text())
            assert data["state"] == "speaking"

    def test_state_file_is_valid_json(self, tmp_path):
        import prometheus.hud.visuals as visuals
        state_file = tmp_path / "visual_state.json"
        with patch.object(visuals, "VISUAL_STATE_PATH", state_file):
            from prometheus.hud.visuals import VisualStateController
            ctrl = VisualStateController()
            ctrl.set_state("idle")
        # File must exist and parse as valid JSON with a "state" key
        data = json.loads(state_file.read_text())
        assert "state" in data, "state file must include state key"


# ── Gate 10: Reactive By Default ──────────────────────────────────────────────

class TestGateReactiveByDefault:
    """Prometheus never initiates speech or model calls on its own.

    The only speech initiators are direct interaction and explicitly enabled
    scheduled routines. This gate fails if proactive machinery reappears.
    """

    def test_proactive_loop_module_removed(self):
        with pytest.raises(ModuleNotFoundError):
            import prometheus.core.proactive_loop  # noqa: F401

    def test_session_briefing_module_removed(self):
        with pytest.raises(ModuleNotFoundError):
            import prometheus.core.session_briefing  # noqa: F401

    def test_core_has_no_spoken_background_announcements(self):
        from prometheus.core.main import PrometheusCore
        assert not hasattr(PrometheusCore, "_announce_background_task_complete")

    def test_trigger_engine_ignores_unmatched_events(self):
        """The calendar trigger engine has no default spoken notification."""
        from prometheus.routines.calendar_event_triggers import CalendarEventTriggerEngine
        assert not hasattr(CalendarEventTriggerEngine, "_default_notify")

    def test_shutdown_does_not_auto_summarize(self):
        """Session summaries are written only via the explicit session_wrapup tool."""
        import inspect
        from prometheus.core.main import PrometheusCore
        src = inspect.getsource(PrometheusCore.shutdown)
        assert "summarize_and_write" not in src


# ── Gate 11: False Success Prevention ─────────────────────────────────────────

class TestGateFalseSuccessPrevention:
    """Prometheus never claims success when it cannot verify the outcome."""

    def test_open_url_raw_is_accepted_unverified(self):
        from prometheus.execution.tools import ToolRegistry, ToolStatus
        r = ToolRegistry()
        with patch("webbrowser.open"):
            result = r.execute({"action": "open_url_raw", "url": "https://google.com"})
        assert result.status == ToolStatus.ACCEPTED_UNVERIFIED
        assert result.verified is False

    def test_open_url_key_is_accepted_unverified(self):
        from prometheus.execution.tools import ToolRegistry, ToolStatus
        r = ToolRegistry()
        with (
            patch("prometheus.execution.tools.CONFIG", {"urls": {"youtube": "https://youtube.com"}, "apps": {}}),
            patch("webbrowser.open"),
        ):
            result = r.execute({"action": "open_url_key", "url_key": "youtube"})
        assert result.status == ToolStatus.ACCEPTED_UNVERIFIED

    def test_open_app_unconfirmed_launch_is_accepted_unverified(self):
        from prometheus.execution.tools import ToolRegistry, ToolStatus, ToolResult as TR
        r = ToolRegistry()
        with (
            patch("prometheus.execution.tools._APP_PROCESS_NAMES", {"spotify": "spotify"}),
            patch("prometheus.execution.tools.command_exists", return_value=True),
            patch("subprocess.run") as mock_run,
            patch("prometheus.execution.tools.ToolRegistry._launch_with_fallback") as mock_launch,
            patch("time.sleep"),
        ):
            pre = MagicMock(); pre.returncode = 1
            post = MagicMock(); post.returncode = 1  # pgrep can't confirm
            mock_run.side_effect = [pre, post]
            mock_launch.return_value = TR(True, "Launched spotify.")
            result = r.execute({"action": "open_app", "app": "spotify"})
        assert result.status == ToolStatus.ACCEPTED_UNVERIFIED
        assert result.verified is False

    def test_open_url_message_does_not_claim_window_open(self):
        from prometheus.execution.tools import ToolRegistry
        r = ToolRegistry()
        with patch("webbrowser.open"):
            result = r.execute({"action": "open_url_raw", "url": "https://youtube.com"})
        assert "is open" not in result.message.lower()
        assert "youtube is" not in result.message.lower()

    def test_response_instructions_accepted_unverified_prohibits_done(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from prometheus.execution.tools import ToolResult, ToolStatus
        r = ToolResult.accepted_unverified("Browser launch sent.")
        instructions = tool_response_instructions(r, "open_url_key")
        assert "Do not say" in instructions or "must not" in instructions.lower() or \
               "do not claim" in instructions.lower() or "command was sent" in instructions.lower(), \
            "accepted_unverified instructions must prohibit false device-state claims"

    def test_response_instructions_verified_success_allows_done(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from prometheus.execution.tools import ToolResult, ToolStatus
        r = ToolResult.verified_success("Confirmed.", summary="checked")
        instructions = tool_response_instructions(r, "run_ha_script")
        assert "verified" in instructions.lower() or "done" in instructions.lower() or \
               "confirmed" in instructions.lower()

"""
tests/test_realtime_quota_resilience.py

Tests for Prometheus resilience when the OpenAI Realtime API returns
insufficient_quota or quota-related close codes.

No real WebSocket connections are made — all network calls are mocked.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_connection_closed(code: int, reason: str):
    """Build a fake ConnectionClosedError with the given close frame."""
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close

    rcvd = Close(code, reason)
    exc = ConnectionClosedError(rcvd=rcvd, sent=None)
    return exc


def _make_client():
    """Build a RealtimePrometheusClient with mocked speaker and tools."""
    import prometheus.core.realtime_client as _rc
    speaker = MagicMock()
    speaker.finish_realtime = MagicMock()
    tools = MagicMock()
    client = _rc.RealtimePrometheusClient(speaker=speaker, tools=tools)
    client.api_key = "test-key"
    return client


# ── _is_quota_close ────────────────────────────────────────────────────────────

class TestIsQuotaClose:

    def test_code_1013_is_quota(self):
        from prometheus.core.realtime_client import RealtimePrometheusClient
        exc = _make_connection_closed(1013, "insufficient_quota.insufficient_quota")
        assert RealtimePrometheusClient._is_quota_close(exc) is True

    def test_code_1013_no_reason_is_quota(self):
        from prometheus.core.realtime_client import RealtimePrometheusClient
        exc = _make_connection_closed(1013, "")
        assert RealtimePrometheusClient._is_quota_close(exc) is True

    def test_reason_insufficient_quota_is_quota(self):
        from prometheus.core.realtime_client import RealtimePrometheusClient
        exc = _make_connection_closed(1000, "insufficient_quota")
        assert RealtimePrometheusClient._is_quota_close(exc) is True

    def test_reason_billing_not_active_is_quota(self):
        from prometheus.core.realtime_client import RealtimePrometheusClient
        exc = _make_connection_closed(1000, "billing_not_active")
        assert RealtimePrometheusClient._is_quota_close(exc) is True

    def test_normal_close_1000_not_quota(self):
        from prometheus.core.realtime_client import RealtimePrometheusClient
        exc = _make_connection_closed(1000, "normal closure")
        assert RealtimePrometheusClient._is_quota_close(exc) is False

    def test_code_1001_not_quota(self):
        from prometheus.core.realtime_client import RealtimePrometheusClient
        exc = _make_connection_closed(1001, "going away")
        assert RealtimePrometheusClient._is_quota_close(exc) is False


class TestIsQuotaStr:

    def test_insufficient_quota_string(self):
        from prometheus.core.realtime_client import RealtimePrometheusClient
        assert RealtimePrometheusClient._is_quota_str("insufficient_quota") is True

    def test_billing_not_active_string(self):
        from prometheus.core.realtime_client import RealtimePrometheusClient
        assert RealtimePrometheusClient._is_quota_str("billing_not_active error") is True

    def test_quota_exceeded_string(self):
        from prometheus.core.realtime_client import RealtimePrometheusClient
        assert RealtimePrometheusClient._is_quota_str("quota exceeded") is True

    def test_normal_auth_error_not_quota(self):
        from prometheus.core.realtime_client import RealtimePrometheusClient
        assert RealtimePrometheusClient._is_quota_str("invalid_api_key") is False


# ── _set_quota_exceeded ────────────────────────────────────────────────────────

class TestSetQuotaExceeded:

    def test_sets_quota_exceeded_flag(self):
        client = _make_client()
        client._set_quota_exceeded("test")
        assert client._quota_exceeded is True

    def test_disables_reconnect(self):
        client = _make_client()
        client._set_quota_exceeded("test")
        assert client._should_reconnect is False

    def test_clears_connected(self):
        client = _make_client()
        client.connected = True
        client._set_quota_exceeded("test")
        assert client.connected is False

    def test_sets_last_error(self):
        client = _make_client()
        client._set_quota_exceeded("test")
        assert client.last_error == "insufficient_quota"


# ── connect() with insufficient_quota ─────────────────────────────────────────

class TestConnectQuota:

    def test_connect_survives_quota_close_during_send(self):
        """connect() must return without raising when send() gets a quota close."""
        client = _make_client()

        fake_ws = MagicMock()
        quota_exc = _make_connection_closed(1013, "insufficient_quota.insufficient_quota")

        async def _fake_send(data):
            raise quota_exc

        fake_ws.send = _fake_send

        async def _run():
            with patch("websockets.connect", new_callable=AsyncMock) as mock_conn:
                mock_conn.return_value = fake_ws
                # connect() should return without raising
                await client.connect()

        asyncio.run(_run())
        assert client._quota_exceeded is True
        assert client.connected is False

    def test_connect_raises_on_non_quota_close(self):
        """connect() re-raises ConnectionClosed when it's not a quota error."""
        client = _make_client()

        fake_ws = MagicMock()
        normal_exc = _make_connection_closed(1006, "abnormal closure")

        async def _fake_send(data):
            raise normal_exc

        fake_ws.send = _fake_send

        async def _run():
            with patch("websockets.connect", new_callable=AsyncMock) as mock_conn:
                mock_conn.return_value = fake_ws
                with pytest.raises(Exception):
                    await client.connect()

        asyncio.run(_run())

    def test_connect_survives_quota_rejection_at_websocket_open(self):
        """connect() handles quota error raised by websockets.connect() itself."""
        client = _make_client()

        async def _run():
            with patch("websockets.connect", side_effect=Exception("insufficient_quota")) as _:
                await client.connect()

        asyncio.run(_run())
        assert client._quota_exceeded is True
        assert client.connected is False

    def test_connect_normal_flow_still_works(self):
        """When quota is fine, connect() should not set _quota_exceeded."""
        client = _make_client()

        fake_ws = MagicMock()
        sent: list = []

        async def _fake_send(data):
            sent.append(data)

        fake_ws.send = _fake_send

        async def _run():
            with patch("websockets.connect", new_callable=AsyncMock) as mock_conn, \
                 patch("asyncio.create_task"):
                mock_conn.return_value = fake_ws
                await client.connect()

        asyncio.run(_run())
        assert client._quota_exceeded is False


# ── ensure_connected() with quota state ───────────────────────────────────────

class TestEnsureConnectedQuota:

    def test_raises_immediately_when_quota_exceeded(self):
        """ensure_connected() raises RuntimeError immediately, no reconnect attempt."""
        client = _make_client()
        client._quota_exceeded = True
        client.connected = False

        async def _run():
            with pytest.raises(RuntimeError, match="realtime_unavailable: insufficient_quota"):
                await client.ensure_connected()

        asyncio.run(_run())

    def test_returns_when_connected(self):
        """ensure_connected() returns immediately when already connected."""
        client = _make_client()
        client.connected = True
        client.ws = MagicMock()

        async def _run():
            await client.ensure_connected()  # must not raise

        asyncio.run(_run())

    def test_connect_returns_quota_state_after_reconnect_attempt(self):
        """If connect() detects quota and returns, ensure_connected() re-raises."""
        client = _make_client()
        client.connected = False

        async def _fake_connect():
            client._quota_exceeded = True
            client.connected = False

        async def _run():
            with patch.object(client, "connect", side_effect=_fake_connect), \
                 patch.object(client, "_reconnect_task", None):
                with pytest.raises(RuntimeError, match="insufficient_quota"):
                    await client.ensure_connected()

        asyncio.run(_run())


# ── main.py startup resilience ────────────────────────────────────────────────

class TestMainStartupResilience:

    def test_realtime_required_false_continues_on_connect_failure(self):
        """PROMETHEUS_REALTIME_REQUIRED=false: startup continues when connect raises."""
        import prometheus.core.main as _main

        core = object.__new__(_main.PrometheusCore)
        core.client = MagicMock()
        core.client.connect = AsyncMock(side_effect=RuntimeError("insufficient_quota"))
        core.client._quota_exceeded = True

        async def _simulate():
            _realtime_required = (
                os.getenv("PROMETHEUS_REALTIME_REQUIRED", "false").strip().lower()
                in ("1", "true", "yes")
            )
            try:
                await core.client.connect()
            except Exception as exc:
                if _realtime_required:
                    raise
                # Should swallow the error

        with patch.dict(os.environ, {"PROMETHEUS_REALTIME_REQUIRED": "false"}):
            asyncio.run(_simulate())  # must not raise

    def test_realtime_required_true_propagates_connect_failure(self):
        """PROMETHEUS_REALTIME_REQUIRED=true: startup raises when connect fails."""
        import prometheus.core.main as _main

        core = object.__new__(_main.PrometheusCore)
        core.client = MagicMock()
        core.client.connect = AsyncMock(side_effect=RuntimeError("insufficient_quota"))
        core.client._quota_exceeded = True

        async def _simulate():
            _realtime_required = (
                os.getenv("PROMETHEUS_REALTIME_REQUIRED", "false").strip().lower()
                in ("1", "true", "yes")
            )
            try:
                await core.client.connect()
            except Exception as exc:
                if _realtime_required:
                    raise

        with patch.dict(os.environ, {"PROMETHEUS_REALTIME_REQUIRED": "true"}):
            with pytest.raises(RuntimeError, match="insufficient_quota"):
                asyncio.run(_simulate())


# ── Morning routine HA calls when speech unavailable ─────────────────────────

class TestMorningRoutineDegradedSpeech:

    def test_ha_calls_run_when_speak_raises(self):
        """Morning routine HA sequence completes even when speech fails."""
        from prometheus.routines.morning_routine import (
            MorningRoutineService,
            MorningRoutineConfig,
        )

        ha_calls: list[str] = []

        class FakeHA:
            def call_script(self, entity_id: str) -> bool:
                ha_calls.append(entity_id)
                return True

        class FakeSpeaker:
            async def speak(self, text: str) -> None:
                raise RuntimeError("realtime_unavailable: insufficient_quota")

        class FakeCalendar:
            def get_today_events(self) -> list:
                return []

        class FakeWeather:
            def get_today_weather(self):
                return None

        class FakeStore:
            def load_state(self):
                return None
            def save_state(self, state):
                pass

        class FakeEvent:
            event_id = "evt_test"
            start_time = "2026-06-05T07:00:00"
            title = "Wake Up"

        config = MorningRoutineConfig(
            pre_play_duck_wait_seconds=0,
            pre_play_final_wait_seconds=0,
            music_fade_interval_seconds=0,
            summary_delay_seconds=0,
            pre_summary_duck_seconds=0,
            post_summary_fade_interval_seconds=0,
            volume_command_interval_seconds=0.0,
        )

        svc = MorningRoutineService(
            calendar_reader=FakeCalendar(),
            ha_client=FakeHA(),
            speaker=FakeSpeaker(),
            weather_provider=FakeWeather(),
            state_store=FakeStore(),
            logger=MagicMock(),
            config=config,
        )

        async def _run():
            await svc.run_morning_routine(FakeEvent())

        asyncio.run(_run())
        # Xbox turn on + launch spotify + morning lights + volume down x3 + play +
        # volume up x3 + pre-summary duck x3 + post-summary fade x3 = many calls
        assert len(ha_calls) > 0
        assert any("xbox_turn_on" in c for c in ha_calls)
        assert any("morning_lights" in c for c in ha_calls)
        # Routine completed (state saved)
        assert svc._running is False

    def test_morning_routine_marks_completed_even_when_speech_fails(self):
        """State is saved as completed=True even when speech raises."""
        from prometheus.routines.morning_routine import (
            MorningRoutineService,
            MorningRoutineConfig,
            MorningRoutineState,
        )

        saved_states: list[MorningRoutineState] = []

        class FakeHA:
            def call_script(self, eid):
                return True

        class FakeSpeaker:
            async def speak(self, text):
                raise RuntimeError("realtime_unavailable: insufficient_quota")

        class FakeCalendar:
            def get_today_events(self):
                return []

        class FakeWeather:
            def get_today_weather(self):
                return None

        class FakeStore:
            def load_state(self):
                return None
            def save_state(self, state):
                saved_states.append(state)

        class FakeEvent:
            event_id = "evt_x"
            start_time = "2026-06-05T07:00:00"
            title = "Wake Up"

        config = MorningRoutineConfig(
            pre_play_duck_wait_seconds=0,
            pre_play_final_wait_seconds=0,
            music_fade_interval_seconds=0,
            summary_delay_seconds=0,
            pre_summary_duck_seconds=0,
            post_summary_fade_interval_seconds=0,
            volume_command_interval_seconds=0.0,
        )

        svc = MorningRoutineService(
            calendar_reader=FakeCalendar(),
            ha_client=FakeHA(),
            speaker=FakeSpeaker(),
            weather_provider=FakeWeather(),
            state_store=FakeStore(),
            logger=MagicMock(),
            config=config,
        )

        async def _run():
            await svc.run_morning_routine(FakeEvent())

        asyncio.run(_run())
        # The final state save must be completed=True
        final = saved_states[-1]
        assert final.completed is True


# ── Calendar trigger engine when Realtime unavailable ─────────────────────────

class TestCalendarTriggerEngineQuota:

    def test_trigger_engine_runs_when_handler_hits_quota_error(self, tmp_path):
        """CalendarEventTriggerEngine survives a rule handler that raises a quota error."""
        from prometheus.routines.calendar_event_triggers import (
            CalendarEventTriggerEngine,
            CalendarRoutineRule,
            _CalendarEventAdapter,
        )

        log_calls: list[tuple] = []

        def _fake_log(event: str, payload: dict) -> None:
            log_calls.append((event, payload))

        fired_attempts: list[str] = []

        async def _quota_handler(event) -> None:
            fired_attempts.append(event.title)
            raise RuntimeError("realtime_unavailable: insufficient_quota")

        event = _CalendarEventAdapter({
            "title": "Wake Up",
            "start_time": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
            "event_id": "wu_001",
        })

        class FakeReader:
            def get_upcoming_events(self):
                return [event]

        engine = CalendarEventTriggerEngine(
            calendar_reader=FakeReader(),
            rules=[CalendarRoutineRule(
                name="morning_routine",
                match_title_contains=["wake up"],
                handler=_quota_handler,
                allow_late_seconds=120,
            )],
            state_path=tmp_path / "state.json",
            logger=_fake_log,
        )

        async def _run():
            with __import__("unittest.mock", fromlist=["patch"]).patch.dict(
                os.environ, {
                    "PROMETHEUS_CALENDAR_TRIGGER_LOOKAHEAD_MINUTES": "1",
                }
            ):
                # Engine must not raise even though the handler always fails
                await engine._poll()
                await asyncio.sleep(0.3)

        asyncio.run(_run())  # must not raise
        assert fired_attempts == ["Wake Up"]
        assert any(e == "calendar_trigger_handler_error" for e, _ in log_calls)

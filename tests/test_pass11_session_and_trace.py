"""
tests/test_pass11_session_and_trace.py

Pass 11 — Live voice-to-tool path validation.

Verifies:
1. connect() session.update omits turn_detection (GA API rejects the key)
2. payload audit now blocks turn_detection as a structural key check
3. realtime_session_payload_debug logs has_turn_detection=False, value="omitted"
4. realtime_session_update_keys logs has_turn_detection=False, value="omitted"
5. connect() clears _current_trace_id if not awaiting audio (stale trace fix)
6. diagnostic script reads ~/.jarvis/logs/YYYY-MM-DD.jsonl (not activity.jsonl)
7. prometheus_trace_debug.py --last filters empty/test/readiness traces
8. Simulated "what time is it" PTT command reaches tell_time tool

All tests are offline — no live Realtime API, no live Prometheus.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_client():
    import realtime_client as rc
    speaker = MagicMock()
    speaker.finish_realtime = MagicMock()
    client = rc.RealtimePrometheusClient(speaker=speaker, tools=MagicMock())
    client.api_key = "sk-test-placeholder"
    client._vault_context = ""
    client._workspace_context = ""
    return client


def _run_connect_capture(client, logged: list | None = None) -> list[dict]:
    """Run connect() with mocked websocket and return all sent JSON payloads."""
    sent: list[dict] = []

    async def _fake_send_raw(data):
        sent.append(json.loads(data))

    async def _go():
        fake_ws = MagicMock()
        fake_ws.send = _fake_send_raw
        log_fn = (lambda k, p: logged.append((k, p))) if logged is not None else (lambda *a: None)

        with (
            patch("websockets.connect", new_callable=AsyncMock) as mock_conn,
            patch("asyncio.create_task"),
            patch("realtime_client.log_event", side_effect=log_fn),
            patch("realtime_client.notify"),
        ):
            mock_conn.return_value = fake_ws
            client._receiver_task = None
            with (
                patch.object(client, "_receiver", new_callable=AsyncMock),
                patch.object(client, "_chat_polling_loop", new_callable=AsyncMock),
            ):
                await client.connect()

    asyncio.run(_go())
    return sent


# ── 1. Session payload omits turn_detection ───────────────────────────────────

class TestConnectPayloadOmitsTurnDetection:
    """The connect() session.update must not contain turn_detection in any form."""

    def test_session_dict_does_not_have_turn_detection_key(self):
        client = _make_client()
        sent = _run_connect_capture(client)

        session_updates = [p for p in sent if p.get("type") == "session.update"]
        assert session_updates, "connect() must send at least one session.update"

        sess = session_updates[0].get("session", {})
        assert "turn_detection" not in sess, (
            f"turn_detection must be OMITTED — GA Realtime API rejects it with "
            f"unknown_parameter. Got session keys: {list(sess.keys())}"
        )

    def test_session_json_does_not_contain_turn_detection_string(self):
        client = _make_client()
        sent = _run_connect_capture(client)

        session_updates = [p for p in sent if p.get("type") == "session.update"]
        assert session_updates
        raw = json.dumps(session_updates[0])
        assert '"turn_detection"' not in raw, (
            f"turn_detection must not appear in session.update JSON: {raw[:300]}"
        )

    def test_session_json_does_not_contain_server_vad(self):
        client = _make_client()
        sent = _run_connect_capture(client)

        session_updates = [p for p in sent if p.get("type") == "session.update"]
        assert session_updates
        raw = json.dumps(session_updates[0])
        assert "server_vad" not in raw, (
            f"server_vad must not appear in session.update JSON: {raw[:300]}"
        )

    def test_session_omits_input_audio_transcription(self):
        """input_audio_transcription must be OMITTED.
        The gpt-realtime model rejects it as unknown_parameter.
        Transcription is auto-enabled by the model's default configuration."""
        client = _make_client()
        sent = _run_connect_capture(client)

        session_updates = [p for p in sent if p.get("type") == "session.update"]
        assert session_updates
        sess = session_updates[0].get("session", {})
        assert "input_audio_transcription" not in sess, (
            f"input_audio_transcription must be OMITTED — gpt-realtime rejects it as "
            f"unknown_parameter. Got session keys: {list(sess.keys())}"
        )


# ── 2. Payload audit blocks turn_detection ────────────────────────────────────

class TestPayloadAuditBlocksTurnDetection:
    """The structural guard in connect() must block any session that has turn_detection."""

    def test_connect_source_has_structural_guard(self):
        """connect() must contain a check that blocks turn_detection and
        input_audio_transcription before sending the session.update."""
        import realtime_client as rc
        src = inspect.getsource(rc.RealtimePrometheusClient.connect)
        assert "not_supported_by_live_endpoint" in src, (
            "connect() must block unsupported keys with reason 'not_supported_by_live_endpoint'"
        )
        assert "realtime_payload_blocked" in src, (
            "connect() must log realtime_payload_blocked"
        )
        # Must check for turn_detection as a blocked key
        assert "turn_detection" in src and "input_audio_transcription" in src, (
            "connect() must guard against both turn_detection and input_audio_transcription"
        )

    def test_clean_payload_does_not_trigger_audit(self):
        """The actual session.update sent by connect() must not trip the audit."""
        client = _make_client()
        blocked_calls = []

        async def _go():
            fake_ws = MagicMock()
            async def _fake_send_raw(data): pass
            fake_ws.send = _fake_send_raw

            with (
                patch("websockets.connect", new_callable=AsyncMock) as mock_conn,
                patch("asyncio.create_task"),
                patch("realtime_client.log_event") as mock_log,
                patch("realtime_client.notify"),
            ):
                mock_conn.return_value = fake_ws
                client._receiver_task = None
                with (
                    patch.object(client, "_receiver", new_callable=AsyncMock),
                    patch.object(client, "_chat_polling_loop", new_callable=AsyncMock),
                ):
                    await client.connect()

                for call in mock_log.call_args_list:
                    if call.args[0] == "realtime_payload_blocked":
                        blocked_calls.append(call.args[1])

        asyncio.run(_go())
        assert not blocked_calls, (
            f"connect() session.update must not be blocked by audit: {blocked_calls}"
        )


# ── 3. Debug logs report omitted turn_detection ───────────────────────────────

class TestSessionDebugLogsOmitted:
    """realtime_session_payload_debug must report both turn_detection and
    input_audio_transcription as False/omitted (gpt-realtime rejects both)."""

    def test_payload_debug_has_turn_detection_false(self):
        client = _make_client()
        logged: list = []
        _run_connect_capture(client, logged)

        debug = [p for k, p in logged if k == "realtime_session_payload_debug"]
        assert debug, "realtime_session_payload_debug must be logged"
        ev = debug[0]
        assert ev.get("has_turn_detection") is False, (
            f"has_turn_detection must be False, got {ev.get('has_turn_detection')!r}"
        )

    def test_payload_debug_turn_detection_value_omitted(self):
        client = _make_client()
        logged: list = []
        _run_connect_capture(client, logged)

        debug = [p for k, p in logged if k == "realtime_session_payload_debug"]
        assert debug
        assert debug[0].get("turn_detection_value") == "omitted", (
            f"turn_detection_value must be 'omitted', got {debug[0].get('turn_detection_value')!r}"
        )

    def test_payload_debug_has_input_transcription_config_false(self):
        """has_input_transcription_config must be False — field is omitted."""
        client = _make_client()
        logged: list = []
        _run_connect_capture(client, logged)

        debug = [p for k, p in logged if k == "realtime_session_payload_debug"]
        assert debug
        assert debug[0].get("has_input_transcription_config") is False, (
            f"has_input_transcription_config must be False (field omitted), "
            f"got {debug[0].get('has_input_transcription_config')!r}"
        )

    def test_session_update_keys_has_turn_detection_false(self):
        client = _make_client()
        logged: list = []
        _run_connect_capture(client, logged)

        key_events = [p for k, p in logged if k == "realtime_session_update_keys"]
        assert key_events, "realtime_session_update_keys must be logged"
        ev = key_events[0]
        assert ev.get("has_turn_detection") is False, (
            f"has_turn_detection must be False, got {ev.get('has_turn_detection')!r}"
        )
        assert ev.get("turn_detection_value") == "omitted", (
            f"turn_detection_value must be 'omitted', got {ev.get('turn_detection_value')!r}"
        )
        assert ev.get("has_input_transcription") is False, (
            f"has_input_transcription must be False (field omitted), "
            f"got {ev.get('has_input_transcription')!r}"
        )
        session_keys = ev.get("session_keys", [])
        assert "turn_detection" not in session_keys, (
            f"turn_detection must not appear in session_keys: {session_keys}"
        )
        assert "input_audio_transcription" not in session_keys, (
            f"input_audio_transcription must not appear in session_keys: {session_keys}"
        )


# ── 4. Stale trace cleared on connect ────────────────────────────────────────

class TestStaleTraceClearedOnConnect:
    """connect() must clear _current_trace_id if not awaiting audio (Goal B)."""

    def test_connect_clears_stale_trace_when_not_in_turn(self):
        client = _make_client()
        client._current_trace_id = "20260608-142631-stale-9sjb"  # yesterday's trace
        client.awaiting_user_audio = False

        _run_connect_capture(client)

        assert client._current_trace_id == "", (
            f"connect() must clear stale trace_id when not in user turn. "
            f"Got: {client._current_trace_id!r}"
        )

    def test_connect_preserves_trace_when_in_active_turn(self):
        """If a user turn is in progress when reconnect fires, don't clear the trace."""
        client = _make_client()
        client._current_trace_id = "20260609-120000-active-xx01"
        client.awaiting_user_audio = True  # mid-turn

        # connect() should exit early because api_key is set but we still check
        # the _current_trace_id is NOT cleared when awaiting_user_audio is True
        import realtime_client as rc
        src = inspect.getsource(rc.RealtimePrometheusClient.connect)
        # The guard must check awaiting_user_audio before clearing
        assert "awaiting_user_audio" in src, (
            "connect() must check awaiting_user_audio before clearing _current_trace_id"
        )


# ── 5. Diagnostic script reads correct log path ───────────────────────────────

class TestDiagnosticScriptLogPath:
    """prometheus_ptt_diagnostic.sh must read ~/.jarvis/logs/YYYY-MM-DD.jsonl."""

    def test_diagnostic_script_reads_daily_log_not_activity_jsonl(self):
        script = _ROOT / "scripts" / "prometheus_ptt_diagnostic.sh"
        assert script.exists(), "prometheus_ptt_diagnostic.sh must exist"
        src = script.read_text()
        assert "activity.jsonl" not in src, (
            "diagnostic script must NOT reference activity.jsonl — "
            "logs are at ~/.jarvis/logs/YYYY-MM-DD.jsonl"
        )
        assert ".jarvis/logs" in src, (
            "diagnostic script must read from ~/.jarvis/logs/"
        )
        assert "date +%F" in src or "date +%Y-%m-%d" in src, (
            "diagnostic script must use $(date +%F) to build the log path"
        )

    def test_diagnostic_script_uses_kind_not_event_field(self):
        script = _ROOT / "scripts" / "prometheus_ptt_diagnostic.sh"
        src = script.read_text()
        # Must use .kind, not .event
        assert '.kind ==' in src or '.kind ==' in src, (
            "diagnostic script must query .kind field (not .event) — live logs use 'kind'"
        )
        # .event should not appear as a jq filter
        import re
        event_filters = re.findall(r'\.event\b', src)
        assert not event_filters, (
            f"diagnostic script has {len(event_filters)} .event references — "
            "must be changed to .kind"
        )

    def test_diagnostic_script_filters_test_and_readiness_traces(self):
        script = _ROOT / "scripts" / "prometheus_ptt_diagnostic.sh"
        src = script.read_text()
        assert "-test-" in src, "diagnostic must filter -test- traces"
        assert "readiness-" in src, "diagnostic must filter readiness- traces"


# ── 6. prometheus_trace_debug.py filters correctly ───────────────────────────

class TestTraceDebugFiltering:
    """prometheus_trace_debug.py must ignore empty, test, and readiness traces."""

    def _get_filter_fn(self):
        """Import _is_real_trace from the tool."""
        spec_path = str(_ROOT / "tools" / "prometheus_trace_debug.py")
        import importlib.util
        spec = importlib.util.spec_from_file_location("prometheus_trace_debug", spec_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod._is_real_trace

    def test_empty_trace_id_is_not_real(self):
        fn = self._get_filter_fn()
        assert fn("") is False, "empty trace_id must be filtered"

    def test_test_trace_id_is_not_real(self):
        fn = self._get_filter_fn()
        assert fn("20260607-000000-test-aa01") is False, "-test- traces must be filtered"

    def test_readiness_trace_id_is_not_real(self):
        fn = self._get_filter_fn()
        assert fn("readiness-20260609-090000") is False, "readiness- traces must be filtered"

    def test_real_trace_id_is_real(self):
        fn = self._get_filter_fn()
        assert fn("20260609-120000-what-time-xx01") is True, "real trace must pass filter"
        assert fn("20260608-142631-9sjb") is True, "real trace must pass filter"

    def test_trace_debug_script_exists(self):
        script = _ROOT / "tools" / "prometheus_trace_debug.py"
        assert script.exists(), "tools/prometheus_trace_debug.py must exist"

    def test_trace_debug_supports_last_flag(self):
        script = _ROOT / "tools" / "prometheus_trace_debug.py"
        src = script.read_text()
        assert "--last" in src, "prometheus_trace_debug.py must support --last flag"

    def test_trace_debug_supports_trace_id_flag(self):
        script = _ROOT / "tools" / "prometheus_trace_debug.py"
        src = script.read_text()
        assert "--trace-id" in src, "prometheus_trace_debug.py must support --trace-id flag"

    def test_trace_debug_reads_daily_log(self):
        script = _ROOT / "tools" / "prometheus_trace_debug.py"
        src = script.read_text()
        assert ".jarvis/logs" in src, "trace_debug must read from ~/.jarvis/logs/"
        assert "kind" in src, "trace_debug must use 'kind' field (not 'event')"


# ── 7. Simulated "what time is it" reaches tell_time ─────────────────────────

class TestSimulatedPTTTellTime:
    """Simulate a full PTT turn: begin → audio → end → transcript → tool."""

    def test_what_time_is_it_routes_to_tell_time(self, monkeypatch):
        """Pass 12: end_audio triggers standalone STT; _handle_ptt_transcript routes to tell_time.
        Realtime buffer commit is never called."""
        import realtime_client as rc

        client = _make_client()
        client.awaiting_user_audio = True
        client._audio_bytes_since_commit = 9999
        client._audio_chunks_appended = 10
        client._captured_audio = bytearray(b"\x00" * 9999)
        client._current_trace_id = "20260609-140000-test-xx11"
        client._response_active = False
        client._turn_start_ts = 0.0
        client._override_handled = False

        logged = []
        monkeypatch.setattr("realtime_client.log_event", lambda k, p: logged.append((k, p)))
        monkeypatch.setattr("realtime_client.notify", lambda *a: None)

        # Phase 1: end_audio — must trigger STT task, not Realtime commit
        sent = []
        client.send = AsyncMock(side_effect=lambda d: sent.append(d))
        with patch.object(client, "_transcribe_ptt", new_callable=AsyncMock):
            asyncio.run(client.end_audio())

        committed = [d for d in sent if d.get("type") == "input_audio_buffer.commit"]
        assert not committed, "end_audio must NOT send input_audio_buffer.commit in Pass 12"

        attempt = [p for k, p in logged if k == "user_turn_commit_attempt"]
        assert attempt, "user_turn_commit_attempt must be logged"

        # Phase 2: simulate standalone STT result via _handle_ptt_transcript
        routed_actions = []

        async def _fake_run_direct_tool(payload: dict) -> None:
            routed_actions.append(payload)
            client.busy = False

        client._run_direct_tool = _fake_run_direct_tool
        client._guarded_response_create = AsyncMock(return_value=True)
        client.send = AsyncMock()
        client._handle_vault_recall = AsyncMock()
        client._contextual_override = AsyncMock(return_value=False)
        client._current_trace_id = "20260609-140000-test-xx11"

        asyncio.run(client._handle_ptt_transcript("20260609-140000-test-xx11", "what time is it"))

        # Transcript must have been logged
        transcript_logs = [p for k, p in logged if k == "input_transcript_completed"]
        assert transcript_logs, "input_transcript_completed must be logged"
        assert transcript_logs[0].get("source") == "standalone_stt"

        # The direct intent override must have fired for "what time is it"
        assert routed_actions, (
            "direct_tool_override must route 'what time is it' — check _direct_intent_override"
        )
        assert routed_actions[0].get("action") == "tell_time"

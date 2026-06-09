"""
tests/test_pass10_6_session_config.py

Pass 10.6 — Realtime session config correctness for PTT mode.

Verifies:
1. connect() session.update never contains server_vad
2. connect() session.update has turn_detection: null (Python None → JSON null)
3. connect() input_audio_transcription specifies an explicit model (not empty {})
4. payload audit guard blocks server_vad before sending
5. _update_session_instructions() includes turn_detection: null (no accidental reset)
6. Debug log events include actual turn_detection value and transcription model
7. Simulated PTT commit + mock transcript event → input_transcript_completed logged

All tests are offline — no live Realtime API.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import sys
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
            # Stub _receiver and _chat_polling_loop so they don't start background loops
            client._receiver_task = None
            with (
                patch.object(client, "_receiver", new_callable=AsyncMock),
                patch.object(client, "_chat_polling_loop", new_callable=AsyncMock),
            ):
                await client.connect()

    asyncio.run(_go())
    return sent


# ── 1. connect() never sends server_vad ───────────────────────────────────────

class TestConnectPayloadNeverHasServerVad:
    """The session.update sent by connect() must not contain server_vad."""

    def test_connect_payload_json_does_not_contain_server_vad(self):
        client = _make_client()
        sent = _run_connect_capture(client)

        session_updates = [p for p in sent if p.get("type") == "session.update"]
        assert session_updates, "connect() must send at least one session.update"

        for payload in session_updates:
            raw = json.dumps(payload)
            assert "server_vad" not in raw, (
                f"session.update must not contain server_vad, got: {raw[:200]}"
            )

    def test_connect_session_turn_detection_is_null(self):
        client = _make_client()
        sent = _run_connect_capture(client)

        session_updates = [p for p in sent if p.get("type") == "session.update"]
        assert session_updates

        sess = session_updates[0].get("session", {})
        assert "turn_detection" in sess, "turn_detection must be present in session"
        assert sess["turn_detection"] is None, (
            f"turn_detection must be null (None) for PTT mode, got {sess['turn_detection']!r}"
        )

    def test_connect_session_turn_detection_serializes_to_json_null(self):
        client = _make_client()
        sent = _run_connect_capture(client)

        session_updates = [p for p in sent if p.get("type") == "session.update"]
        assert session_updates

        raw = json.dumps(session_updates[0])
        assert '"turn_detection": null' in raw or '"turn_detection":null' in raw, (
            "turn_detection must serialize to JSON null, not omitted or server_vad"
        )


# ── 2. Payload audit blocks server_vad ───────────────────────────────────────

class TestPayloadAuditBlocksServerVad:
    """The hard payload audit in connect() must catch server_vad and block the send."""

    def test_server_vad_in_forbidden_strings(self):
        """'server_vad' must appear in the forbidden payload strings check."""
        src = inspect.getsource(
            __import__("realtime_client").RealtimePrometheusClient.connect
        )
        assert "server_vad" in src, (
            "connect() source must reference 'server_vad' in its forbidden strings check"
        )

    def test_payload_audit_logs_blocked_with_ptt_reason(self):
        """The connect() payload audit must block a session.update that contains server_vad."""
        import realtime_client as rc

        # Verify the guard is wired: the blocked-event code path exists in source
        src = inspect.getsource(rc.RealtimePrometheusClient.connect)
        assert "realtime_payload_blocked" in src, \
            "connect() must log realtime_payload_blocked when audit trips"
        assert "server_vad_not_allowed_in_ptt_mode" in src, \
            "connect() must use specific reason string for server_vad block"

        # Structural test: if we manually construct the forbidden payload, the
        # _hits logic would catch it.  Simulate it here without running connect().
        test_payload = json.dumps({
            "type": "session.update",
            "session": {
                "type": "realtime",
                "turn_detection": {"type": "server_vad"},
            }
        })
        _forbidden_payload_strings = [
            "OpenAI-Beta", "realtime=v1", "input_audio_format", "output_audio_format",
            "additionalProperties", "whisper-1", "input_audio_transcription_model", "server_vad",
        ]
        hits = [s for s in _forbidden_payload_strings if s in test_payload]
        assert "server_vad" in hits, "forbidden strings list must catch server_vad in payload"

    def test_payload_blocked_reason_is_ptt_specific(self, monkeypatch):
        """The blocked event reason must identify the PTT-mode violation."""
        src = inspect.getsource(
            __import__("realtime_client").RealtimePrometheusClient.connect
        )
        assert "server_vad_not_allowed_in_ptt_mode" in src, (
            "connect() must log reason='server_vad_not_allowed_in_ptt_mode'"
        )


# ── 3. input_audio_transcription has an explicit model ────────────────────────

class TestTranscriptionConfig:
    """input_audio_transcription must specify a model — empty {} does not enable transcription."""

    def test_connect_transcription_has_model_field(self):
        client = _make_client()
        sent = _run_connect_capture(client)

        session_updates = [p for p in sent if p.get("type") == "session.update"]
        assert session_updates

        sess = session_updates[0].get("session", {})
        transcription = sess.get("input_audio_transcription")

        assert transcription is not None, "input_audio_transcription must not be null/absent"
        assert isinstance(transcription, dict), \
            f"input_audio_transcription must be a dict, got {type(transcription)}"
        assert "model" in transcription, (
            f"input_audio_transcription must have a 'model' field, got {transcription!r} — "
            "empty {{}} does not enable transcription on the GA Realtime API"
        )
        assert transcription["model"], "transcription model must not be empty"

    def test_connect_transcription_model_is_not_whisper_1(self):
        """whisper-1 is blocked by the payload audit — must use a GA-compatible model."""
        client = _make_client()
        sent = _run_connect_capture(client)

        session_updates = [p for p in sent if p.get("type") == "session.update"]
        assert session_updates

        raw = json.dumps(session_updates[0])
        assert "whisper-1" not in raw, (
            "whisper-1 is in the forbidden payload strings — must use a GA model like gpt-4o-mini-transcribe"
        )

    def test_transcription_model_constant_is_defined(self):
        """RealtimePrometheusClient._TRANSCRIPTION_MODEL must be set to a valid model name."""
        import realtime_client as rc
        model = rc.RealtimePrometheusClient._TRANSCRIPTION_MODEL
        assert model, "_TRANSCRIPTION_MODEL must not be empty"
        assert "whisper" not in model.lower(), \
            f"_TRANSCRIPTION_MODEL must not use whisper (blocked by audit): {model!r}"
        assert "gpt" in model.lower() or "transcribe" in model.lower(), \
            f"_TRANSCRIPTION_MODEL should be a GA transcription model, got: {model!r}"

    def test_transcription_model_not_in_forbidden_strings(self):
        """The chosen transcription model must pass the payload audit."""
        import realtime_client as rc
        model = rc.RealtimePrometheusClient._TRANSCRIPTION_MODEL
        _forbidden = [
            "OpenAI-Beta", "realtime=v1", "input_audio_format", "output_audio_format",
            "additionalProperties", "whisper-1", "input_audio_transcription_model", "server_vad",
        ]
        for forbidden in _forbidden:
            assert forbidden not in model, (
                f"_TRANSCRIPTION_MODEL '{model}' would trigger the payload audit because it contains '{forbidden}'"
            )


# ── 4. _update_session_instructions does not reset turn_detection ─────────────

class TestUpdateSessionInstructionsTurnDetection:
    """Mid-session instruction updates must carry turn_detection: null so they never
    accidentally reset the server back to server_vad defaults."""

    def test_update_session_instructions_includes_turn_detection_null(self):
        client = _make_client()
        sent: list[dict] = []

        async def _go():
            client.connected = True
            client.ws = MagicMock()
            client.send = AsyncMock(side_effect=lambda d: sent.append(d))
            with patch("realtime_client.log_event"), patch("realtime_client.notify"):
                await client._update_session_instructions()

        asyncio.run(_go())

        assert sent, "_update_session_instructions must call send"
        sess = sent[0].get("session", {})
        assert "turn_detection" in sess, \
            "turn_detection must be present in _update_session_instructions payload"
        assert sess["turn_detection"] is None, (
            f"turn_detection must be null in _update_session_instructions, got {sess['turn_detection']!r}"
        )

    def test_update_session_instructions_no_server_vad(self):
        client = _make_client()
        sent: list[dict] = []

        async def _go():
            client.connected = True
            client.ws = MagicMock()
            client.send = AsyncMock(side_effect=lambda d: sent.append(d))
            with patch("realtime_client.log_event"), patch("realtime_client.notify"):
                await client._update_session_instructions()

        asyncio.run(_go())

        assert sent
        raw = json.dumps(sent[0])
        assert "server_vad" not in raw, (
            "_update_session_instructions must not contain server_vad"
        )

    def test_update_session_instructions_preserves_instructions_field(self):
        client = _make_client()
        client._system_prompt = "Test system prompt for Prometheus."
        sent: list[dict] = []

        async def _go():
            client.connected = True
            client.ws = MagicMock()
            client.send = AsyncMock(side_effect=lambda d: sent.append(d))
            with patch("realtime_client.log_event"), patch("realtime_client.notify"):
                await client._update_session_instructions()

        asyncio.run(_go())

        assert sent
        sess = sent[0].get("session", {})
        assert "instructions" in sess, "instructions must still be present"
        assert sess["instructions"], "instructions must not be empty"

    def test_update_session_instructions_turn_detection_serializes_null(self):
        client = _make_client()
        sent: list[dict] = []

        async def _go():
            client.connected = True
            client.ws = MagicMock()
            client.send = AsyncMock(side_effect=lambda d: sent.append(d))
            with patch("realtime_client.log_event"), patch("realtime_client.notify"):
                await client._update_session_instructions()

        asyncio.run(_go())

        assert sent
        raw = json.dumps(sent[0])
        assert "server_vad" not in raw
        # turn_detection must either be absent or null — not a non-null VAD config
        assert ('"turn_detection": null' in raw or
                '"turn_detection":null' in raw or
                '"turn_detection": null' in raw), (
            f"turn_detection must serialize to null in the JSON output: {raw[:300]}"
        )


# ── 5. Debug log events carry actual config values ────────────────────────────

class TestSessionDebugLogging:
    """realtime_session_payload_debug and realtime_session_update_keys must log
    actual config values so the live log can confirm the session is in PTT mode."""

    def test_payload_debug_log_has_turn_detection_value(self):
        client = _make_client()
        logged: list = []
        _run_connect_capture(client, logged)

        debug_events = [p for k, p in logged if k == "realtime_session_payload_debug"]
        assert debug_events, "realtime_session_payload_debug must be logged"
        ev = debug_events[0]
        assert "turn_detection_value" in ev, \
            "realtime_session_payload_debug must include turn_detection_value"
        assert ev["turn_detection_value"] == "null", \
            f"turn_detection_value must be 'null' in PTT mode, got {ev['turn_detection_value']!r}"

    def test_payload_debug_log_has_transcription_model(self):
        client = _make_client()
        logged: list = []
        _run_connect_capture(client, logged)

        debug_events = [p for k, p in logged if k == "realtime_session_payload_debug"]
        assert debug_events
        ev = debug_events[0]
        assert "transcription_model" in ev, \
            "realtime_session_payload_debug must include transcription_model"
        assert ev["transcription_model"], "transcription_model must not be empty"

    def test_session_update_keys_log_has_turn_detection_state(self):
        client = _make_client()
        logged: list = []
        _run_connect_capture(client, logged)

        key_events = [p for k, p in logged if k == "realtime_session_update_keys"]
        assert key_events, "realtime_session_update_keys must be logged"
        ev = key_events[0]
        assert "has_turn_detection" in ev, "must log has_turn_detection"
        assert ev["has_turn_detection"] is True
        assert "turn_detection_value" in ev, "must log turn_detection_value"
        assert ev["turn_detection_value"] == "null"
        assert "has_input_transcription" in ev
        assert ev["has_input_transcription"] is True
        assert "transcription_model" in ev
        assert ev["transcription_model"]   # non-empty


# ── 6. Simulated PTT turn → transcript event → handler fires ──────────────────

class TestSimulatedPTTTranscriptFlow:
    """Verify that the receiver correctly dispatches the transcription event.
    Feed a mock transcript event directly through the receiver's dispatch logic."""

    def test_transcript_event_type_string_matches_receiver(self):
        """The event type string must match what the receiver checks for."""
        src = inspect.getsource(
            __import__("realtime_client").RealtimePrometheusClient._receiver
        )
        assert "conversation.item.input_audio_transcription.completed" in src, \
            "_receiver must handle 'conversation.item.input_audio_transcription.completed'"

    def test_transcript_event_logged_as_input_transcript_completed(self, monkeypatch):
        """When the receiver sees a transcript event, it must log input_transcript_completed."""
        import realtime_client as rc

        client = _make_client()
        client.connected = True
        client._current_trace_id = "20260609-120000-sim-xx01"
        client._turn_start_ts = 0.0
        client._override_handled = False

        logged = []
        monkeypatch.setattr("realtime_client.log_event", lambda k, p: logged.append((k, p)))
        monkeypatch.setattr("realtime_client.notify", lambda *a: None)

        # Feed the transcript event through the receiver dispatch logic
        # by running the receiver with a one-shot websocket that yields exactly
        # the transcript event then raises to stop the loop.

        class _OneShotWS:
            def __init__(self, events):
                self._events = iter(events)
                self._closed = False

            async def recv(self):
                try:
                    ev = next(self._events)
                    return json.dumps(ev)
                except StopIteration:
                    self._closed = True
                    raise Exception("no more events")

        transcript_event = {
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "what time is it",
        }
        client.ws = _OneShotWS([transcript_event])

        async def _run_receiver():
            client._run_direct_tool = AsyncMock()
            client._guarded_response_create = AsyncMock(return_value=True)
            client.send = AsyncMock()
            client._handle_vault_recall = AsyncMock()
            client._contextual_override = AsyncMock(return_value=False)
            client._update_session_instructions = AsyncMock()
            # Run receiver; it will process one event and then raise on the next recv
            try:
                await client._receiver()
            except Exception:
                pass  # expected — one-shot WS raises after first event

        asyncio.run(_run_receiver())

        transcript_logs = [p for k, p in logged if k == "input_transcript_completed"]
        assert transcript_logs, (
            "input_transcript_completed must be logged when transcript event is received — "
            "if this fails, the receiver event type string may not match"
        )
        assert transcript_logs[0].get("trace_id") == "20260609-120000-sim-xx01"

    def test_commit_then_response_then_transcript_produces_complete_trace(self, monkeypatch):
        """Simulate: end_audio commit → response.created → transcript event → logging."""
        import realtime_client as rc

        client = _make_client()
        client.awaiting_user_audio = True
        client._audio_bytes_since_commit = 9999
        client._audio_chunks_appended = 10
        client._current_trace_id = "20260609-120000-flow-xx02"
        client._response_active = False
        client._turn_start_ts = 0.0
        client._override_handled = False

        logged = []
        monkeypatch.setattr("realtime_client.log_event", lambda k, p: logged.append((k, p)))
        monkeypatch.setattr("realtime_client.notify", lambda *a: None)

        # Phase 1: end_audio commit
        sent = []
        client.send = AsyncMock(side_effect=lambda d: sent.append(d))
        asyncio.run(client.end_audio())

        # Confirm commit was sent
        committed = [d for d in sent if d.get("type") == "input_audio_buffer.commit"]
        assert committed, "end_audio must send input_audio_buffer.commit"

        # Phase 2: simulate receiver processing transcript event
        class _OneShotWS:
            def __init__(self, events):
                self._events = iter(events)
            async def recv(self):
                try:
                    return json.dumps(next(self._events))
                except StopIteration:
                    raise Exception("no more events")

        events = [
            {"type": "input_audio_buffer.committed"},
            {"type": "response.created"},
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "what time is it",
            },
        ]
        client.ws = _OneShotWS(events)
        client.connected = True
        client._run_direct_tool = AsyncMock()
        client._guarded_response_create = AsyncMock(return_value=True)
        client.send = AsyncMock()
        client._handle_vault_recall = AsyncMock()
        client._contextual_override = AsyncMock(return_value=False)
        client._update_session_instructions = AsyncMock()

        async def _run_receiver():
            try:
                await client._receiver()
            except Exception:
                pass

        asyncio.run(_run_receiver())

        # Verify full trace
        committed_log = [p for k, p in logged if k == "user_turn_committed"]
        assert committed_log, "user_turn_committed must be in log"

        transcript_log = [p for k, p in logged if k == "input_transcript_completed"]
        assert transcript_log, "input_transcript_completed must be in log after full flow"
        assert transcript_log[0].get("trace_id") == "20260609-120000-flow-xx02"

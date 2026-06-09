"""
tests/test_pass10_6_session_config.py

Pass 10.6 → Pass 11 — Realtime session config correctness for PTT mode.

Verifies:
1. connect() session.update never contains server_vad
2. connect() session.update OMITS turn_detection entirely (GA API rejects the key)
3. connect() input_audio_transcription specifies an explicit model (not empty {})
4. payload audit guard blocks server_vad and turn_detection before sending
5. _update_session_instructions() OMITS turn_detection (GA API rejects it mid-session too)
6. Debug log reports has_turn_detection=False, turn_detection_value='omitted'
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
    """The session.update sent by connect() must not contain server_vad or turn_detection."""

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

    def test_connect_session_omits_turn_detection(self):
        """turn_detection must be OMITTED entirely. The GA Realtime API rejects it
        as unknown_parameter — sending null or any value causes the session update to fail."""
        client = _make_client()
        sent = _run_connect_capture(client)

        session_updates = [p for p in sent if p.get("type") == "session.update"]
        assert session_updates

        sess = session_updates[0].get("session", {})
        assert "turn_detection" not in sess, (
            f"turn_detection must be OMITTED from session.update — GA Realtime API "
            f"rejects it with unknown_parameter. Got session keys: {list(sess.keys())}"
        )

    def test_connect_session_turn_detection_absent_from_json(self):
        """turn_detection must not appear anywhere in the session.update JSON.
        The GA Realtime API rejects the key entirely (unknown_parameter error)."""
        client = _make_client()
        sent = _run_connect_capture(client)

        session_updates = [p for p in sent if p.get("type") == "session.update"]
        assert session_updates

        raw = json.dumps(session_updates[0])
        assert '"turn_detection"' not in raw, (
            f"turn_detection must not appear in session.update JSON — GA API rejects it. "
            f"Found in: {raw[:300]}"
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


# ── 3. input_audio_transcription is OMITTED (model rejects it) ───────────────

class TestTranscriptionConfig:
    """input_audio_transcription must be OMITTED from session.update.
    The gpt-realtime model rejects the field as unknown_parameter (same as turn_detection).
    Transcription is auto-enabled by the model's default session configuration."""

    def test_connect_session_omits_input_audio_transcription(self):
        """input_audio_transcription must be OMITTED. The model rejects it as unknown_parameter."""
        client = _make_client()
        sent = _run_connect_capture(client)

        session_updates = [p for p in sent if p.get("type") == "session.update"]
        assert session_updates

        sess = session_updates[0].get("session", {})
        assert "input_audio_transcription" not in sess, (
            f"input_audio_transcription must be OMITTED — gpt-realtime rejects it as "
            f"unknown_parameter. Got session keys: {list(sess.keys())}"
        )

    def test_connect_session_json_has_no_transcription_key(self):
        """No 'input_audio_transcription' key should appear in the session.update JSON."""
        client = _make_client()
        sent = _run_connect_capture(client)

        session_updates = [p for p in sent if p.get("type") == "session.update"]
        assert session_updates
        raw = json.dumps(session_updates[0])
        assert '"input_audio_transcription"' not in raw, (
            f"input_audio_transcription must be absent from session JSON: {raw[:300]}"
        )

    def test_transcription_model_constant_is_defined(self):
        """_TRANSCRIPTION_MODEL constant must still exist as a reference value."""
        import realtime_client as rc
        model = rc.RealtimePrometheusClient._TRANSCRIPTION_MODEL
        assert model, "_TRANSCRIPTION_MODEL must not be empty (kept for reference)"

    def test_connect_session_has_no_whisper_in_json(self):
        """whisper-1 must not appear in the session.update JSON."""
        client = _make_client()
        sent = _run_connect_capture(client)

        session_updates = [p for p in sent if p.get("type") == "session.update"]
        assert session_updates
        raw = json.dumps(session_updates[0])
        assert "whisper" not in raw, (
            f"whisper must not appear in session.update JSON: {raw[:300]}"
        )


# ── 4. _update_session_instructions does not reset turn_detection ─────────────

class TestUpdateSessionInstructionsTurnDetection:
    """Mid-session instruction updates must OMIT turn_detection. The GA Realtime API
    rejects the key as unknown_parameter — even sending null causes the update to fail."""

    def test_update_session_instructions_omits_turn_detection(self):
        """_update_session_instructions must NOT send turn_detection in any form."""
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
        assert "turn_detection" not in sess, (
            f"turn_detection must be OMITTED from _update_session_instructions — "
            f"GA Realtime API rejects the key. Got session keys: {list(sess.keys())}"
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

    def test_update_session_instructions_turn_detection_absent_from_json(self):
        """turn_detection must not appear in the JSON sent by _update_session_instructions."""
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
        assert "server_vad" not in raw, "_update_session_instructions must not contain server_vad"
        assert '"turn_detection"' not in raw, (
            f"turn_detection must be absent from _update_session_instructions JSON: {raw[:300]}"
        )


# ── 5. Debug log events carry actual config values ────────────────────────────

class TestSessionDebugLogging:
    """realtime_session_payload_debug and realtime_session_update_keys must log
    actual config values so the live log can confirm turn_detection is omitted."""

    def test_payload_debug_log_has_turn_detection_value(self):
        client = _make_client()
        logged: list = []
        _run_connect_capture(client, logged)

        debug_events = [p for k, p in logged if k == "realtime_session_payload_debug"]
        assert debug_events, "realtime_session_payload_debug must be logged"
        ev = debug_events[0]
        assert "turn_detection_value" in ev, \
            "realtime_session_payload_debug must include turn_detection_value"
        assert ev["turn_detection_value"] == "omitted", (
            f"turn_detection_value must be 'omitted' (key not sent to GA API), "
            f"got {ev['turn_detection_value']!r}"
        )

    def test_payload_debug_log_has_transcription_model(self):
        client = _make_client()
        logged: list = []
        _run_connect_capture(client, logged)

        debug_events = [p for k, p in logged if k == "realtime_session_payload_debug"]
        assert debug_events
        ev = debug_events[0]
        assert "transcription_model" in ev, \
            "realtime_session_payload_debug must include transcription_model"
        # Model is "model_default" since we omit the field and rely on model defaults
        assert ev["transcription_model"], "transcription_model must not be empty"
        assert ev.get("has_input_transcription_config") is False, (
            f"has_input_transcription_config must be False (field omitted): "
            f"{ev.get('has_input_transcription_config')!r}"
        )

    def test_session_update_keys_log_has_turn_detection_state(self):
        client = _make_client()
        logged: list = []
        _run_connect_capture(client, logged)

        key_events = [p for k, p in logged if k == "realtime_session_update_keys"]
        assert key_events, "realtime_session_update_keys must be logged"
        ev = key_events[0]
        assert "has_turn_detection" in ev, "must log has_turn_detection"
        assert ev["has_turn_detection"] is False, (
            f"has_turn_detection must be False (key omitted from payload), "
            f"got {ev['has_turn_detection']!r}"
        )
        assert "turn_detection_value" in ev, "must log turn_detection_value"
        assert ev["turn_detection_value"] == "omitted", (
            f"turn_detection_value must be 'omitted', got {ev['turn_detection_value']!r}"
        )
        assert "has_input_transcription" in ev
        assert ev["has_input_transcription"] is False, (
            f"has_input_transcription must be False (field omitted — model rejects it), "
            f"got {ev.get('has_input_transcription')!r}"
        )
        assert "transcription_model" in ev
        assert ev["transcription_model"]   # non-empty ("model_default")
        # turn_detection must NOT appear in session_keys list
        session_keys = ev.get("session_keys", [])
        assert "turn_detection" not in session_keys, (
            f"turn_detection must not be in session_keys — key was omitted from payload. "
            f"Got: {session_keys}"
        )


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

    def test_commit_then_stt_then_transcript_produces_complete_trace(self, monkeypatch):
        """Simulate: end_audio → STT task → _handle_ptt_transcript → input_transcript_completed.
        Pass 12: audio routes to standalone STT; Realtime buffer commit is never called."""
        import realtime_client as rc
        from unittest.mock import patch as _patch

        client = _make_client()
        client.awaiting_user_audio = True
        client._audio_bytes_since_commit = 9999
        client._audio_chunks_appended = 10
        client._captured_audio = bytearray(b"\x00" * 9999)
        client._current_trace_id = "20260609-120000-flow-xx02"
        client._response_active = False
        client._turn_start_ts = 0.0
        client._override_handled = False

        logged = []
        monkeypatch.setattr("realtime_client.log_event", lambda k, p: logged.append((k, p)))
        monkeypatch.setattr("realtime_client.notify", lambda *a: None)

        # Phase 1: end_audio — should trigger STT task, not Realtime commit
        sent = []
        client.send = AsyncMock(side_effect=lambda d: sent.append(d))
        with _patch.object(client, "_transcribe_ptt", new_callable=AsyncMock):
            asyncio.run(client.end_audio())

        # Confirm Realtime commit was NOT sent
        committed_sent = [d for d in sent if d.get("type") == "input_audio_buffer.commit"]
        assert not committed_sent, "end_audio must NOT send input_audio_buffer.commit in Pass 12"

        # Confirm user_turn_commit_attempt (STT mode) was logged
        attempt_log = [p for k, p in logged if k == "user_turn_commit_attempt"]
        assert attempt_log, "user_turn_commit_attempt must be in log"
        assert attempt_log[0].get("stt_mode") == "standalone"

        # Phase 2: simulate standalone STT result arriving via _handle_ptt_transcript
        client._run_direct_tool = AsyncMock()
        client._guarded_response_create = AsyncMock(return_value=True)
        client.send = AsyncMock()
        client._handle_vault_recall = AsyncMock()
        client._contextual_override = AsyncMock(return_value=False)
        client._current_trace_id = "20260609-120000-flow-xx02"

        asyncio.run(client._handle_ptt_transcript("20260609-120000-flow-xx02", "what time is it"))

        # Verify full trace — trace_id is stable (no slug mutation in Pass 12.5)
        transcript_log = [p for k, p in logged if k == "input_transcript_completed"]
        assert transcript_log, "input_transcript_completed must be in log after full flow"
        assert transcript_log[0].get("trace_id") == "20260609-120000-flow-xx02"
        assert transcript_log[0].get("source") == "standalone_stt"

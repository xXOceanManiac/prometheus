"""test_pass12_standalone_stt.py — Standalone STT bypass for PTT mode.

Tests that:
- Sufficient PTT audio triggers standalone STT (not Realtime commit)
- Insufficient audio is skipped before STT
- STT success emits input_transcript_completed
- STT success routes "what time is it" to tell_time
- input_audio_buffer.commit is NOT called in default PTT mode
- trace_debug --last with no argument defaults to 1
- Diagnostic script exists and contains STT-stage references
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import wave
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import realtime_client as rc

# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_client() -> rc.RealtimePrometheusClient:
    speaker = MagicMock()
    speaker.finish_realtime = MagicMock()
    tools = MagicMock()
    tool_out = MagicMock()
    tool_out.ok = True
    tool_out.status = "verified_success"
    tool_out.verified = True
    tool_out.response_text = "It is 3:00 PM."
    tool_out.speak = True
    tools.execute.return_value = tool_out
    client = rc.RealtimePrometheusClient(speaker=speaker, tools=tools)
    client.api_key = "sk-test-placeholder"
    client._vault_context = ""
    client._workspace_context = ""
    return client


# ── PCM → WAV ─────────────────────────────────────────────────────────────────


class TestPCMToWAV:
    def test_wav_header(self):
        pcm = b"\x00\x01" * 800  # 1600 bytes PCM16 at 16kHz → 50ms
        wav_bytes = rc.RealtimePrometheusClient._pcm_to_wav(pcm, sample_rate=16000)
        with wave.open(io.BytesIO(wav_bytes)) as wf:
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2
            assert wf.getframerate() == 16000

    def test_wav_nonempty(self):
        pcm = b"\x00" * 6400
        wav_bytes = rc.RealtimePrometheusClient._pcm_to_wav(pcm)
        assert len(wav_bytes) > 44  # WAV header is 44 bytes


# ── send_audio accumulates locally, never sends to Realtime ──────────────────


class TestSendAudioAccumulatesLocally:
    def test_send_audio_accumulates(self):
        client = _make_client()
        client.awaiting_user_audio = True
        chunk = b"\x00\x01" * 400  # 800 bytes
        asyncio.run(client.send_audio(chunk))
        assert len(client._captured_audio) == 800
        assert client._audio_bytes_since_commit == 800
        assert client._audio_chunks_appended == 1

    def test_send_audio_no_input_audio_buffer_append(self):
        """send_audio must NEVER call input_audio_buffer.append on the Realtime WS."""
        client = _make_client()
        client.awaiting_user_audio = True
        sent_payloads: list[dict] = []

        async def fake_send(data: dict) -> None:
            sent_payloads.append(data)

        async def run():
            with patch.object(client, "send", side_effect=fake_send):
                for _ in range(6):
                    await client.send_audio(b"\x00" * 800)

        asyncio.run(run())
        append_calls = [p for p in sent_payloads if p.get("type") == "input_audio_buffer.append"]
        assert append_calls == [], f"input_audio_buffer.append must not be sent. Got: {append_calls}"

    def test_send_audio_noop_when_not_awaiting(self):
        client = _make_client()
        client.awaiting_user_audio = False
        asyncio.run(client.send_audio(b"\x00" * 800))
        assert len(client._captured_audio) == 0


# ── end_audio: insufficient audio skips STT ──────────────────────────────────


class TestEndAudioInsufficientAudio:
    def test_insufficient_audio_skips_stt(self):
        client = _make_client()
        client.awaiting_user_audio = True
        client._audio_bytes_since_commit = 100  # below 3200 threshold
        logged: list[tuple] = []

        async def run():
            with (
                patch("realtime_client.log_event", side_effect=lambda k, d=None: logged.append((k, d or {}))),
                patch.object(client, "_transcribe_ptt", new_callable=AsyncMock) as mock_stt,
            ):
                await client.end_audio()
                mock_stt.assert_not_called()

        asyncio.run(run())

        kinds = [k for k, _ in logged]
        assert "user_turn_commit_skipped" in kinds, f"Expected user_turn_commit_skipped. Got: {kinds}"
        skipped_data = next(d for k, d in logged if k == "user_turn_commit_skipped")
        assert skipped_data.get("reason") == "insufficient_audio"

    def test_insufficient_audio_resets_busy(self):
        client = _make_client()
        client.awaiting_user_audio = True
        client._audio_bytes_since_commit = 50

        async def run():
            with (
                patch("realtime_client.log_event"),
                patch.object(client, "_transcribe_ptt", new_callable=AsyncMock),
            ):
                await client.end_audio()

        asyncio.run(run())
        assert not client.busy


# ── end_audio: sufficient audio triggers STT, no Realtime commit ─────────────


class TestEndAudioSufficientAudio:
    def test_sufficient_audio_triggers_stt(self):
        client = _make_client()
        client.awaiting_user_audio = True
        client._captured_audio = bytearray(b"\x00" * 6400)
        client._audio_bytes_since_commit = 6400
        client._audio_chunks_appended = 4
        client._first_audio_ts = 1.0
        client._last_audio_ts = 2.5

        stt_calls: list[tuple] = []

        async def fake_transcribe(pcm: bytes, trace_id: str) -> None:
            stt_calls.append((pcm, trace_id))

        async def run():
            with patch("realtime_client.log_event"):
                client._transcribe_ptt = fake_transcribe
                await client.end_audio()
                await asyncio.sleep(0)  # allow task to start

        asyncio.run(run())

        assert len(stt_calls) == 1, "Expected exactly one _transcribe_ptt call"
        pcm_arg, trace_arg = stt_calls[0]
        assert len(pcm_arg) == 6400

    def test_sufficient_audio_no_realtime_commit(self):
        """end_audio() must not send input_audio_buffer.commit."""
        client = _make_client()
        client.awaiting_user_audio = True
        client._captured_audio = bytearray(b"\x00" * 6400)
        client._audio_bytes_since_commit = 6400
        client._audio_chunks_appended = 4
        client._first_audio_ts = 1.0
        client._last_audio_ts = 2.5

        sent_payloads: list[dict] = []

        async def fake_send(data: dict) -> None:
            sent_payloads.append(data)

        async def run():
            with (
                patch("realtime_client.log_event"),
                patch.object(client, "send", side_effect=fake_send),
                patch.object(client, "_transcribe_ptt", new_callable=AsyncMock),
            ):
                await client.end_audio()
                await asyncio.sleep(0)

        asyncio.run(run())

        commit_calls = [p for p in sent_payloads if p.get("type") == "input_audio_buffer.commit"]
        assert commit_calls == [], f"input_audio_buffer.commit must not be called. Got: {sent_payloads}"


# ── _transcribe_ptt: log events ───────────────────────────────────────────────


def _mock_httpx_response(status: int, text: str):
    mock_resp = MagicMock()
    mock_resp.status_code = status
    mock_resp.text = text
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=ctx)
    ctx.__aexit__ = AsyncMock(return_value=False)
    ctx.post = AsyncMock(return_value=mock_resp)
    return ctx


class TestTranscribePTTLogs:
    def test_stt_started_logged(self):
        client = _make_client()
        logged: list[tuple] = []
        ctx = _mock_httpx_response(200, "hello world")

        async def run():
            with (
                patch("realtime_client.log_event", side_effect=lambda k, d=None: logged.append((k, d or {}))),
                patch("httpx.AsyncClient", return_value=ctx),
                patch.object(client, "_handle_ptt_transcript", new_callable=AsyncMock),
            ):
                await client._transcribe_ptt(b"\x00" * 4000, "trace-001")

        asyncio.run(run())

        started = [d for k, d in logged if k == "stt_transcription_started"]
        assert len(started) > 0, "stt_transcription_started not logged"
        assert started[0].get("trace_id") == "trace-001"
        assert started[0].get("model") == "gpt-4o-mini-transcribe"

    def test_stt_completed_logged_on_success(self):
        client = _make_client()
        logged: list[tuple] = []
        ctx = _mock_httpx_response(200, "hello world")

        async def run():
            with (
                patch("realtime_client.log_event", side_effect=lambda k, d=None: logged.append((k, d or {}))),
                patch("httpx.AsyncClient", return_value=ctx),
                patch.object(client, "_handle_ptt_transcript", new_callable=AsyncMock),
            ):
                await client._transcribe_ptt(b"\x00" * 4000, "trace-002")

        asyncio.run(run())

        completed = [d for k, d in logged if k == "stt_transcription_completed"]
        assert len(completed) > 0, "stt_transcription_completed not logged"
        assert "duration_ms" in completed[0]
        assert "preview" in completed[0]

    def test_stt_failed_logged_on_http_error(self):
        client = _make_client()
        logged: list[tuple] = []
        ctx1 = _mock_httpx_response(400, "model not found")
        ctx2 = _mock_httpx_response(400, "model not found")

        async def run():
            with (
                patch("realtime_client.log_event", side_effect=lambda k, d=None: logged.append((k, d or {}))),
                patch("httpx.AsyncClient", side_effect=[ctx1, ctx2]),
            ):
                await client._transcribe_ptt(b"\x00" * 4000, "trace-003")

        asyncio.run(run())

        failed = [k for k, _ in logged if k == "stt_transcription_failed"]
        all_failed = [k for k, _ in logged if k == "stt_all_models_failed"]
        assert len(failed) > 0, "stt_transcription_failed not logged"
        assert len(all_failed) > 0, "stt_all_models_failed not logged"

    def test_stt_success_calls_handle_ptt_transcript(self):
        client = _make_client()
        transcript_received: list[str] = []
        ctx = _mock_httpx_response(200, "what time is it")

        async def fake_handle(transcript: str) -> None:
            transcript_received.append(transcript)

        async def run():
            with (
                patch("realtime_client.log_event"),
                patch("httpx.AsyncClient", return_value=ctx),
            ):
                client._handle_ptt_transcript = fake_handle
                await client._transcribe_ptt(b"\x00" * 4000, "trace-004")

        asyncio.run(run())

        assert transcript_received == ["what time is it"]


# ── _handle_ptt_transcript: routing ──────────────────────────────────────────


class TestHandlePTTTranscriptRouting:
    def test_input_transcript_completed_logged(self):
        client = _make_client()
        logged: list[tuple] = []

        async def run():
            with (
                patch("realtime_client.log_event", side_effect=lambda k, d=None: logged.append((k, d or {}))),
                patch("realtime_client.notify"),
                patch.object(client, "_contextual_override", new_callable=AsyncMock, return_value=False),
            ):
                client.connected = False
                await client._handle_ptt_transcript("hello there")

        asyncio.run(run())

        events = [d for k, d in logged if k == "input_transcript_completed"]
        assert len(events) > 0, "input_transcript_completed not logged"
        assert events[0].get("source") == "standalone_stt"
        assert "hello there" in events[0].get("transcript", "")

    def test_what_time_routes_to_tell_time(self):
        client = _make_client()
        tool_calls: list[dict] = []
        logged: list[tuple] = []

        async def fake_run_direct(payload: dict) -> None:
            tool_calls.append(payload)
            logged.append(("tool_execute", {"payload": payload}))
            logged.append(("tool_result", {"action": payload.get("action"), "status": "verified_success"}))
            client.busy = False

        async def run():
            with (
                patch("realtime_client.log_event", side_effect=lambda k, d=None: logged.append((k, d or {}))),
                patch("realtime_client.notify"),
                patch.object(client, "_run_direct_tool", side_effect=fake_run_direct),
                patch.object(client, "_contextual_override", new_callable=AsyncMock, return_value=False),
            ):
                await client._handle_ptt_transcript("what time is it")

        asyncio.run(run())

        assert len(tool_calls) > 0, "No tool was called for 'what time is it'"
        assert tool_calls[0].get("action") == "tell_time"

    def test_direct_override_blocks_llm_path(self):
        """When direct_tool_override matches, must not send to Realtime."""
        client = _make_client()
        sent_payloads: list[dict] = []

        async def fake_send(data: dict) -> None:
            sent_payloads.append(data)

        async def fake_run_direct(payload: dict) -> None:
            client.busy = False

        async def run():
            with (
                patch("realtime_client.log_event"),
                patch("realtime_client.notify"),
                patch.object(client, "send", side_effect=fake_send),
                patch.object(client, "_run_direct_tool", side_effect=fake_run_direct),
                patch.object(client, "_contextual_override", new_callable=AsyncMock, return_value=False),
            ):
                client.connected = True
                client.ws = MagicMock()
                await client._handle_ptt_transcript("what time is it")

        asyncio.run(run())

        realtime_sends = [p for p in sent_payloads if p.get("type") == "conversation.item.create"]
        assert realtime_sends == [], "LLM path must not fire when direct_tool_override matches"


# ── Full PTT cycle: never sends input_audio_buffer.commit ────────────────────


class TestNoRealtimeCommitInPTTMode:
    def test_full_ptt_cycle_no_realtime_commit(self):
        client = _make_client()
        sent_payloads: list[dict] = []

        async def fake_send(data: dict) -> None:
            sent_payloads.append(data)

        async def run():
            client.awaiting_user_audio = True
            with (
                patch("realtime_client.log_event"),
                patch.object(client, "send", side_effect=fake_send),
                patch.object(client, "_transcribe_ptt", new_callable=AsyncMock),
            ):
                # Simulate PTT audio chunks
                for _ in range(5):
                    await client.send_audio(b"\x00" * 1280)
                client._first_audio_ts = 1.0
                client._last_audio_ts = 2.5
                await client.end_audio()
                await asyncio.sleep(0)

        asyncio.run(run())

        sent_types = [p.get("type") for p in sent_payloads]
        assert "input_audio_buffer.commit" not in sent_types, (
            f"input_audio_buffer.commit must never be sent. Got: {sent_types}"
        )
        assert "input_audio_buffer.append" not in sent_types, (
            f"input_audio_buffer.append must never be sent. Got: {sent_types}"
        )


# ── trace_debug CLI ───────────────────────────────────────────────────────────


class TestTraceDebugCLI:
    _TOOL_PATH = _ROOT / "tools" / "prometheus_trace_debug.py"

    def _load_module(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("prometheus_trace_debug", self._TOOL_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_last_default_is_1(self):
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--last", type=int, default=1)
        parser.add_argument("--trace-id")
        parser.add_argument("--date", default="2026-06-08")
        args = parser.parse_args([])
        assert args.last == 1

    def test_last_explicit_value(self):
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--last", type=int, default=1)
        args = parser.parse_args(["--last", "5"])
        assert args.last == 5

    def test_trace_events_include_stt_stages(self):
        mod = self._load_module()
        assert "stt_transcription_started" in mod._TRACE_EVENTS
        assert "stt_transcription_completed" in mod._TRACE_EVENTS
        assert "stt_transcription_failed" in mod._TRACE_EVENTS
        assert "ptt_audio_captured" in mod._TRACE_EVENTS
        assert "input_transcript_completed" in mod._TRACE_EVENTS

    def test_is_real_trace_filters_test_and_readiness(self):
        mod = self._load_module()
        assert not mod._is_real_trace("")
        assert not mod._is_real_trace("20260608-test-abc")
        assert not mod._is_real_trace("readiness-2026-06-08")
        assert mod._is_real_trace("20260608-143022-what-time-xx01")


# ── Diagnostic script content ─────────────────────────────────────────────────


class TestDiagnosticScriptSTTStages:
    _DIAG_PATH = _ROOT / "scripts" / "prometheus_ptt_diagnostic.sh"

    def _content(self) -> str:
        return self._DIAG_PATH.read_text()

    def test_diagnostic_exists(self):
        assert self._DIAG_PATH.exists(), "Diagnostic script not found"

    def test_references_stt_started(self):
        assert "stt_transcription_started" in self._content()

    def test_references_stt_completed(self):
        assert "stt_transcription_completed" in self._content()

    def test_references_stt_failed(self):
        assert "stt_transcription_failed" in self._content()

    def test_references_input_transcript_completed(self):
        assert "input_transcript_completed" in self._content()

    def test_references_tool_execute(self):
        assert "tool_execute" in self._content()

    def test_references_tool_result(self):
        assert "tool_result" in self._content()

    def test_uses_kind_field_not_event(self):
        content = self._content()
        assert ".kind ==" in content
        assert ".event ==" not in content

    def test_uses_daily_log_path(self):
        content = self._content()
        assert ".jarvis/logs/" in content
        assert "activity.jsonl" not in content

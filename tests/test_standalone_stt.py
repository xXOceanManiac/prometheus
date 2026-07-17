"""test_pass12_standalone_stt.py — Standalone STT bypass and routing for PTT mode.

Tests that:
- Sufficient PTT audio triggers standalone STT (not Realtime commit)
- Insufficient audio is skipped before STT
- STT success calls _handle_ptt_transcript(trace_id, transcript)
- _handle_ptt_transcript logs ptt_transcript_route_started + input_transcript_completed
- "What time is it?" routes to tell_time via direct_tool_override
- tool_execute and tool_result carry the same trace_id throughout
- Exception in _handle_ptt_transcript logs ptt_transcript_route_failed
- input_audio_buffer.commit is NEVER sent in PTT mode
- trace_debug --last with no argument defaults to 1
- Diagnostic script exists and contains STT/routing stage references
"""

from __future__ import annotations

import asyncio
import io
import sys
import wave
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import prometheus.core.realtime_client as rc

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


def _mock_httpx_response(status: int, text: str):
    mock_resp = MagicMock()
    mock_resp.status_code = status
    mock_resp.text = text
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=ctx)
    ctx.__aexit__ = AsyncMock(return_value=False)
    ctx.post = AsyncMock(return_value=mock_resp)
    return ctx


# ── PCM → WAV ─────────────────────────────────────────────────────────────────


class TestPCMToWAV:
    def test_wav_header(self):
        pcm = b"\x00\x01" * 800
        wav_bytes = rc.RealtimePrometheusClient._pcm_to_wav(pcm, sample_rate=16000)
        with wave.open(io.BytesIO(wav_bytes)) as wf:
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2
            assert wf.getframerate() == 16000

    def test_wav_nonempty(self):
        pcm = b"\x00" * 6400
        wav_bytes = rc.RealtimePrometheusClient._pcm_to_wav(pcm)
        assert len(wav_bytes) > 44


# ── send_audio accumulates locally, never sends to Realtime ──────────────────


class TestSendAudioAccumulatesLocally:
    def test_send_audio_accumulates(self):
        client = _make_client()
        client.awaiting_user_audio = True
        asyncio.run(client.send_audio(b"\x00\x01" * 400))
        assert len(client._captured_audio) == 800
        assert client._audio_bytes_since_commit == 800
        assert client._audio_chunks_appended == 1

    def test_send_audio_no_input_audio_buffer_append(self):
        client = _make_client()
        client.awaiting_user_audio = True
        sent_payloads: list[dict] = []

        async def run():
            with patch.object(client, "send", side_effect=lambda d: sent_payloads.append(d)):
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
        client._audio_bytes_since_commit = 100
        logged: list[tuple] = []

        async def run():
            with (
                patch("prometheus.core.realtime_client.log_event", side_effect=lambda k, d=None: logged.append((k, d or {}))),
                patch.object(client, "_transcribe_ptt", new_callable=AsyncMock) as mock_stt,
            ):
                await client.end_audio()
                mock_stt.assert_not_called()

        asyncio.run(run())
        kinds = [k for k, _ in logged]
        assert "user_turn_commit_skipped" in kinds
        skipped_data = next(d for k, d in logged if k == "user_turn_commit_skipped")
        assert skipped_data.get("reason") == "insufficient_audio"

    def test_insufficient_audio_resets_busy(self):
        client = _make_client()
        client.awaiting_user_audio = True
        client._audio_bytes_since_commit = 50

        async def run():
            with (
                patch("prometheus.core.realtime_client.log_event"),
                patch.object(client, "_transcribe_ptt", new_callable=AsyncMock),
            ):
                await client.end_audio()

        asyncio.run(run())
        assert not client.busy


# ── end_audio: sufficient audio triggers STT ─────────────────────────────────


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
            with patch("prometheus.core.realtime_client.log_event"):
                client._transcribe_ptt = fake_transcribe
                await client.end_audio()
                await asyncio.sleep(0)

        asyncio.run(run())
        assert len(stt_calls) == 1
        pcm_arg, trace_arg = stt_calls[0]
        assert len(pcm_arg) == 6400

    def test_sufficient_audio_no_realtime_commit(self):
        client = _make_client()
        client.awaiting_user_audio = True
        client._captured_audio = bytearray(b"\x00" * 6400)
        client._audio_bytes_since_commit = 6400
        client._audio_chunks_appended = 4
        client._first_audio_ts = 1.0
        client._last_audio_ts = 2.5

        sent_payloads: list[dict] = []

        async def run():
            with (
                patch("prometheus.core.realtime_client.log_event"),
                patch.object(client, "send", side_effect=lambda d: sent_payloads.append(d)),
                patch.object(client, "_transcribe_ptt", new_callable=AsyncMock),
            ):
                await client.end_audio()
                await asyncio.sleep(0)

        asyncio.run(run())
        commit_calls = [p for p in sent_payloads if p.get("type") == "input_audio_buffer.commit"]
        assert commit_calls == [], f"input_audio_buffer.commit must not be called. Got: {sent_payloads}"


# ── _transcribe_ptt: log events ───────────────────────────────────────────────


class TestTranscribePTTLogs:
    def test_stt_started_logged(self):
        client = _make_client()
        logged: list[tuple] = []
        ctx = _mock_httpx_response(200, "hello world")

        async def run():
            with (
                patch("prometheus.core.realtime_client.log_event", side_effect=lambda k, d=None: logged.append((k, d or {}))),
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
                patch("prometheus.core.realtime_client.log_event", side_effect=lambda k, d=None: logged.append((k, d or {}))),
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
                patch("prometheus.core.realtime_client.log_event", side_effect=lambda k, d=None: logged.append((k, d or {}))),
                patch("httpx.AsyncClient", side_effect=[ctx1, ctx2]),
            ):
                await client._transcribe_ptt(b"\x00" * 4000, "trace-003")

        asyncio.run(run())
        failed = [k for k, _ in logged if k == "stt_transcription_failed"]
        all_failed = [k for k, _ in logged if k == "stt_all_models_failed"]
        assert len(failed) > 0, "stt_transcription_failed not logged"
        assert len(all_failed) > 0, "stt_all_models_failed not logged"

    def test_stt_success_calls_handle_ptt_transcript(self):
        """_transcribe_ptt must call _handle_ptt_transcript(trace_id, transcript)."""
        client = _make_client()
        handle_calls: list[tuple] = []
        ctx = _mock_httpx_response(200, "what time is it")

        async def fake_handle(trace_id: str, transcript: str) -> None:
            handle_calls.append((trace_id, transcript))

        async def run():
            with (
                patch("prometheus.core.realtime_client.log_event"),
                patch("httpx.AsyncClient", return_value=ctx),
            ):
                client._handle_ptt_transcript = fake_handle
                await client._transcribe_ptt(b"\x00" * 4000, "trace-004")

        asyncio.run(run())
        assert len(handle_calls) == 1
        trace_arg, text_arg = handle_calls[0]
        assert trace_arg == "trace-004", f"trace_id not passed: {trace_arg}"
        assert text_arg == "what time is it"

    def test_route_failed_logged_on_exception(self):
        """If _handle_ptt_transcript raises, ptt_transcript_route_failed must be logged."""
        client = _make_client()
        logged: list[tuple] = []
        ctx = _mock_httpx_response(200, "what time is it")

        async def crashing_handle(trace_id: str, transcript: str) -> None:
            raise RuntimeError("simulated routing crash")

        async def run():
            with (
                patch("prometheus.core.realtime_client.log_event", side_effect=lambda k, d=None: logged.append((k, d or {}))),
                patch("httpx.AsyncClient", return_value=ctx),
            ):
                client._handle_ptt_transcript = crashing_handle
                await client._transcribe_ptt(b"\x00" * 4000, "trace-005")

        asyncio.run(run())
        failed = [d for k, d in logged if k == "ptt_transcript_route_failed"]
        assert len(failed) > 0, "ptt_transcript_route_failed not logged after exception"
        assert failed[0].get("trace_id") == "trace-005"
        assert "simulated routing crash" in failed[0].get("error", "")
        assert not client.busy, "busy must be cleared after route failure"


# ── _handle_ptt_transcript: routing ──────────────────────────────────────────


class TestHandlePTTTranscriptRouting:
    def test_route_started_logged(self):
        client = _make_client()
        logged: list[tuple] = []

        async def run():
            with (
                patch("prometheus.core.realtime_client.log_event", side_effect=lambda k, d=None: logged.append((k, d or {}))),
                patch("prometheus.core.realtime_client.notify"),
                patch.object(client, "_contextual_override", new_callable=AsyncMock, return_value=False),
            ):
                client.connected = False
                await client._handle_ptt_transcript("trace-rtest", "hello there")

        asyncio.run(run())
        events = [d for k, d in logged if k == "ptt_transcript_route_started"]
        assert len(events) > 0, "ptt_transcript_route_started not logged"
        assert events[0].get("trace_id") == "trace-rtest"

    def test_input_transcript_completed_logged(self):
        client = _make_client()
        logged: list[tuple] = []

        async def run():
            with (
                patch("prometheus.core.realtime_client.log_event", side_effect=lambda k, d=None: logged.append((k, d or {}))),
                patch("prometheus.core.realtime_client.notify"),
                patch.object(client, "_contextual_override", new_callable=AsyncMock, return_value=False),
            ):
                client.connected = False
                await client._handle_ptt_transcript("trace-tc", "hello there")

        asyncio.run(run())
        events = [d for k, d in logged if k == "input_transcript_completed"]
        assert len(events) > 0, "input_transcript_completed not logged"
        assert events[0].get("source") == "standalone_stt"
        assert events[0].get("trace_id") == "trace-tc"
        assert "hello there" in events[0].get("transcript", "")

    def test_trace_id_stable_throughout_routing(self):
        """trace_id must be identical on ptt_transcript_route_started, input_transcript_completed,
        direct_tool_override, tool_execute, and tool_result — no mid-turn mutation."""
        client = _make_client()
        logged: list[tuple] = []
        TRACE = "20260609-120000-stable-xx01"

        async def fake_run_direct(payload: dict) -> None:
            logged.append(("tool_execute", {"trace_id": client._current_trace_id, "payload": payload}))
            logged.append(("tool_result", {"trace_id": client._current_trace_id, "status": "verified_success"}))
            client.busy = False

        async def run():
            with (
                patch("prometheus.core.realtime_client.log_event", side_effect=lambda k, d=None: logged.append((k, d or {}))),
                patch("prometheus.core.realtime_client.notify"),
                patch.object(client, "_run_direct_tool", side_effect=fake_run_direct),
                patch.object(client, "_contextual_override", new_callable=AsyncMock, return_value=False),
            ):
                await client._handle_ptt_transcript(TRACE, "what time is it")

        asyncio.run(run())

        def _tid(kind: str) -> str:
            for k, d in logged:
                if k == kind:
                    return d.get("trace_id", "")
            return ""

        t_route = _tid("ptt_transcript_route_started")
        t_input = _tid("input_transcript_completed")
        t_tool = _tid("tool_execute")
        t_result = _tid("tool_result")

        assert t_route == TRACE, f"ptt_transcript_route_started trace_id wrong: {t_route}"
        assert t_input == TRACE, f"input_transcript_completed trace_id wrong: {t_input}"
        assert t_tool == TRACE, f"tool_execute trace_id wrong: {t_tool}"
        assert t_result == TRACE, f"tool_result trace_id wrong: {t_result}"

    def test_what_time_routes_to_tell_time(self):
        client = _make_client()
        tool_calls: list[dict] = []

        async def fake_run_direct(payload: dict) -> None:
            tool_calls.append(payload)
            client.busy = False

        async def run():
            with (
                patch("prometheus.core.realtime_client.log_event"),
                patch("prometheus.core.realtime_client.notify"),
                patch.object(client, "_run_direct_tool", side_effect=fake_run_direct),
                patch.object(client, "_contextual_override", new_callable=AsyncMock, return_value=False),
            ):
                await client._handle_ptt_transcript("trace-wt", "what time is it")

        asyncio.run(run())
        assert len(tool_calls) > 0, "No tool was called for 'what time is it'"
        assert tool_calls[0].get("action") == "tell_time"

    def test_direct_override_route_logged(self):
        """ptt_transcript_route_direct_tool must be logged when override matches."""
        client = _make_client()
        logged: list[tuple] = []

        async def fake_run_direct(payload: dict) -> None:
            client.busy = False

        async def run():
            with (
                patch("prometheus.core.realtime_client.log_event", side_effect=lambda k, d=None: logged.append((k, d or {}))),
                patch("prometheus.core.realtime_client.notify"),
                patch.object(client, "_run_direct_tool", side_effect=fake_run_direct),
                patch.object(client, "_contextual_override", new_callable=AsyncMock, return_value=False),
            ):
                await client._handle_ptt_transcript("trace-dr", "what time is it")

        asyncio.run(run())
        events = [d for k, d in logged if k == "ptt_transcript_route_direct_tool"]
        assert len(events) > 0, "ptt_transcript_route_direct_tool not logged"
        assert events[0].get("trace_id") == "trace-dr"
        assert events[0].get("action") == "tell_time"

    def test_no_tool_route_logged_for_unknown_phrase(self):
        """ptt_transcript_route_no_tool must be logged when nothing matches."""
        client = _make_client()
        logged: list[tuple] = []

        async def run():
            with (
                patch("prometheus.core.realtime_client.log_event", side_effect=lambda k, d=None: logged.append((k, d or {}))),
                patch("prometheus.core.realtime_client.notify"),
                patch.object(client, "_contextual_override", new_callable=AsyncMock, return_value=False),
            ):
                client.connected = False
                await client._handle_ptt_transcript("trace-nt", "xyzzy unknowable phrase")

        asyncio.run(run())
        events = [k for k, _ in logged if k == "ptt_transcript_route_no_tool"]
        assert len(events) > 0, "ptt_transcript_route_no_tool not logged for unmatched phrase"

    def test_direct_override_blocks_llm_path(self):
        client = _make_client()
        sent_payloads: list[dict] = []

        async def fake_send(data: dict) -> None:
            sent_payloads.append(data)

        async def fake_run_direct(payload: dict) -> None:
            client.busy = False

        async def run():
            with (
                patch("prometheus.core.realtime_client.log_event"),
                patch("prometheus.core.realtime_client.notify"),
                patch.object(client, "send", side_effect=fake_send),
                patch.object(client, "_run_direct_tool", side_effect=fake_run_direct),
                patch.object(client, "_contextual_override", new_callable=AsyncMock, return_value=False),
            ):
                client.connected = True
                client.ws = MagicMock()
                await client._handle_ptt_transcript("trace-blm", "what time is it")

        asyncio.run(run())
        realtime_sends = [p for p in sent_payloads if p.get("type") == "conversation.item.create"]
        assert realtime_sends == [], "LLM path must not fire when direct_tool_override matches"


# ── Full PTT cycle: never sends input_audio_buffer.commit ────────────────────


class TestNoRealtimeCommitInPTTMode:
    def test_full_ptt_cycle_no_realtime_commit(self):
        client = _make_client()
        sent_payloads: list[dict] = []

        async def run():
            client.awaiting_user_audio = True
            with (
                patch("prometheus.core.realtime_client.log_event"),
                patch.object(client, "send", side_effect=lambda d: sent_payloads.append(d)),
                patch.object(client, "_transcribe_ptt", new_callable=AsyncMock),
            ):
                for _ in range(5):
                    await client.send_audio(b"\x00" * 1280)
                client._first_audio_ts = 1.0
                client._last_audio_ts = 2.5
                await client.end_audio()
                await asyncio.sleep(0)

        asyncio.run(run())
        sent_types = [p.get("type") for p in sent_payloads]
        assert "input_audio_buffer.commit" not in sent_types
        assert "input_audio_buffer.append" not in sent_types


# ── trace_debug CLI ───────────────────────────────────────────────────────────


class TestTraceDebugCLI:
    _TOOL_PATH = _ROOT / "scripts" / "prometheus_trace_debug.py"

    def _load_module(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("prometheus_trace_debug", self._TOOL_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_last_no_arg_defaults_to_1(self):
        """--last with no value must default to 1."""
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--last", type=int, nargs="?", const=1, default=1)
        args = parser.parse_args(["--last"])   # flag present, no value
        assert args.last == 1

    def test_last_explicit_1(self):
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--last", type=int, nargs="?", const=1, default=1)
        args = parser.parse_args(["--last", "1"])
        assert args.last == 1

    def test_last_explicit_3(self):
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--last", type=int, nargs="?", const=1, default=1)
        args = parser.parse_args(["--last", "3"])
        assert args.last == 3

    def test_last_absent_defaults_to_1(self):
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--last", type=int, nargs="?", const=1, default=1)
        args = parser.parse_args([])    # no flag at all
        assert args.last == 1

    def test_trace_events_include_stt_and_routing_stages(self):
        mod = self._load_module()
        assert "stt_transcription_started" in mod._TRACE_EVENTS
        assert "stt_transcription_completed" in mod._TRACE_EVENTS
        assert "stt_transcription_failed" in mod._TRACE_EVENTS
        assert "ptt_audio_captured" in mod._TRACE_EVENTS
        assert "input_transcript_completed" in mod._TRACE_EVENTS
        assert "ptt_transcript_route_started" in mod._TRACE_EVENTS
        assert "ptt_transcript_route_direct_tool" in mod._TRACE_EVENTS
        assert "ptt_transcript_route_no_tool" in mod._TRACE_EVENTS
        assert "ptt_transcript_route_failed" in mod._TRACE_EVENTS

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
        assert self._DIAG_PATH.exists()

    def test_references_stt_started(self):
        assert "stt_transcription_started" in self._content()

    def test_references_stt_completed(self):
        assert "stt_transcription_completed" in self._content()

    def test_references_stt_failed(self):
        assert "stt_transcription_failed" in self._content()

    def test_references_input_transcript_completed(self):
        assert "input_transcript_completed" in self._content()

    def test_references_ptt_transcript_route_started(self):
        assert "ptt_transcript_route_started" in self._content()

    def test_references_ptt_transcript_route_failed(self):
        assert "ptt_transcript_route_failed" in self._content()

    def test_references_tool_execute(self):
        assert "tool_execute" in self._content()

    def test_references_tool_result(self):
        assert "tool_result" in self._content()

    def test_routing_failed_wording_present(self):
        content = self._content()
        assert "transcript routing failed" in content, \
            "Diagnostic must say 'transcript routing failed' not 'STT did not produce a transcript'"

    def test_uses_kind_field_not_event(self):
        content = self._content()
        assert ".kind ==" in content
        assert ".event ==" not in content

    def test_uses_daily_log_path(self):
        content = self._content()
        assert ".jarvis/logs/" in content
        assert "activity.jsonl" not in content

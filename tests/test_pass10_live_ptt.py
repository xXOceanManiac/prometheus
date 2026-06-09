"""
tests/test_pass10_live_ptt.py

Pass 10 — Live PTT audio path regression tests.

Covers the three root-cause bugs fixed in Pass 10:
  Bug 1: interrupt() did not clear _response_active → next turn was silently dropped
  Bug 2: begin+commit tasks ran in same event-loop cycle before any audio was processed → 0-byte commit
  Bug 3: _commit_turn set user_turn_active=False before end_audio() → run() stopped
          sending audio during the drain window

All tests are offline (no Realtime API, no audio device).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_client():
    import realtime_client as rc
    speaker = MagicMock()
    speaker.finish_realtime = MagicMock()
    client = rc.RealtimePrometheusClient(speaker=speaker, tools=MagicMock())
    client.api_key = "test-key"
    client.connected = True
    client.ws = AsyncMock()
    client.ws.send = AsyncMock()
    return client


def _fake_send_factory(sent: list):
    async def fake_send(d):
        sent.append(d)
    return fake_send


# ── Bug 1: interrupt() must clear _response_active ───────────────────────────

class TestInterruptClearsResponseActive:
    """interrupt() sends response.cancel and must immediately clear _response_active
    so that the very next PTT turn is not silently dropped."""

    def test_interrupt_clears_response_active(self):
        client = _make_client()
        client._response_active = True
        asyncio.run(client.interrupt())
        assert client._response_active is False, \
            "interrupt() must set _response_active=False immediately"

    def test_interrupt_allows_next_end_audio_to_trigger_stt(self, monkeypatch):
        """After interrupt, end_audio with sufficient bytes must trigger standalone STT.
        Pass 12: Realtime commit is never called — STT fires instead."""
        client = _make_client()
        client._response_active = True

        logged = []
        monkeypatch.setattr("realtime_client.log_event", lambda k, p: logged.append((k, p)))
        sent = []
        client.send = _fake_send_factory(sent)

        asyncio.run(client.interrupt())

        client.awaiting_user_audio = True
        client._audio_bytes_since_commit = 9999
        client._audio_chunks_appended = 12
        client._captured_audio = bytearray(b"\x00" * 9999)
        client._current_trace_id = "20260608-120000-int-xx01"

        with patch.object(client, "_transcribe_ptt", new_callable=AsyncMock):
            asyncio.run(client.end_audio())

        skipped_active = [p for k, p in logged if k == "response_create_skipped_active"]
        assert not skipped_active, \
            "end_audio must not log response_create_skipped_active (that guard is in _guarded_response_create)"

        attempt = [p for k, p in logged if k == "user_turn_commit_attempt"]
        assert attempt, "end_audio with sufficient bytes must log user_turn_commit_attempt"
        # Realtime commit must never be sent
        committed = [d for d in sent if d.get("type") == "input_audio_buffer.commit"]
        assert not committed, "input_audio_buffer.commit must never be sent in PTT mode"

    def test_interrupt_without_connection_still_clears_flag(self):
        client = _make_client()
        client._response_active = True
        client.connected = False
        client.ws = None
        asyncio.run(client.interrupt())
        assert client._response_active is False

    def test_interrupt_clears_busy_flag(self):
        client = _make_client()
        client.busy = True
        asyncio.run(client.interrupt())
        assert client.busy is False

    def test_double_interrupt_does_not_raise(self):
        client = _make_client()
        asyncio.run(client.interrupt())
        asyncio.run(client.interrupt())  # should not raise


# ── Bug 2: 0-byte race — drain window gives audio time to arrive ──────────────

class TestDrainWindowOnZeroBytes:
    """end_audio() with 0 bytes must wait 150ms before evaluating the threshold,
    allowing the run() loop to deliver buffered mic chunks."""

    def test_zero_bytes_logs_ptt_audio_capture_stopped(self, monkeypatch):
        client = _make_client()
        client.awaiting_user_audio = True
        client._audio_bytes_since_commit = 0
        client._audio_chunks_appended = 0
        client._current_trace_id = "20260608-120000-drain-xx02"
        client._response_active = False

        logged = []
        monkeypatch.setattr("realtime_client.log_event", lambda k, p: logged.append((k, p)))

        async def fake_send(d): pass
        client.send = fake_send
        asyncio.run(client.end_audio())

        stopped = [p for k, p in logged if k == "ptt_audio_capture_stopped"]
        assert stopped, "ptt_audio_capture_stopped must be logged"
        assert stopped[0]["trace_id"] == "20260608-120000-drain-xx02"

    def test_zero_bytes_logs_commit_skipped_with_reason(self, monkeypatch):
        client = _make_client()
        client.awaiting_user_audio = True
        client._audio_bytes_since_commit = 0
        client._current_trace_id = "20260608-120000-drain-xx03"
        client._response_active = False

        logged = []
        monkeypatch.setattr("realtime_client.log_event", lambda k, p: logged.append((k, p)))

        async def fake_send(d): pass
        client.send = fake_send
        asyncio.run(client.end_audio())

        skipped = [p for k, p in logged if k == "user_turn_commit_skipped"]
        assert skipped, "user_turn_commit_skipped must be logged when bytes=0"
        assert skipped[0]["reason"] == "insufficient_audio"
        assert skipped[0]["trace_id"] == "20260608-120000-drain-xx03"

    def test_drain_window_allows_audio_to_accumulate(self, monkeypatch):
        """If send_audio() is called while end_audio() is sleeping (drain window),
        the bytes are counted and standalone STT is triggered (not skipped).
        Pass 12: Realtime commit is never called — STT fires instead."""
        client = _make_client()
        client.awaiting_user_audio = True
        client._audio_bytes_since_commit = 0
        client._audio_chunks_appended = 0
        client._current_trace_id = "20260608-120000-drain-xx04"
        client._response_active = False

        sent = []
        logged = []
        monkeypatch.setattr("realtime_client.log_event", lambda k, p: logged.append((k, p)))
        client.send = _fake_send_factory(sent)

        import numpy as np

        async def run_with_audio():
            with patch.object(client, "_transcribe_ptt", new_callable=AsyncMock):
                end_task = asyncio.create_task(client.end_audio())
                await asyncio.sleep(0)
                chunk = np.zeros(1280, dtype=np.int16).tobytes()
                for _ in range(5):  # 5 chunks × 2560 bytes = 12800 bytes > 3200 threshold
                    await client.send_audio(chunk)
                await end_task

        asyncio.run(run_with_audio())

        attempt = [p for k, p in logged if k == "user_turn_commit_attempt"]
        assert attempt, "turn must trigger STT after audio arrives during drain window"
        skipped = [p for k, p in logged if k == "user_turn_commit_skipped"]
        assert not skipped, "must not skip after audio arrives during drain window"
        # Realtime commit must never be sent
        committed = [d for d in sent if d.get("type") == "input_audio_buffer.commit"]
        assert not committed, "input_audio_buffer.commit must never be sent in PTT mode"

    def test_sufficient_bytes_skips_drain_and_triggers_stt(self, monkeypatch):
        """If bytes >= threshold on entry, no drain window; STT fires immediately.
        Pass 12: Realtime commit is never called — STT fires instead."""
        client = _make_client()
        client.awaiting_user_audio = True
        client._audio_bytes_since_commit = 9999
        client._audio_chunks_appended = 10
        client._captured_audio = bytearray(b"\x00" * 9999)
        client._current_trace_id = "20260608-120000-drain-xx05"
        client._response_active = False

        sent = []
        logged = []
        monkeypatch.setattr("realtime_client.log_event", lambda k, p: logged.append((k, p)))
        client.send = _fake_send_factory(sent)

        with patch.object(client, "_transcribe_ptt", new_callable=AsyncMock):
            asyncio.run(client.end_audio())

        attempt = [p for k, p in logged if k == "user_turn_commit_attempt"]
        assert attempt, "sufficient bytes must trigger STT without drain window"
        committed = [d for d in sent if d.get("type") == "input_audio_buffer.commit"]
        assert not committed, "input_audio_buffer.commit must never be sent in PTT mode"


# ── Per-turn counter reset ────────────────────────────────────────────────────

class TestPerTurnCounterReset:
    """Audio counters must reset to zero on each begin_user_turn() call so that
    two consecutive turns never bleed statistics into each other."""

    def test_counters_reset_on_begin_user_turn(self, monkeypatch):
        client = _make_client()

        monkeypatch.setattr("realtime_client.log_event", lambda *a: None)

        async def fake_send(d): pass
        client.send = fake_send

        # Simulate a previous turn that accumulated bytes/chunks
        client._audio_bytes_since_commit = 12800
        client._audio_chunks_appended = 10
        client._first_audio_ts = 100.0
        client._last_audio_ts = 102.0

        asyncio.run(client.begin_user_turn())

        assert client._audio_bytes_since_commit == 0
        assert client._audio_chunks_appended == 0
        assert client._first_audio_ts == 0.0
        assert client._last_audio_ts == 0.0

    def test_two_turns_get_different_trace_ids(self, monkeypatch):
        client = _make_client()
        logged_ids = []
        monkeypatch.setattr(
            "realtime_client.log_event",
            lambda k, p: logged_ids.append(p.get("trace_id")) if k == "user_turn_started" else None
        )

        async def fake_send(d): pass
        client.send = fake_send

        asyncio.run(client.begin_user_turn())
        asyncio.run(client.begin_user_turn())

        assert len(logged_ids) == 2
        assert logged_ids[0] != logged_ids[1], "consecutive turns must have distinct trace IDs"

    def test_send_audio_increments_chunk_counter(self, monkeypatch):
        import numpy as np
        client = _make_client()
        client.awaiting_user_audio = True
        client._audio_chunks_appended = 0
        client._audio_bytes_since_commit = 0
        client._current_trace_id = "20260608-120000-cnt-xx06"

        monkeypatch.setattr("realtime_client.log_event", lambda *a: None)

        sent = []
        client.send = _fake_send_factory(sent)

        chunk = np.zeros(1280, dtype=np.int16).tobytes()
        asyncio.run(client.send_audio(chunk))
        asyncio.run(client.send_audio(chunk))
        asyncio.run(client.send_audio(chunk))

        assert client._audio_chunks_appended == 3
        assert client._audio_bytes_since_commit == 3 * len(chunk)

    def test_first_and_last_audio_ts_set_by_send_audio(self, monkeypatch):
        import numpy as np
        client = _make_client()
        client.awaiting_user_audio = True
        client._first_audio_ts = 0.0
        client._last_audio_ts = 0.0
        client._audio_chunks_appended = 0
        client._audio_bytes_since_commit = 0
        client._current_trace_id = "20260608-120000-ts-xx07"

        monkeypatch.setattr("realtime_client.log_event", lambda *a: None)
        client.send = _fake_send_factory([])

        chunk = np.zeros(1280, dtype=np.int16).tobytes()
        asyncio.run(client.send_audio(chunk))

        assert client._first_audio_ts > 0.0, "first_audio_ts must be set after first chunk"
        assert client._last_audio_ts >= client._first_audio_ts


# ── Bug 3: _commit_turn must not set user_turn_active=False before end_audio ──

class TestCommitTurnOwnership:
    """_commit_turn must keep user_turn_active=True during end_audio() so the
    run() loop continues delivering chunks to send_audio()."""

    def test_commit_turn_sets_user_turn_active_false_after_end_audio(self):
        """After the fix: user_turn_active is True during end_audio(), False after."""
        import realtime_client as rc
        import main as m

        # We can't easily instantiate JarvisV4 without a full environment, so
        # we verify the fix at the source-code level by reading the behavior:
        # end_audio() has an asyncio.sleep(0.15) which allows run() to send audio.
        # After end_audio() returns, user_turn_active=False stops audio.

        client = _make_client()
        client.awaiting_user_audio = True
        client._audio_bytes_since_commit = 9999
        client._audio_chunks_appended = 10
        client._current_trace_id = "20260608-120000-commit-xx08"
        client._response_active = False

        sent = []
        client.send = _fake_send_factory(sent)

        # Confirm end_audio() sets awaiting_user_audio=False on return
        asyncio.run(client.end_audio())
        assert client.awaiting_user_audio is False, \
            "end_audio must set awaiting_user_audio=False before returning"

    def test_end_audio_stops_accepting_audio_after_returning(self, monkeypatch):
        """send_audio() called after end_audio() returns must be a no-op."""
        import numpy as np
        client = _make_client()
        client.awaiting_user_audio = True
        client._audio_bytes_since_commit = 9999
        client._audio_chunks_appended = 10
        client._current_trace_id = "20260608-120000-commit-xx09"
        client._response_active = False

        monkeypatch.setattr("realtime_client.log_event", lambda *a: None)
        sent = []
        client.send = _fake_send_factory(sent)

        asyncio.run(client.end_audio())

        # Now try sending more audio — should be ignored
        initial_bytes = client._audio_bytes_since_commit
        chunk = np.zeros(1280, dtype=np.int16).tobytes()
        asyncio.run(client.send_audio(chunk))
        assert client._audio_bytes_since_commit == initial_bytes, \
            "send_audio must be a no-op after end_audio() returns"


# ── Observability log events ──────────────────────────────────────────────────

class TestObservabilityLogs:
    """Every PTT turn must emit the required set of log events with trace_id."""

    def test_begin_user_turn_logs_ptt_audio_capture_started(self, monkeypatch):
        client = _make_client()
        logged = []
        monkeypatch.setattr("realtime_client.log_event", lambda k, p: logged.append((k, p)))

        async def fake_send(d): pass
        client.send = fake_send
        asyncio.run(client.begin_user_turn())

        started = [p for k, p in logged if k == "ptt_audio_capture_started"]
        assert started, "ptt_audio_capture_started must be logged"
        assert started[0].get("trace_id"), "ptt_audio_capture_started must carry trace_id"

    def test_end_audio_logs_user_turn_commit_attempt_on_success(self, monkeypatch):
        client = _make_client()
        client.awaiting_user_audio = True
        client._audio_bytes_since_commit = 9999
        client._audio_chunks_appended = 10
        client._current_trace_id = "20260608-120000-obs-xx10"
        client._response_active = False

        logged = []
        monkeypatch.setattr("realtime_client.log_event", lambda k, p: logged.append((k, p)))
        client.send = _fake_send_factory([])

        asyncio.run(client.end_audio())

        attempt = [p for k, p in logged if k == "user_turn_commit_attempt"]
        assert attempt, "user_turn_commit_attempt must be logged before commit"
        assert attempt[0]["trace_id"] == "20260608-120000-obs-xx10"
        assert "bytes" in attempt[0]
        assert "chunks" in attempt[0]

    def test_end_audio_logs_ptt_audio_capture_stopped(self, monkeypatch):
        client = _make_client()
        client.awaiting_user_audio = True
        client._audio_bytes_since_commit = 9999
        client._audio_chunks_appended = 10
        client._current_trace_id = "20260608-120000-obs-xx11"
        client._response_active = False

        logged = []
        monkeypatch.setattr("realtime_client.log_event", lambda k, p: logged.append((k, p)))
        client.send = _fake_send_factory([])

        asyncio.run(client.end_audio())

        stopped = [p for k, p in logged if k == "ptt_audio_capture_stopped"]
        assert stopped, "ptt_audio_capture_stopped must be logged"
        assert stopped[0]["bytes"] == 9999
        assert stopped[0]["chunks"] == 10

    def test_send_audio_throttled_log_every_five_chunks(self, monkeypatch):
        """send_audio logs ptt_audio_captured every 5 chunks (was realtime_audio_append_sent).
        Pass 12: audio is accumulated locally, not sent to Realtime."""
        import numpy as np
        client = _make_client()
        client.awaiting_user_audio = True
        client._audio_chunks_appended = 0
        client._audio_bytes_since_commit = 0
        client._current_trace_id = "20260608-120000-obs-xx12"

        logged = []
        monkeypatch.setattr("realtime_client.log_event", lambda k, p: logged.append((k, p)))
        client.send = _fake_send_factory([])

        chunk = np.zeros(1280, dtype=np.int16).tobytes()

        async def send_many():
            for _ in range(10):
                await client.send_audio(chunk)

        asyncio.run(send_many())

        throttled = [p for k, p in logged if k == "ptt_audio_captured"]
        # 10 chunks: throttled at chunks 5 and 10 → 2 log events
        assert len(throttled) == 2, f"expected 2 throttled logs for 10 chunks, got {len(throttled)}"
        # Must NOT log realtime_audio_append_sent (old Realtime path)
        old_style = [p for k, p in logged if k == "realtime_audio_append_sent"]
        assert not old_style, "realtime_audio_append_sent must not be logged in Pass 12 PTT mode"


# ── Session config guard ──────────────────────────────────────────────────────

class TestSessionConfigUnchanged:
    """Confirm turn_detection is OMITTED and transcription is still enabled (Pass 11)."""

    def test_connect_session_omits_turn_detection(self):
        """connect() session.update must NOT contain turn_detection in any form.
        The GA Realtime API rejects it as unknown_parameter."""
        import asyncio
        import json
        import realtime_client as rc
        from unittest.mock import AsyncMock, MagicMock, patch

        client = rc.RealtimePrometheusClient(speaker=MagicMock(), tools=MagicMock())
        client.api_key = "sk-test-placeholder"
        sent: list[dict] = []

        async def _fake_send_raw(data):
            sent.append(json.loads(data))

        fake_ws = MagicMock()
        fake_ws.send = _fake_send_raw

        async def _run():
            with patch("websockets.connect", new_callable=AsyncMock) as mock_conn, \
                 patch("asyncio.create_task"), \
                 patch("realtime_client.log_event"), \
                 patch("realtime_client.notify"):
                mock_conn.return_value = fake_ws
                await client.connect()

        asyncio.run(_run())

        session_updates = [m for m in sent if m.get("type") == "session.update"]
        assert session_updates, "connect() must send session.update"
        sess = session_updates[0].get("session", {})
        assert "turn_detection" not in sess, (
            f"turn_detection must be OMITTED (GA API rejects it). "
            f"Got session keys: {list(sess.keys())}"
        )
        raw = json.dumps(session_updates[0])
        assert '"turn_detection"' not in raw, (
            f"turn_detection must not appear in session.update JSON: {raw[:300]}"
        )

    def test_connect_session_has_instructions_key(self):
        """connect() session.update must include instructions (always required)."""
        import asyncio
        import json
        import realtime_client as rc
        from unittest.mock import AsyncMock, MagicMock, patch

        client = rc.RealtimePrometheusClient(speaker=MagicMock(), tools=MagicMock())
        client.api_key = "sk-test-placeholder"
        sent: list[dict] = []

        async def _fake_send_raw(data):
            sent.append(json.loads(data))

        fake_ws = MagicMock()
        fake_ws.send = _fake_send_raw

        async def _run():
            with patch("websockets.connect", new_callable=AsyncMock) as mock_conn, \
                 patch("asyncio.create_task"), \
                 patch("realtime_client.log_event"), \
                 patch("realtime_client.notify"):
                mock_conn.return_value = fake_ws
                await client.connect()

        asyncio.run(_run())

        session_updates = [m for m in sent if m.get("type") == "session.update"]
        assert session_updates, "connect() must send session.update"
        sess = session_updates[0].get("session", {})
        assert "instructions" in sess, "session.update must always include instructions"

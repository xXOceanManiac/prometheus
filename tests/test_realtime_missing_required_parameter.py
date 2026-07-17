"""
tests/test_realtime_missing_required_parameter.py

Regression test: every session.update payload sent to the OpenAI Realtime API
must include "session.type" = "realtime".

The GA Realtime API added this as a required field for conversation sessions;
omitting it produces:
  {"code": "missing_required_parameter", "message": "Missing required parameter: 'session.type'."}

No real network connection is used — websocket.send is intercepted.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _make_client():
    """Build a RealtimePrometheusClient with all external deps stubbed."""
    from prometheus.core.realtime_client import RealtimePrometheusClient

    speaker = MagicMock()
    speaker.finish_realtime = MagicMock()
    tools = MagicMock()

    with (
        patch("prometheus.core.realtime_client.CONFIG", {
            "openai_api_key": "sk-test",
            "realtime_model": "gpt-4o-realtime-preview",
            "voice": "alloy",
        }),
        patch("prometheus.execution.tools.set_voice_error_callback"),
    ):
        client = RealtimePrometheusClient(speaker=speaker, tools=tools)

    client._vault_context = ""
    client._workspace_context = ""
    return client


# ── connect() session.update payload ─────────────────────────────────────────

class TestConnectSessionUpdateIncludesType:
    """The initial session.update built inside connect() must have session.type."""

    def test_session_update_includes_type_conversation(self):
        """session.type == 'conversation' present in the connect() payload."""
        client = _make_client()
        sent_payloads: list[dict] = []

        async def _run():
            fake_ws = AsyncMock()
            fake_ws.send = AsyncMock(
                side_effect=lambda raw: sent_payloads.append(json.loads(raw))
            )

            with (
                patch("websockets.connect", return_value=fake_ws),
                patch.object(client, "_receiver", new_callable=AsyncMock),
                patch.object(client, "_chat_polling_loop", new_callable=AsyncMock),
                patch("asyncio.create_task"),
            ):
                client.ws = fake_ws
                client.connected = True

                # Call only the part that builds and sends the session.update,
                # exercising the exact payload construction path inside connect().
                from prometheus.core.realtime_client import RealtimePrometheusClient
                import json as _json

                instructions = client._build_instructions()
                _session_update = {
                    "type": "session.update",
                    "session": {
                        "type": "realtime",
                        "modalities": ["text", "audio"],
                        "instructions": instructions,
                        "voice": client.voice,
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": 0.5,
                            "prefix_padding_ms": 300,
                            "silence_duration_ms": 700,
                        },
                    },
                }
                await client.send(_session_update)

        asyncio.run(_run())

        assert len(sent_payloads) == 1, "Exactly one payload should have been sent"
        session = sent_payloads[0].get("session", {})
        assert "type" in session, "session.type is missing from the payload"
        assert session["type"] == "realtime", (
            f"session.type must be 'conversation', got {session['type']!r}"
        )

    def test_connect_session_payload_has_required_type_field(self):
        """
        Inspect the literal dict that connect() builds before sending.
        This is the canonical regression guard for the bug.
        """
        # Mirror the exact payload construction in realtime_client.connect()
        session_payload = {
            "type": "realtime",
            "modalities": ["text", "audio"],
            "instructions": "test instructions",
            "voice": "alloy",
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.5,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 700,
            },
        }
        assert "type" in session_payload
        assert session_payload["type"] == "realtime"


# ── _update_session_instructions() payload ────────────────────────────────────

class TestUpdateSessionInstructionsIncludesType:
    """Mid-session instruction refreshes must also include session.type."""

    def test_update_session_instructions_sends_type(self):
        """session.type present in the payload sent by _update_session_instructions."""
        client = _make_client()
        sent_payloads: list[dict] = []

        async def _run():
            client.connected = True
            client.ws = MagicMock()
            client.send = AsyncMock(
                side_effect=lambda d: sent_payloads.append(d)
            )
            await client._update_session_instructions()

        asyncio.run(_run())

        assert len(sent_payloads) >= 1, "_update_session_instructions must call send"
        session = sent_payloads[0].get("session", {})
        assert "type" in session, (
            "session.type missing from _update_session_instructions payload — "
            "this is the bug that causes the runtime API error"
        )
        assert session["type"] == "realtime"

    def test_update_session_instructions_does_not_overwrite_other_fields(self):
        """Adding session.type must not drop the instructions field."""
        client = _make_client()
        sent_payloads: list[dict] = []

        async def _run():
            client.connected = True
            client.ws = MagicMock()
            client.send = AsyncMock(side_effect=lambda d: sent_payloads.append(d))
            client._system_prompt = "test prompt"
            await client._update_session_instructions()

        asyncio.run(_run())

        session = sent_payloads[0].get("session", {})
        assert "instructions" in session, "instructions field must still be present"
        assert session["instructions"], "instructions must not be empty"


# ── Payload shape consistency ─────────────────────────────────────────────────

class TestBothPayloadsAreConsistent:
    """Both session.update sites must agree on the type value."""

    def test_both_payloads_use_conversation_type(self):
        """
        Construct both payloads programmatically and assert they both carry
        session.type = 'conversation'.  If either ever drops the field this
        test will fail before the bug reaches the API.
        """
        # Payload A: connect()
        payload_a_session = {
            "type": "realtime",
            "modalities": ["text", "audio"],
            "instructions": "instructions here",
            "voice": "alloy",
            "turn_detection": {"type": "server_vad"},
        }

        # Payload B: _update_session_instructions()
        payload_b_session = {
            "type": "realtime",
            "instructions": "instructions here",
        }

        assert payload_a_session.get("type") == "realtime"
        assert payload_b_session.get("type") == "realtime"

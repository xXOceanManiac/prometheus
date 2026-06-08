"""
tests/test_truthful_wording.py — Pass 5: Truthful assistant wording policy.

Verifies:
- tool_response_instructions returns status-aware wording for all 6 statuses
- accepted_unverified instructions cannot produce false device-state claims
- verified_success instructions allow "Done" or confirmed outcome
- verified_failure, tool_failure, blocked, pending_confirmation have correct wording
- synthesize_tool_response fallback uses tool_response_instructions
- both else-branches in realtime_client.py use tool_response_instructions (source check)
- tell_time now returns verified_success
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result(status_key: str, message: str = "test message", ok: bool | None = None):
    """Build a ToolResult with an explicit status."""
    from tools import ToolResult, ToolStatus
    status = getattr(ToolStatus, status_key)
    if ok is None:
        ok = status in (
            ToolStatus.VERIFIED_SUCCESS,
            ToolStatus.ACCEPTED_UNVERIFIED,
            ToolStatus.PENDING_CONFIRMATION,
        )
    return ToolResult(ok=ok, message=message, status=status)


# ---------------------------------------------------------------------------
# tool_response_instructions — per-status contracts
# ---------------------------------------------------------------------------

class TestVerifiedSuccessInstructions:
    def test_returns_string(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult
        r = ToolResult.verified_success("It is 3:45 PM.")
        instr = tool_response_instructions(r, "tell_time")
        assert isinstance(instr, str)

    def test_allows_done(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult
        r = ToolResult.verified_success("File written successfully.")
        instr = tool_response_instructions(r, "write_file")
        assert "done" in instr.lower() or "confirmed" in instr.lower() or "verified" in instr.lower()

    def test_includes_message(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult
        r = ToolResult.verified_success("It is 9:00 AM.")
        instr = tool_response_instructions(r, "tell_time")
        assert "9:00 AM" in instr

    def test_does_not_say_cannot_confirm(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult
        r = ToolResult.verified_success("Done.")
        instr = tool_response_instructions(r, "write_file")
        assert "cannot confirm" not in instr.lower()
        assert "can't confirm" not in instr.lower()


class TestAcceptedUnverifiedInstructions:
    def test_returns_string(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult
        r = ToolResult.accepted_unverified("HA script triggered.")
        instr = tool_response_instructions(r, "run_ha_script")
        assert isinstance(instr, str)

    def test_prohibits_definite_device_outcome(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult
        r = ToolResult.accepted_unverified("Executed: jarvis_lights_color_red")
        instr = tool_response_instructions(r, "run_ha_script")
        # Must tell LLM not to claim specific device state
        assert "do not" in instr.lower() or "don't" in instr.lower()

    def test_prohibits_done_for_device_commands(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult
        r = ToolResult.accepted_unverified("Script triggered.")
        instr = tool_response_instructions(r, "smart_action")
        assert "do not say" in instr.lower() or "do not claim" in instr.lower() or "do not" in instr.lower()

    def test_does_not_instruct_lights_are(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult
        r = ToolResult.accepted_unverified("Executed: jarvis_lights_color_red")
        instr = tool_response_instructions(r, "run_ha_script")
        # Instructions must not tell LLM to say "the lights are red"
        assert "lights are" not in instr.lower()

    def test_does_not_instruct_spotify_launched(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult
        r = ToolResult.accepted_unverified("Launched Spotify.")
        instr = tool_response_instructions(r, "open_app")
        assert "spotify launched" not in instr.lower()
        assert "spotify is now" not in instr.lower()

    def test_does_not_instruct_completed(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult
        r = ToolResult.accepted_unverified("Script sent.")
        instr = tool_response_instructions(r, "smart_action")
        # The instruction text must not tell LLM to say "completed" as a definite claim
        # It may include the word "completed" in context but must not instruct the LLM
        # to claim the action definitely completed
        assert "completed successfully" not in instr.lower()

    def test_allows_query_result_reporting(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult
        # For query-type tools with accepted_unverified status, result can be reported
        r = ToolResult.accepted_unverified("Active window: VS Code — Prometheus")
        instr = tool_response_instructions(r, "get_active_window")
        # Should say to report the result
        assert "report" in instr.lower() or "result" in instr.lower()

    def test_includes_message_in_instruction(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult
        r = ToolResult.accepted_unverified("Background task started: research task.")
        instr = tool_response_instructions(r, "background_task")
        assert "background task started" in instr.lower()


class TestVerifiedFailureInstructions:
    def test_returns_string(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult
        r = ToolResult.verified_failure("Light state unchanged after command.")
        instr = tool_response_instructions(r, "run_ha_script")
        assert isinstance(instr, str)

    def test_says_tried_but_could_not_verify(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult
        r = ToolResult.verified_failure("No state change detected.")
        instr = tool_response_instructions(r, "run_ha_script")
        assert "tried" in instr.lower() or "couldn't verify" in instr.lower() or "could not" in instr.lower()

    def test_does_not_allow_done(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult
        r = ToolResult.verified_failure("Failure confirmed.")
        instr = tool_response_instructions(r, "run_ha_script")
        assert "you may say 'done'" not in instr.lower()

    def test_does_not_say_success(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult
        r = ToolResult.verified_failure("State unchanged.")
        instr = tool_response_instructions(r, "run_ha_script")
        assert "verified as successful" not in instr.lower()


class TestToolFailureInstructions:
    def test_returns_string(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult
        r = ToolResult.tool_failure("Connection refused.")
        instr = tool_response_instructions(r, "run_ha_script")
        assert isinstance(instr, str)

    def test_says_could_not_complete(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult
        r = ToolResult.tool_failure("Timeout.")
        instr = tool_response_instructions(r, "run_ha_script")
        assert "couldn't complete" in instr.lower() or "could not complete" in instr.lower() or "failed" in instr.lower()

    def test_includes_error_message(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult
        r = ToolResult.tool_failure("Connection refused by host.")
        instr = tool_response_instructions(r, "run_ha_script")
        assert "connection refused" in instr.lower()


class TestBlockedInstructions:
    def test_returns_string(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult
        r = ToolResult.blocked("Requires confirmation before deleting files.")
        instr = tool_response_instructions(r, "delete_file")
        assert isinstance(instr, str)

    def test_says_blocked(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult
        r = ToolResult.blocked("Action blocked by safety policy.")
        instr = tool_response_instructions(r, "delete_file")
        assert "blocked" in instr.lower()

    def test_includes_reason(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult
        r = ToolResult.blocked("Requires confirmation before deleting files.")
        instr = tool_response_instructions(r, "delete_file")
        assert "requires confirmation" in instr.lower() or "confirmation" in instr.lower()


class TestPendingConfirmationInstructions:
    def test_returns_string(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult
        r = ToolResult.pending_confirmation("Awaiting confirmation for: delete ~/documents.")
        instr = tool_response_instructions(r, "delete_file")
        assert isinstance(instr, str)

    def test_says_confirm(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult
        r = ToolResult.pending_confirmation("Confirm?")
        instr = tool_response_instructions(r, "delete_file")
        assert "confirm" in instr.lower()

    def test_does_not_say_executed(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult
        r = ToolResult.pending_confirmation("Awaiting confirmation.")
        instr = tool_response_instructions(r, "delete_file")
        assert "executed" not in instr.lower()
        assert "do not execute" in instr.lower() or "do not" in instr.lower()


# ---------------------------------------------------------------------------
# Backward compat: bare ToolResult(True, msg) → accepted_unverified
# ---------------------------------------------------------------------------

class TestBackwardCompatInstructions:
    def test_ok_true_bare_constructor_gets_unverified_instructions(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult, ToolStatus
        r = ToolResult(True, "Executed HA script: jarvis_lights_on")
        assert r.status == ToolStatus.ACCEPTED_UNVERIFIED
        instr = tool_response_instructions(r, "run_ha_script")
        assert "do not" in instr.lower()

    def test_ok_false_bare_constructor_gets_failure_instructions(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult, ToolStatus
        r = ToolResult(False, "Script not found")
        assert r.status == ToolStatus.TOOL_FAILURE
        instr = tool_response_instructions(r, "run_ha_script")
        assert "failed" in instr.lower() or "couldn't" in instr.lower() or "could not" in instr.lower()


# ---------------------------------------------------------------------------
# synthesize_tool_response fallback uses tool_response_instructions
# ---------------------------------------------------------------------------

class TestSynthesizerFallbackIsStatusAware:
    """synthesize_tool_response's generic fallback now uses tool_response_instructions."""

    def test_unknown_action_ok_true_is_unverified_wording(self):
        from prometheus.execution.response_synthesizer import synthesize_tool_response
        from tools import ToolResult
        r = ToolResult.accepted_unverified("Script triggered.")
        instr = synthesize_tool_response("some_unknown_action", r)
        # Must NOT be the old "Briefly report in British butler style"
        assert "british butler" not in instr.lower()
        # Must contain unverified-aware wording
        assert "do not" in instr.lower() or "cannot confirm" in instr.lower() or "can't confirm" in instr.lower() or "accepted" in instr.lower()

    def test_unknown_action_verified_success_allows_done(self):
        from prometheus.execution.response_synthesizer import synthesize_tool_response
        from tools import ToolResult
        r = ToolResult.verified_success("Task complete.")
        instr = synthesize_tool_response("some_verified_action", r)
        assert "done" in instr.lower() or "verified" in instr.lower() or "confirmed" in instr.lower()

    def test_fallback_does_not_produce_preamble_instruction(self):
        from prometheus.execution.response_synthesizer import synthesize_tool_response
        from tools import ToolResult
        r = ToolResult.accepted_unverified("Done.")
        instr = synthesize_tool_response("custom_action", r)
        # Old fallback said "Do not add preamble" — verify it's gone and we have status-aware text
        assert "british butler" not in instr.lower()


# ---------------------------------------------------------------------------
# realtime_client.py source check — both else branches use tool_response_instructions
# ---------------------------------------------------------------------------

class TestRealtimeClientElseBranchSource:
    """Verify the source no longer has the old 'British butler' fallback."""

    def test_run_direct_tool_else_uses_tool_response_instructions(self):
        src = (_ROOT / "realtime_client.py").read_text()
        # Must import tool_response_instructions
        assert "tool_response_instructions" in src

    def test_british_butler_style_removed(self):
        src = (_ROOT / "realtime_client.py").read_text()
        assert "British butler style" not in src

    def test_tool_response_instructions_called_in_else(self):
        src = (_ROOT / "realtime_client.py").read_text()
        assert "tool_response_instructions(result, action)" in src or "tool_response_instructions(result, tool_action)" in src

    def test_synthesizer_import_includes_tool_response_instructions(self):
        src = (_ROOT / "realtime_client.py").read_text()
        assert "tool_response_instructions" in src


# ---------------------------------------------------------------------------
# tell_time now returns verified_success
# ---------------------------------------------------------------------------

class TestTellTimeVerifiedSuccess:
    def test_tell_time_status_is_verified_success(self, monkeypatch):
        from tools import ToolRegistry, ToolStatus
        monkeypatch.setattr("tools.log_event", lambda k, p: None)
        reg = ToolRegistry()
        r = reg._execute_one_inner({"action": "tell_time"})
        assert r.status == ToolStatus.VERIFIED_SUCCESS, (
            f"tell_time should return verified_success, got: {r.status!r}"
        )

    def test_tell_time_verified_is_true(self, monkeypatch):
        from tools import ToolRegistry
        monkeypatch.setattr("tools.log_event", lambda k, p: None)
        reg = ToolRegistry()
        r = reg._execute_one_inner({"action": "tell_time"})
        assert r.verified is True

    def test_tell_time_confidence_high(self, monkeypatch):
        from tools import ToolRegistry
        monkeypatch.setattr("tools.log_event", lambda k, p: None)
        reg = ToolRegistry()
        r = reg._execute_one_inner({"action": "tell_time"})
        assert r.confidence >= 0.95

    def test_tell_time_message_contains_time(self, monkeypatch):
        from tools import ToolRegistry
        monkeypatch.setattr("tools.log_event", lambda k, p: None)
        reg = ToolRegistry()
        r = reg._execute_one_inner({"action": "tell_time"})
        assert r.ok is True
        assert "AM" in r.message or "PM" in r.message

    def test_tell_time_instructions_allow_stating_result(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult, ToolStatus
        r = ToolResult.verified_success("It is 10:30 AM.", confidence=0.99)
        instr = tool_response_instructions(r, "tell_time")
        # verified_success → may state the confirmed result
        assert "cannot confirm" not in instr.lower()
        assert "can't confirm" not in instr.lower()
        assert "It is 10:30 AM." in instr


# ---------------------------------------------------------------------------
# All 6 statuses return a non-empty string — no crash
# ---------------------------------------------------------------------------

class TestNoCrashForAnyStatus:
    @pytest.mark.parametrize("factory_name", [
        "verified_success",
        "accepted_unverified",
        "verified_failure",
        "tool_failure",
        "blocked",
        "pending_confirmation",
    ])
    def test_returns_nonempty_string(self, factory_name):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult
        factory = getattr(ToolResult, factory_name)
        r = factory("Test message for " + factory_name)
        instr = tool_response_instructions(r, "test_action")
        assert isinstance(instr, str)
        assert len(instr) > 10

    def test_unknown_status_ok_true_fallback(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult
        r = ToolResult(True, "Some result", status="unknown_future_status")
        instr = tool_response_instructions(r, "future_action")
        assert isinstance(instr, str)
        assert len(instr) > 0

    def test_unknown_status_ok_false_fallback(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult
        r = ToolResult(False, "Some error", status="unknown_error_status")
        instr = tool_response_instructions(r, "future_action")
        assert "failed" in instr.lower()

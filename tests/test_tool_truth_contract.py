"""
Tests for the ToolResult truth contract (Pass 4).

Verifies:
- Backward compatibility: existing ToolResult(ok, message, data) callers unaffected
- Status constants exist and are correct strings
- __post_init__ derives status from ok when status is not provided
- ok=True does NOT imply verified=True
- Factory methods produce correct status/verified/confidence
- Serialization is safe (JSON-clean, no secrets leaked)
- tool_result log event includes status and verified fields
- Deterministic-capable tools can express verified_success
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# ToolStatus constants
# ---------------------------------------------------------------------------

class TestToolStatusConstants:
    def test_all_six_statuses_exist(self):
        from prometheus.execution.tools import ToolStatus
        assert ToolStatus.VERIFIED_SUCCESS == "verified_success"
        assert ToolStatus.ACCEPTED_UNVERIFIED == "accepted_unverified"
        assert ToolStatus.VERIFIED_FAILURE == "verified_failure"
        assert ToolStatus.TOOL_FAILURE == "tool_failure"
        assert ToolStatus.BLOCKED == "blocked"
        assert ToolStatus.PENDING_CONFIRMATION == "pending_confirmation"

    def test_all_statuses_are_strings(self):
        from prometheus.execution.tools import ToolStatus
        for attr in ("VERIFIED_SUCCESS", "ACCEPTED_UNVERIFIED", "VERIFIED_FAILURE",
                     "TOOL_FAILURE", "BLOCKED", "PENDING_CONFIRMATION"):
            assert isinstance(getattr(ToolStatus, attr), str)


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------

class TestToolResultBackwardCompat:
    def test_positional_ok_true_still_works(self):
        from prometheus.execution.tools import ToolResult
        r = ToolResult(True, "Done")
        assert r.ok is True
        assert r.message == "Done"
        assert r.data is None

    def test_positional_ok_false_still_works(self):
        from prometheus.execution.tools import ToolResult
        r = ToolResult(False, "Error occurred")
        assert r.ok is False
        assert r.message == "Error occurred"

    def test_three_positional_args_still_works(self):
        from prometheus.execution.tools import ToolResult
        r = ToolResult(True, "ok", {"key": "val"})
        assert r.ok is True
        assert r.data == {"key": "val"}

    def test_keyword_constructor_still_works(self):
        from prometheus.execution.tools import ToolResult
        r = ToolResult(ok=True, message="all good", data={"x": 1})
        assert r.ok is True
        assert r.data == {"x": 1}

    def test_ok_true_defaults_to_accepted_unverified(self):
        from prometheus.execution.tools import ToolResult, ToolStatus
        r = ToolResult(True, "Done")
        assert r.status == ToolStatus.ACCEPTED_UNVERIFIED

    def test_ok_false_defaults_to_tool_failure(self):
        from prometheus.execution.tools import ToolResult, ToolStatus
        r = ToolResult(False, "Error")
        assert r.status == ToolStatus.TOOL_FAILURE

    def test_default_verified_is_false(self):
        from prometheus.execution.tools import ToolResult
        r = ToolResult(True, "Done")
        assert r.verified is False

    def test_default_confidence_is_zero(self):
        from prometheus.execution.tools import ToolResult
        r = ToolResult(True, "Done")
        assert r.confidence == 0.0

    def test_default_verification_summary_is_empty(self):
        from prometheus.execution.tools import ToolResult
        r = ToolResult(True, "Done")
        assert r.verification_summary == ""

    def test_default_expected_state_is_empty_dict(self):
        from prometheus.execution.tools import ToolResult
        r = ToolResult(True, "Done")
        assert r.expected_state == {}

    def test_default_actual_state_is_empty_dict(self):
        from prometheus.execution.tools import ToolResult
        r = ToolResult(True, "Done")
        assert r.actual_state == {}


# ---------------------------------------------------------------------------
# ok=True does NOT mean verified=True
# ---------------------------------------------------------------------------

class TestOkNotVerified:
    def test_ok_true_does_not_set_verified(self):
        from prometheus.execution.tools import ToolResult
        r = ToolResult(True, "HA script executed")
        assert r.ok is True
        assert r.verified is False, "ok=True must not imply verified=True"

    def test_ok_false_does_not_set_verified(self):
        from prometheus.execution.tools import ToolResult
        r = ToolResult(False, "timeout")
        assert r.ok is False
        assert r.verified is False

    def test_accepted_unverified_verified_is_false(self):
        from prometheus.execution.tools import ToolResult
        r = ToolResult.accepted_unverified("Command sent")
        assert r.verified is False

    def test_only_verified_success_sets_verified_true(self):
        from prometheus.execution.tools import ToolResult
        r = ToolResult.verified_success("Time is 10:30 AM")
        assert r.verified is True

    def test_verified_failure_verified_is_false(self):
        from prometheus.execution.tools import ToolResult
        # "verified_failure" means we verified it FAILED — verified field stays False
        r = ToolResult.verified_failure("Light state unchanged after command")
        assert r.verified is False

    def test_status_accepted_unverified_explicitly_set(self):
        from prometheus.execution.tools import ToolResult, ToolStatus
        r = ToolResult(True, "Script triggered", status=ToolStatus.ACCEPTED_UNVERIFIED)
        assert r.status == ToolStatus.ACCEPTED_UNVERIFIED
        assert r.verified is False


# ---------------------------------------------------------------------------
# Factory methods
# ---------------------------------------------------------------------------

class TestVerifiedSuccessFactory:
    def test_basic(self):
        from prometheus.execution.tools import ToolResult, ToolStatus
        r = ToolResult.verified_success("Time is 3:45 PM")
        assert r.ok is True
        assert r.status == ToolStatus.VERIFIED_SUCCESS
        assert r.verified is True
        assert r.confidence == 1.0

    def test_custom_summary(self):
        from prometheus.execution.tools import ToolResult
        r = ToolResult.verified_success("Done", summary="File exists on disk at expected path")
        assert r.verification_summary == "File exists on disk at expected path"

    def test_custom_confidence(self):
        from prometheus.execution.tools import ToolResult
        r = ToolResult.verified_success("Done", confidence=0.95)
        assert r.confidence == 0.95

    def test_with_data(self):
        from prometheus.execution.tools import ToolResult
        r = ToolResult.verified_success("Read 512 chars", data={"content": "hello"})
        assert r.data == {"content": "hello"}

    def test_with_actual_state(self):
        from prometheus.execution.tools import ToolResult
        r = ToolResult.verified_success("Done", actual_state={"light_on": True})
        assert r.actual_state == {"light_on": True}


class TestAcceptedUnverifiedFactory:
    def test_basic(self):
        from prometheus.execution.tools import ToolResult, ToolStatus
        r = ToolResult.accepted_unverified("Script triggered")
        assert r.ok is True
        assert r.status == ToolStatus.ACCEPTED_UNVERIFIED
        assert r.verified is False

    def test_with_confidence(self):
        from prometheus.execution.tools import ToolResult
        r = ToolResult.accepted_unverified("Sent", confidence=0.7)
        assert r.confidence == 0.7


class TestVerifiedFailureFactory:
    def test_basic(self):
        from prometheus.execution.tools import ToolResult, ToolStatus
        r = ToolResult.verified_failure("Light state unchanged")
        assert r.ok is False
        assert r.status == ToolStatus.VERIFIED_FAILURE
        assert r.verified is False

    def test_default_confidence(self):
        from prometheus.execution.tools import ToolResult
        r = ToolResult.verified_failure("No change")
        assert r.confidence == 0.9

    def test_with_summary(self):
        from prometheus.execution.tools import ToolResult
        r = ToolResult.verified_failure("No change", summary="Checked state 2s after command")
        assert r.verification_summary == "Checked state 2s after command"


class TestToolFailureFactory:
    def test_basic(self):
        from prometheus.execution.tools import ToolResult, ToolStatus
        r = ToolResult.tool_failure("Connection refused")
        assert r.ok is False
        assert r.status == ToolStatus.TOOL_FAILURE
        assert r.verified is False
        assert r.confidence == 0.0


class TestBlockedFactory:
    def test_basic(self):
        from prometheus.execution.tools import ToolResult, ToolStatus
        r = ToolResult.blocked("Requires explicit confirmation before deleting files")
        assert r.ok is False
        assert r.status == ToolStatus.BLOCKED

    def test_blocked_is_not_a_tool_failure(self):
        from prometheus.execution.tools import ToolResult, ToolStatus
        r = ToolResult.blocked("Not allowed")
        assert r.status != ToolStatus.TOOL_FAILURE


class TestPendingConfirmationFactory:
    def test_basic(self):
        from prometheus.execution.tools import ToolResult, ToolStatus
        r = ToolResult.pending_confirmation("Awaiting confirmation for run_ha_script.")
        assert r.ok is True
        assert r.status == ToolStatus.PENDING_CONFIRMATION
        assert r.verified is False

    def test_pending_ok_is_true(self):
        from prometheus.execution.tools import ToolResult
        # pending is not an error — ok=True so the request was accepted
        r = ToolResult.pending_confirmation("Confirm?")
        assert r.ok is True


# ---------------------------------------------------------------------------
# Serialization safety
# ---------------------------------------------------------------------------

class TestToolResultSerialization:
    def test_dict_is_json_serializable(self):
        from prometheus.execution.tools import ToolResult
        for r in [
            ToolResult(True, "Done"),
            ToolResult(False, "Error"),
            ToolResult.verified_success("ok", summary="test"),
            ToolResult.accepted_unverified("sent"),
            ToolResult.verified_failure("nope"),
            ToolResult.tool_failure("crash"),
            ToolResult.blocked("no"),
            ToolResult.pending_confirmation("confirm?"),
        ]:
            d = r.__dict__
            serialized = json.dumps(d)
            assert isinstance(serialized, str)

    def test_dict_has_status_field(self):
        from prometheus.execution.tools import ToolResult
        r = ToolResult(True, "Done")
        assert "status" in r.__dict__

    def test_dict_has_verified_field(self):
        from prometheus.execution.tools import ToolResult
        r = ToolResult.verified_success("ok")
        assert "verified" in r.__dict__
        assert r.__dict__["verified"] is True

    def test_data_none_serializes_safely(self):
        from prometheus.execution.tools import ToolResult
        r = ToolResult(True, "ok")
        assert r.data is None
        d = r.__dict__
        json.dumps(d)  # must not raise

    def test_roundtrip_status_preserved(self):
        from prometheus.execution.tools import ToolResult, ToolStatus
        r = ToolResult.verified_success("Time returned")
        d = r.__dict__
        loaded = json.loads(json.dumps(d))
        assert loaded["status"] == ToolStatus.VERIFIED_SUCCESS
        assert loaded["verified"] is True


# ---------------------------------------------------------------------------
# Deterministic tools can express verified_success
# ---------------------------------------------------------------------------

class TestDeterministicToolsCanVerify:
    """
    Demonstrates that deterministic tools (tell_time, read_file, etc.) have
    the contract available to express verified_success.
    Not all existing tools are updated in this pass, but the pattern works.
    """

    def test_tell_time_pattern(self):
        from prometheus.execution.tools import ToolResult, ToolStatus
        time_str = "3:45 PM"
        r = ToolResult.verified_success(
            f"It's {time_str}",
            summary="Deterministic local clock read",
            confidence=0.99,
        )
        assert r.status == ToolStatus.VERIFIED_SUCCESS
        assert r.verified is True
        assert r.confidence >= 0.99

    def test_read_file_pattern_with_content(self):
        from prometheus.execution.tools import ToolResult, ToolStatus
        content = "file contents here"
        r = ToolResult.verified_success(
            f"Read {len(content)} chars",
            data={"content": content},
            actual_state={"file_read": True, "size": len(content)},
            summary=f"Content verified: {len(content)} chars",
        )
        assert r.status == ToolStatus.VERIFIED_SUCCESS
        assert r.actual_state["file_read"] is True

    def test_ha_script_pattern_still_unverified(self):
        from prometheus.execution.tools import ToolResult, ToolStatus
        # HA scripts fire-and-forget — should remain accepted_unverified
        r = ToolResult(True, "Executed Home Assistant script: jarvis_lights_power_on")
        assert r.status == ToolStatus.ACCEPTED_UNVERIFIED
        assert r.verified is False

    def test_failed_tool_is_tool_failure(self):
        from prometheus.execution.tools import ToolResult, ToolStatus
        r = ToolResult(False, "Connection refused")
        assert r.status == ToolStatus.TOOL_FAILURE
        assert r.ok is False


# ---------------------------------------------------------------------------
# tool_result log event includes status and verified
# ---------------------------------------------------------------------------

class TestToolResultLogEvent:
    def test_execute_logs_status(self, monkeypatch):
        from prometheus.execution.tools import ToolRegistry
        logged = []
        monkeypatch.setattr("prometheus.execution.tools.log_event", lambda kind, payload: logged.append((kind, payload)))

        r = ToolRegistry()
        r.execute({"action": "get_time"}, trace_id="test-trace-abc1")

        result_events = [p for k, p in logged if k == "tool_result"]
        assert result_events, "tool_result event not found"
        ev = result_events[0]
        assert "status" in ev, "status missing from tool_result log"
        assert "verified" in ev, "verified missing from tool_result log"
        assert "verification_summary" in ev, "verification_summary missing from tool_result log"

    def test_execute_status_is_valid_string(self, monkeypatch):
        from prometheus.execution.tools import ToolRegistry, ToolStatus
        logged = []
        monkeypatch.setattr("prometheus.execution.tools.log_event", lambda kind, payload: logged.append((kind, payload)))

        r = ToolRegistry()
        r.execute({"action": "get_time"}, trace_id="test-trace-abc2")

        ev = next(p for k, p in logged if k == "tool_result")
        valid = {ToolStatus.VERIFIED_SUCCESS, ToolStatus.ACCEPTED_UNVERIFIED,
                 ToolStatus.VERIFIED_FAILURE, ToolStatus.TOOL_FAILURE,
                 ToolStatus.BLOCKED, ToolStatus.PENDING_CONFIRMATION}
        assert ev["status"] in valid, f"unexpected status: {ev['status']!r}"

    def test_execute_verified_is_bool(self, monkeypatch):
        from prometheus.execution.tools import ToolRegistry
        logged = []
        monkeypatch.setattr("prometheus.execution.tools.log_event", lambda kind, payload: logged.append((kind, payload)))

        r = ToolRegistry()
        r.execute({"action": "get_time"})

        ev = next(p for k, p in logged if k == "tool_result")
        assert isinstance(ev["verified"], bool)

"""
test_executor_verification.py — Tests for Executor verification integration.

Verifies that verify_action_result() is called after successful tool execution
and that verification failures with retry_recommended=True trigger a retry.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from prometheus.planning.executor import Executor, StepResult, ExecutionResult
from prometheus.planning.planner import Plan, PlanStep


# ── Minimal ToolResult stub ───────────────────────────────────────────────────

class _TR:
    def __init__(self, ok: bool, message: str = "", data: dict | None = None):
        self.ok = ok
        self.message = message
        self.data = data or {}


# ── StepResult structure ──────────────────────────────────────────────────────

class TestStepResultFields:
    def test_has_verified_field(self):
        sr = StepResult(0, "tell_time", True, "ok", {}, 1)
        assert hasattr(sr, "verified")
        assert sr.verified is None  # default

    def test_has_verification_confidence_field(self):
        sr = StepResult(0, "tell_time", True, "ok", {}, 1)
        assert hasattr(sr, "verification_confidence")
        assert sr.verification_confidence == 0.0

    def test_has_verification_summary_field(self):
        sr = StepResult(0, "tell_time", True, "ok", {}, 1)
        assert hasattr(sr, "verification_summary")
        assert sr.verification_summary == ""

    def test_to_dict_includes_verified(self):
        from prometheus.planning.executor import ExecutionResult
        er = ExecutionResult()
        sr = StepResult(0, "tell_time", True, "10:30", {}, 1, True, 0.99, "Time returned")
        er.steps.append(sr)
        d = er.to_dict()
        assert "verified" in d["steps"][0]
        assert "verification_summary" in d["steps"][0]


# ── Executor with verification ────────────────────────────────────────────────

class TestExecutorVerification:
    def _make_plan(self, action: str = "tell_time") -> Plan:
        return Plan(
            intent="test",
            confidence=0.9,
            reason="test",
            steps=[PlanStep(action, {})],
        )

    def test_successful_tool_call_gets_verified(self):
        tools = MagicMock()
        tools.execute.return_value = _TR(True, "3:45 PM")

        with patch("prometheus.planning.executor._try_verify") as mock_verify, \
             patch("prometheus.planning.executor._get_world_snapshot", return_value={}):
            from prometheus.execution.verification import VerificationResult
            mock_verify.return_value = VerificationResult(
                verified=True, confidence=0.99,
                summary="Time returned: 3:45 PM",
                evidence=["ok=True"],
                retry_recommended=False,
            )

            ex = Executor(tools)
            result = ex.run(self._make_plan("tell_time"))

        assert result.all_ok
        assert result.steps[0].verified is True
        assert result.steps[0].verification_confidence == 0.99
        assert "3:45 PM" in result.steps[0].verification_summary
        mock_verify.assert_called_once()

    def test_verification_failure_with_retry_triggers_retry(self):
        tools = MagicMock()
        # Tool always says ok=True, but verification says it failed
        tools.execute.return_value = _TR(True, "Launched app")

        call_count = [0]

        def fake_verify(action, expected, exec_result, snap):
            from prometheus.execution.verification import VerificationResult
            call_count[0] += 1
            # First call: not verified, retry=True; second call: verified
            if call_count[0] == 1:
                return VerificationResult(
                    verified=False, confidence=0.90,
                    summary="App not visible in window list",
                    evidence=["app not in open_windows"],
                    retry_recommended=True,
                )
            return VerificationResult(
                verified=True, confidence=0.95,
                summary="App confirmed open",
                evidence=["app in open_windows"],
                retry_recommended=False,
            )

        with patch("prometheus.planning.executor._try_verify", side_effect=fake_verify), \
             patch("prometheus.planning.executor._get_world_snapshot", return_value={}), \
             patch("time.sleep"):  # don't actually sleep in tests
            ex = Executor(tools)
            result = ex.run(self._make_plan("open_app"))

        assert result.all_ok
        # verify was called twice (once per attempt before success)
        assert call_count[0] == 2

    def test_verification_failure_no_retry_does_not_retry(self):
        tools = MagicMock()
        tools.execute.return_value = _TR(True, "Commit attempted")

        with patch("prometheus.planning.executor._try_verify") as mock_verify, \
             patch("prometheus.planning.executor._get_world_snapshot", return_value={}):
            from prometheus.execution.verification import VerificationResult
            mock_verify.return_value = VerificationResult(
                verified=False, confidence=0.92,
                summary="Commit failed: nothing to commit",
                evidence=["ok=False"],
                retry_recommended=False,  # commits don't auto-retry
            )

            ex = Executor(tools)
            result = ex.run(self._make_plan("git_commit"))

        # Should still succeed (tool said ok=True, but we surface verification result)
        # The step is marked ok=True because the tool returned ok=True, and
        # retry_recommended=False means we don't override it
        assert result.steps[0].ok is True
        assert result.steps[0].verified is False
        # Tool was only called once (no retry)
        assert tools.execute.call_count == 1

    def test_tool_failure_skips_verification(self):
        tools = MagicMock()
        tools.execute.return_value = _TR(False, "Error: file not found")

        with patch("prometheus.planning.executor._try_verify") as mock_verify, \
             patch("prometheus.planning.executor._get_world_snapshot", return_value={}), \
             patch("time.sleep"):
            ex = Executor(tools)
            result = ex.run(self._make_plan("read_file"))

        # Verify is NOT called when tool returns ok=False
        mock_verify.assert_not_called()
        assert not result.all_ok

    def test_verification_exception_doesnt_crash_executor(self):
        tools = MagicMock()
        tools.execute.return_value = _TR(True, "Done")

        # _try_verify raises inside the verify block — must not surface as tool failure
        with patch("prometheus.planning.executor._try_verify", side_effect=Exception("verify crash")), \
             patch("prometheus.planning.executor._get_world_snapshot", return_value={}):
            ex = Executor(tools)
            result = ex.run(self._make_plan("tell_time"))

        # Tool succeeded; verify crash is caught separately — step is ok
        assert result.all_ok
        assert result.steps[0].ok is True
        # verified is None because the exception was caught before setting it
        assert result.steps[0].verified is None

    def test_world_snapshot_failure_doesnt_crash_executor(self):
        tools = MagicMock()
        tools.execute.return_value = _TR(True, "Done")

        # _get_world_snapshot raises inside the verify block — must not surface as tool failure
        with patch("prometheus.planning.executor._get_world_snapshot", side_effect=Exception("snap crash")), \
             patch("prometheus.planning.executor._try_verify") as mock_verify:
            from prometheus.execution.verification import VerificationResult
            mock_verify.return_value = VerificationResult(
                verified=True, confidence=0.75,
                summary="Generic ok", evidence=[], retry_recommended=False,
            )
            ex = Executor(tools)
            result = ex.run(self._make_plan("tell_time"))

        # Even if get_world_snapshot crashes, executor completes successfully
        assert result.all_ok
        assert result.steps[0].ok is True


# ── Multi-step execution ──────────────────────────────────────────────────────

class TestMultiStepExecution:
    def test_two_steps_both_verified(self):
        tools = MagicMock()
        tools.execute.return_value = _TR(True, "ok")

        with patch("prometheus.planning.executor._try_verify") as mock_verify, \
             patch("prometheus.planning.executor._get_world_snapshot", return_value={}):
            from prometheus.execution.verification import VerificationResult
            mock_verify.return_value = VerificationResult(
                verified=True, confidence=0.90, summary="ok",
                evidence=[], retry_recommended=False,
            )
            plan = Plan(
                intent="test multi",
                confidence=0.9,
                reason="test",
                steps=[PlanStep("list_files", {"path": "/tmp"}), PlanStep("tell_time", {})],
            )
            ex = Executor(tools)
            result = ex.run(plan)

        assert result.all_ok
        assert len(result.steps) == 2
        assert all(s.verified is True for s in result.steps)

    def test_first_step_fail_second_still_runs(self):
        # First step exhausts retries (3 failures), second step succeeds.
        # Executor runs all steps regardless of prior step failure.
        tools = MagicMock()
        tools.execute.side_effect = [
            _TR(False, "step 1 fail attempt 1"),
            _TR(False, "step 1 fail attempt 2"),
            _TR(False, "step 1 fail attempt 3"),
            _TR(True, "step 2 ok"),
        ]

        with patch("prometheus.planning.executor._try_verify") as mock_verify, \
             patch("prometheus.planning.executor._get_world_snapshot", return_value={}), \
             patch("time.sleep"):
            from prometheus.execution.verification import VerificationResult
            mock_verify.return_value = VerificationResult(
                verified=True, confidence=0.90, summary="ok",
                evidence=[], retry_recommended=False,
            )
            plan = Plan(
                intent="test multi",
                confidence=0.9,
                reason="test",
                steps=[PlanStep("read_file", {"path": "/bad"}), PlanStep("tell_time", {})],
            )
            ex = Executor(tools)
            result = ex.run(plan)

        assert len(result.steps) == 2
        assert not result.steps[0].ok    # step 1 failed after 3 retries
        assert result.steps[1].ok        # step 2 succeeded
        assert not result.all_ok         # because first step failed


# ── Integration: verify imports cleanly ──────────────────────────────────────

class TestImportIntegrity:
    def test_executor_imports(self):
        from prometheus.planning.executor import Executor, StepResult, ExecutionResult
        assert Executor is not None

    def test_try_verify_function_importable(self):
        from prometheus.planning.executor import _try_verify
        assert callable(_try_verify)

    def test_get_world_snapshot_function_importable(self):
        from prometheus.planning.executor import _get_world_snapshot
        assert callable(_get_world_snapshot)

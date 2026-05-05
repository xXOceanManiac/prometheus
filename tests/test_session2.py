"""
tests/test_session2.py — Session 2 test suite.

Tests GitSafety, SuccessCriteria, CodingAgent (mocked), and background dispatch.
All tests must pass. Run with:  python3 tests/test_session2.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

# Make sure we import from the project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from git_safety import GitSafety
from success_criteria import SuccessCriteria, SuccessCriteriaEngine
from coding_agent import CodingAgent, CodingResult, start_coding_task, get_coding_status

_PASS = "✅"
_FAIL = "❌"


def _run_test(name: str, fn) -> bool:
    try:
        fn()
        return True
    except AssertionError as exc:
        print(f"{_FAIL} {name} — AssertionError: {exc}")
        return False
    except Exception as exc:
        print(f"{_FAIL} {name} — {type(exc).__name__}: {exc}")
        return False


# ──────────────────────────────────────────────────────────
# Test 1 — GitSafety checkpoint
# ──────────────────────────────────────────────────────────

def test_1_checkpoint():
    gs = GitSafety()
    sha = gs.checkpoint("test-checkpoint")
    assert sha, "checkpoint() returned empty string"
    assert len(sha) == 8, f"expected 8-char SHA, got {len(sha)}: {sha!r}"
    assert all(c in "0123456789abcdef" for c in sha.lower()), f"SHA not hex: {sha!r}"

    # Confirm the commit message appears in git log
    import subprocess
    result = subprocess.run(
        ["git", "log", "--oneline", "-5"],
        capture_output=True, text=True,
        cwd=str(Path(__file__).parent.parent)
    )
    assert "prometheus-checkpoint" in result.stdout, (
        f"checkpoint commit not found in git log:\n{result.stdout}"
    )
    print(f"{_PASS} Test 1 — git checkpoint created: {sha}")


# ──────────────────────────────────────────────────────────
# Test 2 — GitSafety rollback
# ──────────────────────────────────────────────────────────

def test_2_rollback():
    import subprocess
    repo_root = Path(__file__).parent.parent

    gs = GitSafety()

    # Create a throwaway file and stage it
    target = repo_root / "test_rollback_target.txt"
    target.write_text("rollback test\n", encoding="utf-8")

    # Checkpoint includes this file
    sha = gs.checkpoint("rollback-test")
    assert sha, "checkpoint() failed before rollback test"

    # Remove the file and verify it's gone
    target.unlink()
    assert not target.exists(), "file should be deleted before rollback"

    # Rollback
    ok = gs.rollback(sha)
    assert ok, f"rollback() returned False for sha={sha}"

    # File should be restored
    assert target.exists(), f"file not restored after rollback to {sha}"

    # Cleanup: remove the file and commit to keep repo clean
    target.unlink()
    subprocess.run(["git", "add", "-A"], cwd=str(repo_root), capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "test: clean up rollback test file"],
        cwd=str(repo_root), capture_output=True
    )

    print(f"{_PASS} Test 2 — rollback restored file correctly")


# ──────────────────────────────────────────────────────────
# Test 3 — SuccessCriteria inference
# ──────────────────────────────────────────────────────────

def test_3_infer():
    engine = SuccessCriteriaEngine()

    cases = [
        ("fix the vault injection bug", "log"),
        ("write tests for memory_core", "test"),
        ("fix the import error on startup", "shell_exit"),
        ("create file new_module.py", "file_exists"),
        ("do something completely custom and unique", "manual"),
    ]

    for goal, expected_type in cases:
        c = engine.infer_from_goal(goal)
        assert c.check_type == expected_type, (
            f"goal={goal!r}: expected {expected_type!r}, got {c.check_type!r}"
        )
        print(f"  {_PASS} inferred {c.check_type} for '{goal}'")

    print(f"{_PASS} Test 3 — SuccessCriteria inference correct for all 5 goals")


# ──────────────────────────────────────────────────────────
# Test 4 — SuccessCriteria evaluate (log type)
# ──────────────────────────────────────────────────────────

def test_4_evaluate_log():
    engine = SuccessCriteriaEngine()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write('{"kind": "vault_context_injected", "ts": "2026-01-01"}\n')
        log_path = f.name

    try:
        criteria = SuccessCriteria(
            goal="fix vault injection",
            check_type="log",
            check_value="vault_context_injected",
        )
        result = engine.evaluate(criteria, log_path=log_path)
        assert result is True, "evaluate() should return True when event is in log"

        # Negative case
        criteria_miss = SuccessCriteria(
            goal="fix vault injection",
            check_type="log",
            check_value="nonexistent_event_xyz",
        )
        result_miss = engine.evaluate(criteria_miss, log_path=log_path)
        assert result_miss is False, "evaluate() should return False when event not in log"
    finally:
        os.unlink(log_path)

    print(f"{_PASS} Test 4 — log-type success criteria evaluated correctly")


# ──────────────────────────────────────────────────────────
# Test 5 — SuccessCriteria evaluate (test type)
# ──────────────────────────────────────────────────────────

def test_5_evaluate_test():
    engine = SuccessCriteriaEngine()

    # Passing command
    c_pass = SuccessCriteria(goal="test", check_type="test", check_value="echo ok")
    assert engine.evaluate(c_pass) is True, "echo ok should succeed"

    # Failing command
    c_fail = SuccessCriteria(goal="test", check_type="test", check_value="false")
    assert engine.evaluate(c_fail) is False, "'false' command should fail"

    print(f"{_PASS} Test 5 — test-type success criteria evaluated correctly")


# ──────────────────────────────────────────────────────────
# Test 6 — CodingAgent success path (mocked)
# ──────────────────────────────────────────────────────────

class _MockCodingAgentSuccess(CodingAgent):
    """Subclass that skips the real claude CLI call."""
    def _run_claude(self, prompt: str) -> str:
        return "done — task completed successfully"


def test_6_success_path():
    agent = _MockCodingAgentSuccess(max_retries=3, timeout=30)
    criteria = SuccessCriteria(
        goal="mock task",
        check_type="test",
        check_value="true",  # always succeeds
        description="shell 'true' exits 0",
    )
    result = agent.run(goal="mock task", criteria=criteria)

    assert result.success is True, f"expected success, got {result}"
    assert result.attempts == 1, f"expected 1 attempt, got {result.attempts}"
    assert len(result.checkpoint_sha) == 8, f"bad checkpoint SHA: {result.checkpoint_sha!r}"

    print(f"{_PASS} Test 6 — CodingAgent success path: 1 attempt, checkpoint {result.checkpoint_sha}")


# ──────────────────────────────────────────────────────────
# Test 7 — CodingAgent retry path (mocked)
# ──────────────────────────────────────────────────────────

class _MockCodingAgentRetry(CodingAgent):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._eval_call_count = 0

    def _run_claude(self, prompt: str) -> str:
        return "attempt output"

    def _evaluate(self, criteria, _output):
        self._eval_call_count += 1
        # Fail first two, succeed on third
        return self._eval_call_count >= 3


def test_7_retry_path():
    agent = _MockCodingAgentRetry(max_retries=3, timeout=30)
    criteria = SuccessCriteria(
        goal="retry task",
        check_type="manual",  # won't be used — _evaluate is overridden
        check_value="",
        description="mock retry",
    )
    result = agent.run(goal="retry task", criteria=criteria)

    assert result.success is True, f"expected success after retries, got {result}"
    assert result.attempts == 3, f"expected 3 attempts, got {result.attempts}"

    print(f"{_PASS} Test 7 — CodingAgent retry path: succeeded on attempt 3")


# ──────────────────────────────────────────────────────────
# Test 8 — CodingAgent rollback path (mocked)
# ──────────────────────────────────────────────────────────

class _MockCodingAgentRollback(CodingAgent):
    def _run_claude(self, prompt: str) -> str:
        return "failed attempt"

    def _evaluate(self, criteria, _output):
        return False  # always fails


def test_8_rollback_path():
    agent = _MockCodingAgentRollback(max_retries=2, timeout=30)
    criteria = SuccessCriteria(
        goal="rollback task",
        check_type="manual",
        check_value="",
        description="mock rollback",
    )
    result = agent.run(goal="rollback task", criteria=criteria)

    assert result.success is False, "expected failure"
    assert result.rolled_back is True, "expected rollback"
    assert result.attempts == 2, f"expected 2 attempts, got {result.attempts}"
    assert len(result.checkpoint_sha) == 8, f"bad checkpoint SHA: {result.checkpoint_sha!r}"

    print(f"{_PASS} Test 8 — CodingAgent rollback: 2 attempts, rolled back to {result.checkpoint_sha}")


# ──────────────────────────────────────────────────────────
# Test 9 — start_coding_task dispatches to background
# ──────────────────────────────────────────────────────────

def test_9_background_dispatch():
    # Clear any previous coding result
    from working_memory import WorkingMemory
    WorkingMemory().write({"last_coding_result": None})

    t0 = time.time()
    result = start_coding_task("write a hello world script")
    elapsed = time.time() - t0

    assert elapsed < 1.0, f"start_coding_task took {elapsed:.2f}s — should be immediate"
    assert result.get("status") == "started", f"expected 'started', got {result}"
    assert "goal" in result, "result missing 'goal'"
    assert "criteria" in result, "result missing 'criteria'"

    # Wait for background task to complete (it will run the real CodingAgent, which
    # will attempt 'claude' CLI — likely fails quickly since claude may not be installed)
    deadline = time.time() + 15.0
    status: dict = {}
    while time.time() < deadline:
        time.sleep(0.5)
        status = get_coding_status()
        if isinstance(status, dict) and status.get("status") != "no task running" and "completed_at" in status:
            break

    assert "completed_at" in status or status.get("status") != "no task running", (
        f"working_memory['last_coding_result'] not populated after 15s: {status}"
    )

    print(f"{_PASS} Test 9 — coding task dispatched to background, result available")


# ──────────────────────────────────────────────────────────
# Runner
# ──────────────────────────────────────────────────────────

def main():
    print("\n═══════════════════════════════════════════")
    print("  Prometheus Session 2 — Test Suite")
    print("═══════════════════════════════════════════\n")

    tests = [
        ("Test 1 — GitSafety checkpoint", test_1_checkpoint),
        ("Test 2 — GitSafety rollback", test_2_rollback),
        ("Test 3 — SuccessCriteria inference", test_3_infer),
        ("Test 4 — SuccessCriteria evaluate (log)", test_4_evaluate_log),
        ("Test 5 — SuccessCriteria evaluate (test)", test_5_evaluate_test),
        ("Test 6 — CodingAgent success path", test_6_success_path),
        ("Test 7 — CodingAgent retry path", test_7_retry_path),
        ("Test 8 — CodingAgent rollback path", test_8_rollback_path),
        ("Test 9 — Background dispatch", test_9_background_dispatch),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        ok = _run_test(name, fn)
        if ok:
            passed += 1
        else:
            failed += 1

    print()
    print("═══════════════════════════════════════════")
    print(f"  Results: {passed}/{len(tests)} passed", ("✅" if failed == 0 else "❌"))
    print("═══════════════════════════════════════════\n")

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()

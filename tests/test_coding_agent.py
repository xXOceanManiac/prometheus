"""
tests/test_coding_agent.py — GitSafety, SuccessCriteria, and CodingAgent.

All git operations run against a throwaway repository (temp_git_repo fixture)
and the claude CLI is never invoked — _run_claude is always mocked.
"""
from __future__ import annotations

import tempfile
import time

import pytest

from git_safety import GitSafety
from success_criteria import SuccessCriteria, SuccessCriteriaEngine
from coding_agent import CodingAgent, start_coding_task, get_coding_status


# ── GitSafety ─────────────────────────────────────────────────────────────────

class TestGitSafety:
    def test_checkpoint(self, temp_git_repo):
        gs = GitSafety(repo_root=temp_git_repo)
        sha = gs.checkpoint("test-checkpoint")
        assert sha, "checkpoint() returned empty string"
        assert len(sha) == 8, f"expected 8-char SHA, got {sha!r}"
        assert all(c in "0123456789abcdef" for c in sha.lower())

        import subprocess
        result = subprocess.run(
            ["git", "log", "--oneline", "-5"],
            capture_output=True, text=True, cwd=str(temp_git_repo),
        )
        assert "prometheus-checkpoint" in result.stdout

    def test_rollback_restores_file(self, temp_git_repo):
        gs = GitSafety(repo_root=temp_git_repo)

        target = temp_git_repo / "test_rollback_target.txt"
        target.write_text("rollback test\n", encoding="utf-8")

        sha = gs.checkpoint("rollback-test")
        assert sha, "checkpoint() failed before rollback test"

        target.unlink()
        assert not target.exists()

        assert gs.rollback(sha) is True
        assert target.exists(), f"file not restored after rollback to {sha}"

    def test_diff_since_and_current_sha(self, temp_git_repo):
        gs = GitSafety(repo_root=temp_git_repo)
        sha = gs.checkpoint("base")
        (temp_git_repo / "newfile.txt").write_text("hi\n", encoding="utf-8")
        gs.checkpoint("with newfile")
        assert "newfile.txt" in gs.diff_since(sha)
        assert len(gs.current_sha()) == 8


# ── SuccessCriteria ───────────────────────────────────────────────────────────

class TestSuccessCriteria:
    def test_infer_from_goal(self):
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

    def test_evaluate_log_type(self):
        engine = SuccessCriteriaEngine()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"kind": "vault_context_injected", "ts": "2026-01-01"}\n')
            log_path = f.name
        criteria = SuccessCriteria(
            goal="log check", check_type="log",
            check_value="vault_context_injected",
        )
        assert engine.evaluate(criteria, log_path=log_path) is True

        criteria_missing = SuccessCriteria(
            goal="log check", check_type="log",
            check_value="never_logged_event",
        )
        assert engine.evaluate(criteria_missing, log_path=log_path) is False

    def test_evaluate_test_type(self):
        engine = SuccessCriteriaEngine()
        c_pass = SuccessCriteria(goal="test", check_type="test", check_value="echo ok")
        assert engine.evaluate(c_pass) is True
        c_fail = SuccessCriteria(goal="test", check_type="test", check_value="false")
        assert engine.evaluate(c_fail) is False


# ── CodingAgent (claude CLI always mocked) ───────────────────────────────────

class _MockAgentSuccess(CodingAgent):
    def _run_claude(self, prompt: str) -> str:
        return "done — task completed successfully"


class _MockAgentRetry(CodingAgent):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._eval_call_count = 0

    def _run_claude(self, prompt: str) -> str:
        return "attempt output"

    def _evaluate(self, criteria, _output):
        self._eval_call_count += 1
        return self._eval_call_count >= 3


class _MockAgentAlwaysFails(CodingAgent):
    def _run_claude(self, prompt: str) -> str:
        return "failed attempt"

    def _evaluate(self, criteria, _output):
        return False


class TestCodingAgent:
    def test_success_path(self, temp_git_repo):
        agent = _MockAgentSuccess(
            git_safety=GitSafety(repo_root=temp_git_repo), max_retries=3, timeout=30
        )
        criteria = SuccessCriteria(
            goal="mock task", check_type="test", check_value="true",
            description="shell 'true' exits 0",
        )
        result = agent.run(goal="mock task", criteria=criteria)
        assert result.success is True
        assert result.attempts == 1
        assert len(result.checkpoint_sha) == 8

    def test_retry_path(self, temp_git_repo):
        agent = _MockAgentRetry(
            git_safety=GitSafety(repo_root=temp_git_repo), max_retries=3, timeout=30
        )
        criteria = SuccessCriteria(
            goal="retry task", check_type="manual", check_value="",
            description="mock retry",
        )
        result = agent.run(goal="retry task", criteria=criteria)
        assert result.success is True
        assert result.attempts == 3

    def test_rollback_path(self, temp_git_repo):
        agent = _MockAgentAlwaysFails(
            git_safety=GitSafety(repo_root=temp_git_repo), max_retries=2, timeout=30
        )
        criteria = SuccessCriteria(
            goal="rollback task", check_type="manual", check_value="",
            description="mock rollback",
        )
        result = agent.run(goal="rollback task", criteria=criteria)
        assert result.success is False
        assert result.rolled_back is True
        assert result.attempts == 2


# ── Background dispatch (no real CLI, no real repo) ──────────────────────────

class TestBackgroundDispatch:
    def test_start_coding_task_is_immediate_and_tracked(
        self, temp_git_repo, monkeypatch
    ):
        import coding_agent as ca_mod
        import git_safety as gs_mod

        # Isolate: checkpoints land in the temp repo, claude never runs
        monkeypatch.setattr(gs_mod, "_REPO_ROOT", temp_git_repo)
        monkeypatch.setattr(
            ca_mod.CodingAgent, "_run_claude",
            lambda self, prompt: "done — mocked", raising=True,
        )

        from working_memory import WorkingMemory
        WorkingMemory().write({"last_coding_result": None})

        t0 = time.time()
        result = start_coding_task("write a hello world script")
        elapsed = time.time() - t0

        assert elapsed < 1.0, f"start_coding_task took {elapsed:.2f}s"
        assert result.get("status") == "started"
        assert "goal" in result and "criteria" in result

        def _populated(s: dict) -> bool:
            # Either the intermediate {"status": "running", ...} record or the
            # final CodingResult dict (has attempts/completed_at, no "status").
            if not isinstance(s, dict) or s.get("status") == "no task running":
                return False
            return bool(
                s.get("status") or s.get("completed_at") or s.get("attempts")
            )

        deadline = time.time() + 5.0
        status: dict = {}
        while time.time() < deadline:
            time.sleep(0.1)
            status = get_coding_status()
            if _populated(status):
                break

        assert _populated(status), f"working memory never populated: {status}"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))

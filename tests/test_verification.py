"""
test_verification.py — Tests for VerificationResult and verify_action_result().
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from prometheus.execution.verification import verify_action_result, VerificationResult


# ── VerificationResult structure ─────────────────────────────────────────────

class TestVerificationResultStructure:
    def test_result_has_required_fields(self):
        r = verify_action_result("tell_time", "returns time", {"ok": True, "message": "10:30 AM"})
        assert hasattr(r, "verified")
        assert hasattr(r, "confidence")
        assert hasattr(r, "summary")
        assert hasattr(r, "evidence")
        assert hasattr(r, "retry_recommended")

    def test_verified_is_bool(self):
        r = verify_action_result("tell_time", "", {"ok": True, "message": "10:30"})
        assert isinstance(r.verified, bool)

    def test_confidence_is_float(self):
        r = verify_action_result("tell_time", "", {"ok": True, "message": "10:30"})
        assert isinstance(r.confidence, float)
        assert 0.0 <= r.confidence <= 1.0

    def test_evidence_is_list(self):
        r = verify_action_result("tell_time", "", {"ok": True, "message": "10:30"})
        assert isinstance(r.evidence, list)

    def test_summary_is_string(self):
        r = verify_action_result("tell_time", "", {"ok": True, "message": "10:30"})
        assert isinstance(r.summary, str)

    def test_retry_recommended_is_bool(self):
        r = verify_action_result("read_file", "", {"ok": False, "message": "File not found"})
        assert isinstance(r.retry_recommended, bool)


# ── tell_time verifier ────────────────────────────────────────────────────────

class TestTellTimeVerifier:
    def test_success(self):
        r = verify_action_result("tell_time", "returns time", {"ok": True, "message": "3:45 PM"})
        assert r.verified is True
        assert r.confidence >= 0.95

    def test_failure_no_message(self):
        r = verify_action_result("tell_time", "", {"ok": False, "message": ""})
        assert r.verified is False

    def test_no_retry_on_failure(self):
        r = verify_action_result("tell_time", "", {"ok": False, "message": ""})
        assert r.retry_recommended is False


# ── write_file verifier ───────────────────────────────────────────────────────

class TestWriteFileVerifier:
    def test_file_exists_on_disk(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("test content")
            path = f.name
        try:
            r = verify_action_result(
                "write_file", "file written",
                {"ok": True, "message": f"Wrote {path}", "data": {"path": path}},
            )
            assert r.verified is True
            assert r.confidence >= 0.95
        finally:
            Path(path).unlink(missing_ok=True)

    def test_file_missing_from_disk(self):
        r = verify_action_result(
            "write_file", "file written",
            {"ok": True, "message": "Wrote file", "data": {"path": "/tmp/nonexistent_xyz_12345.txt"}},
        )
        assert r.verified is False
        assert r.retry_recommended is True

    def test_no_path_falls_back_to_ok_flag(self):
        r = verify_action_result(
            "write_file", "file written",
            {"ok": True, "message": "Wrote file", "data": {}},
        )
        assert r.verified is True
        assert r.confidence < 0.85  # lower confidence without disk check

    def test_failure(self):
        r = verify_action_result(
            "write_file", "file written",
            {"ok": False, "message": "Write blocked: path outside workspace"},
        )
        assert r.verified is False
        assert r.retry_recommended is True


# ── screenshot verifier ───────────────────────────────────────────────────────

class TestScreenshotVerifier:
    def test_screenshot_file_exists(self):
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".png", delete=False) as f:
            f.write(b"\x89PNG" + b"\x00" * 2000)
            path = f.name
        try:
            r = verify_action_result(
                "screenshot", "file created",
                {"ok": True, "message": f"Saved {path}", "data": {"path": path}},
            )
            assert r.verified is True
            assert r.confidence >= 0.90
        finally:
            Path(path).unlink(missing_ok=True)

    def test_screenshot_ok_without_path(self):
        r = verify_action_result(
            "screenshot", "file created",
            {"ok": True, "message": "Screenshot saved", "data": {}},
        )
        assert r.verified is True
        assert r.confidence >= 0.70

    def test_screenshot_failure(self):
        r = verify_action_result(
            "screenshot", "file created",
            {"ok": False, "message": "Display not available"},
        )
        assert r.verified is False


# ── run_shell verifier ────────────────────────────────────────────────────────

class TestRunShellVerifier:
    def test_success_exit_zero(self):
        r = verify_action_result(
            "run_shell", "command exits 0",
            {"ok": True, "message": "ok", "data": {"output": "total 24\ndrwxr-xr-x ..."}},
        )
        assert r.verified is True
        assert r.confidence >= 0.85

    def test_failure_nonzero(self):
        r = verify_action_result(
            "run_shell", "command exits 0",
            {"ok": False, "message": "Command failed", "data": {"output": "command not found"}},
        )
        assert r.verified is False
        assert r.retry_recommended is True

    def test_evidence_includes_output(self):
        r = verify_action_result(
            "run_shell", "command exits 0",
            {"ok": True, "data": {"output": "my output"}},
        )
        assert any("my output" in e for e in r.evidence)


# ── run_python verifier ───────────────────────────────────────────────────────

class TestRunPythonVerifier:
    def test_success(self):
        r = verify_action_result(
            "run_python", "exits 0",
            {"ok": True, "data": {"output": "Hello, world!"}},
        )
        assert r.verified is True

    def test_failure(self):
        r = verify_action_result(
            "run_python", "exits 0",
            {"ok": False, "message": "SyntaxError at line 5"},
        )
        assert r.verified is False
        assert r.retry_recommended is True


# ── git verifiers ─────────────────────────────────────────────────────────────

class TestGitVerifiers:
    def test_git_commit_success(self):
        r = verify_action_result(
            "git_commit", "commit created",
            {"ok": True, "message": "ok", "data": {"output": "[main abc1234] Add feature"}},
        )
        assert r.verified is True
        assert r.confidence >= 0.90

    def test_git_commit_failure_no_retry(self):
        r = verify_action_result(
            "git_commit", "commit created",
            {"ok": False, "message": "Nothing to commit"},
        )
        assert r.verified is False
        assert r.retry_recommended is False  # commits don't auto-retry

    def test_git_status_success(self):
        r = verify_action_result(
            "git_status", "returns output",
            {"ok": True, "data": {"output": "M  realtime_client.py"}},
        )
        assert r.verified is True

    def test_git_diff_success(self):
        r = verify_action_result(
            "git_diff", "returns diff",
            {"ok": True, "data": {"output": "diff --git ..."}},
        )
        assert r.verified is True


# ── open_app verifier ─────────────────────────────────────────────────────────

class TestOpenAppVerifier:
    def test_app_visible_in_windows(self):
        snap = {"open_windows": ["Firefox — My Page", "VS Code — Prometheus"]}
        r = verify_action_result(
            "open_app", "app running",
            {"ok": True, "message": "Launched firefox", "data": {"app": "firefox"}},
            snap,
        )
        assert r.verified is True
        assert r.confidence >= 0.90

    def test_app_not_in_windows_but_ok(self):
        snap = {"open_windows": ["Konsole"]}
        r = verify_action_result(
            "open_app", "app running",
            {"ok": True, "message": "Launched firefox", "data": {"app": "firefox"}},
            snap,
        )
        assert r.verified is True
        assert r.confidence < 0.80

    def test_app_launch_failed(self):
        r = verify_action_result(
            "open_app", "app running",
            {"ok": False, "message": "App not found: fakeapp"},
        )
        assert r.verified is False
        assert r.retry_recommended is True


# ── list_files verifier ───────────────────────────────────────────────────────

class TestListFilesVerifier:
    def test_with_files(self):
        r = verify_action_result(
            "list_files", "returns files",
            {"ok": True, "data": {"files": ["main.py", "tools.py", "config.py"]}},
        )
        assert r.verified is True
        assert r.confidence >= 0.90

    def test_ok_no_files(self):
        r = verify_action_result(
            "list_files", "returns files",
            {"ok": True, "data": {}},
        )
        assert r.verified is True
        assert r.confidence < 0.90

    def test_failure(self):
        r = verify_action_result(
            "list_files", "returns files",
            {"ok": False, "message": "Path not found"},
        )
        assert r.verified is False


# ── read_file verifier ────────────────────────────────────────────────────────

class TestReadFileVerifier:
    def test_with_content(self):
        r = verify_action_result(
            "read_file", "returns content",
            {"ok": True, "data": {"content": "# My readme\n\nThis is the project."}},
        )
        assert r.verified is True
        assert r.confidence >= 0.90

    def test_ok_no_content(self):
        r = verify_action_result(
            "read_file", "returns content",
            {"ok": True, "message": "File is empty", "data": {}},
        )
        assert r.verified is True
        assert r.confidence < 0.80

    def test_failure(self):
        r = verify_action_result(
            "read_file", "returns content",
            {"ok": False, "message": "File not found"},
        )
        assert r.verified is False
        assert r.retry_recommended is True


# ── web_search verifier ───────────────────────────────────────────────────────

class TestWebSearchVerifier:
    def test_with_summary(self):
        r = verify_action_result(
            "web_search", "returns result",
            {"ok": True, "data": {"summary": "Python async patterns include asyncio..."}},
        )
        assert r.verified is True
        assert r.confidence >= 0.85

    def test_ok_no_summary(self):
        r = verify_action_result(
            "web_search", "returns result",
            {"ok": True, "data": {}},
        )
        assert r.verified is True
        assert r.confidence < 0.75

    def test_failure(self):
        r = verify_action_result(
            "web_search", "returns result",
            {"ok": False, "message": "Search timeout"},
        )
        assert r.verified is False


# ── Mission state verifiers ───────────────────────────────────────────────────

class TestMissionVerifiers:
    def test_set_mission_success(self):
        r = verify_action_result(
            "set_mission", "mission updated",
            {"ok": True, "message": "Mission set"},
        )
        assert r.verified is True

    def test_add_subtask_success(self):
        r = verify_action_result(
            "add_subtask", "subtask added",
            {"ok": True, "message": "Subtask added"},
        )
        assert r.verified is True

    def test_complete_subtask_failure(self):
        r = verify_action_result(
            "complete_subtask", "subtask complete",
            {"ok": False, "message": "Subtask not found"},
        )
        assert r.verified is False
        assert r.retry_recommended is True


# ── Generic / unknown tool ────────────────────────────────────────────────────

class TestGenericVerifier:
    def test_unknown_tool_ok(self):
        r = verify_action_result(
            "unknown_tool_xyz", "something happened",
            {"ok": True, "message": "Done"},
        )
        assert r.verified is True
        assert r.confidence >= 0.70

    def test_unknown_tool_failure(self):
        r = verify_action_result(
            "unknown_tool_xyz", "something happened",
            {"ok": False, "message": "Error occurred"},
        )
        assert r.verified is False
        assert r.retry_recommended is True


# ── Error resilience ─────────────────────────────────────────────────────────

class TestErrorResilience:
    def test_none_execution_result_doesnt_crash(self):
        r = verify_action_result("tell_time", "", {})
        assert isinstance(r, VerificationResult)

    def test_missing_ok_key_doesnt_crash(self):
        r = verify_action_result("tell_time", "", {"message": "10:30"})
        assert isinstance(r, VerificationResult)
        assert r.verified is False

    def test_none_snapshot_doesnt_crash(self):
        r = verify_action_result("open_app", "app running", {"ok": True}, None)
        assert isinstance(r, VerificationResult)

    def test_empty_tool_name_doesnt_crash(self):
        r = verify_action_result("", "", {"ok": True})
        assert isinstance(r, VerificationResult)

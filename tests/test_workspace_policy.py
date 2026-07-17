"""
test_workspace_policy.py — Workspace safety tests.

Verifies that write_file is restricted to runtime/workspace and that
resolve_workspace_path correctly enforces the boundary.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from prometheus.infra.paths import (
    PROJECT_ROOT,
    RUNTIME_ROOT,
    REPORTS_DIR,
    WORKSPACE_ROOT,
    LOGS_DIR,
)
from prometheus.execution.workspace_policy import resolve_workspace_path, ensure_workspace_root


# ── Path constant structure tests ─────────────────────────────────────────────

class TestPathConstants:
    def test_project_root_is_correct(self):
        assert PROJECT_ROOT == Path(__file__).resolve().parent.parent

    def test_runtime_root_inside_project(self):
        assert PROJECT_ROOT in RUNTIME_ROOT.parents

    def test_workspace_root_inside_runtime(self):
        assert RUNTIME_ROOT in WORKSPACE_ROOT.parents

    def test_reports_dir_inside_runtime(self):
        assert RUNTIME_ROOT in REPORTS_DIR.parents

    def test_logs_dir_inside_runtime(self):
        assert RUNTIME_ROOT in LOGS_DIR.parents

    def test_workspace_root_name(self):
        assert WORKSPACE_ROOT.name == "workspace"
        assert WORKSPACE_ROOT.parent.name == "runtime"

    def test_reports_dir_name(self):
        assert REPORTS_DIR.name == "reports"
        assert REPORTS_DIR.parent.name == "runtime"


# ── resolve_workspace_path tests ──────────────────────────────────────────────

class TestResolveWorkspacePath:
    def test_relative_path_lands_in_workspace(self):
        p = resolve_workspace_path("myproject/main.py")
        assert p == (WORKSPACE_ROOT / "myproject" / "main.py").resolve()
        assert WORKSPACE_ROOT.resolve() in p.parents

    def test_absolute_path_inside_workspace_succeeds(self):
        target = WORKSPACE_ROOT / "allowed" / "file.txt"
        p = resolve_workspace_path(str(target))
        assert p == target.resolve()

    def test_absolute_path_outside_workspace_fails(self):
        with pytest.raises(PermissionError):
            resolve_workspace_path("/etc/passwd")

    def test_home_dir_path_outside_workspace_fails(self):
        home_path = str(Path.home() / "Desktop" / "test.py")
        with pytest.raises(PermissionError):
            resolve_workspace_path(home_path)

    def test_path_traversal_fails(self):
        with pytest.raises(PermissionError):
            resolve_workspace_path("../../Desktop/test.py")

    def test_jarvis_dir_blocked(self):
        jarvis_path = str(Path.home() / ".jarvis" / "config.json")
        with pytest.raises(PermissionError):
            resolve_workspace_path(jarvis_path)

    def test_empty_path_raises_value_error(self):
        with pytest.raises(ValueError):
            resolve_workspace_path("")

    def test_none_raises_value_error(self):
        with pytest.raises(ValueError):
            resolve_workspace_path(None)

    def test_workspace_root_itself_is_allowed(self):
        p = resolve_workspace_path(str(WORKSPACE_ROOT))
        assert p == WORKSPACE_ROOT.resolve()

    def test_tilde_expansion_inside_workspace(self):
        workspace_rel = str(WORKSPACE_ROOT / "script.sh").replace(str(Path.home()), "~")
        p = resolve_workspace_path(workspace_rel)
        assert p == (WORKSPACE_ROOT / "script.sh").resolve()


# ── ensure_workspace_root tests ───────────────────────────────────────────────

class TestEnsureWorkspaceRoot:
    def test_creates_workspace_if_missing(self, tmp_path, monkeypatch):
        import prometheus.execution.workspace_policy as wp
        fake_root = tmp_path / "workspace"
        monkeypatch.setattr(wp, "WORKSPACE_ROOT", fake_root)
        result = ensure_workspace_root()
        assert fake_root.exists()
        assert fake_root.is_dir()

    def test_idempotent_when_already_exists(self, tmp_path, monkeypatch):
        import prometheus.execution.workspace_policy as wp
        fake_root = tmp_path / "workspace"
        fake_root.mkdir()
        monkeypatch.setattr(wp, "WORKSPACE_ROOT", fake_root)
        result = ensure_workspace_root()
        assert fake_root.exists()


# ── write_file tool integration tests ─────────────────────────────────────────

class TestWriteFileTool:
    """Integration tests against the write_file action in ToolRegistry."""

    @pytest.fixture
    def registry(self):
        from prometheus.execution.tools import ToolRegistry
        return ToolRegistry()

    def test_relative_write_lands_in_workspace(self, registry, tmp_path, monkeypatch):
        import prometheus.execution.workspace_policy as wp
        fake_root = tmp_path / "workspace"
        monkeypatch.setattr(wp, "WORKSPACE_ROOT", fake_root)

        result = registry.execute({
            "action": "write_file",
            "path": "hello.txt",
            "content": "hello world",
        })
        assert result.ok, result.message
        written = fake_root / "hello.txt"
        assert written.exists()
        assert written.read_text() == "hello world"

    def test_absolute_inside_workspace_succeeds(self, registry, tmp_path, monkeypatch):
        import prometheus.execution.workspace_policy as wp
        fake_root = tmp_path / "workspace"
        fake_root.mkdir(parents=True)
        monkeypatch.setattr(wp, "WORKSPACE_ROOT", fake_root)

        target = str(fake_root / "sub" / "file.py")
        result = registry.execute({
            "action": "write_file",
            "path": target,
            "content": "# code",
        })
        assert result.ok, result.message

    def test_absolute_outside_workspace_blocked(self, registry, tmp_path, monkeypatch):
        import prometheus.execution.workspace_policy as wp
        fake_root = tmp_path / "workspace"
        monkeypatch.setattr(wp, "WORKSPACE_ROOT", fake_root)

        result = registry.execute({
            "action": "write_file",
            "path": "/tmp/evil.py",
            "content": "rm -rf /",
        })
        assert not result.ok
        assert "blocked" in result.message.lower() or "outside" in result.message.lower()

    def test_cannot_write_to_jarvis_dir(self, registry, tmp_path, monkeypatch):
        import prometheus.execution.workspace_policy as wp
        fake_root = tmp_path / "workspace"
        monkeypatch.setattr(wp, "WORKSPACE_ROOT", fake_root)

        jarvis_path = str(Path.home() / ".jarvis" / "config.json")
        result = registry.execute({
            "action": "write_file",
            "path": jarvis_path,
            "content": "{}",
        })
        assert not result.ok

    def test_path_traversal_blocked(self, registry, tmp_path, monkeypatch):
        import prometheus.execution.workspace_policy as wp
        fake_root = tmp_path / "workspace"
        monkeypatch.setattr(wp, "WORKSPACE_ROOT", fake_root)

        result = registry.execute({
            "action": "write_file",
            "path": "../../Desktop/injected.py",
            "content": "malicious",
        })
        assert not result.ok

    def test_missing_path_blocked(self, registry):
        result = registry.execute({
            "action": "write_file",
            "path": "",
            "content": "data",
        })
        assert not result.ok

    def test_workspace_root_created_if_missing(self, registry, tmp_path, monkeypatch):
        import prometheus.execution.workspace_policy as wp
        fake_root = tmp_path / "new_workspace"
        assert not fake_root.exists()
        monkeypatch.setattr(wp, "WORKSPACE_ROOT", fake_root)

        result = registry.execute({
            "action": "write_file",
            "path": "init.txt",
            "content": "init",
        })
        assert result.ok
        assert fake_root.exists()

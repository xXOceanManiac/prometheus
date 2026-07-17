"""Shared pytest fixtures for the Prometheus test suite."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# Ensure the project root is importable regardless of how pytest is invoked.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture
def temp_git_repo(tmp_path: Path) -> Path:
    """A throwaway git repository with one initial commit.

    Any test that exercises GitSafety, CodingAgent, or Orchestrator MUST use
    this (directly or via monkeypatching git_safety._REPO_ROOT) so checkpoint
    commits never land in the real Prometheus repository.
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=str(repo), check=True, capture_output=True
        )

    _git("init", "-q")
    _git("config", "user.email", "test@prometheus.local")
    _git("config", "user.name", "Prometheus Test")
    (repo / "README.md").write_text("test repo\n", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-q", "-m", "initial")
    return repo

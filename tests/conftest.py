"""Shared pytest fixtures for the Prometheus test suite.

Hermetic guarantee: this file runs before any prometheus module is imported,
and everything below keeps the suite away from real state and paid services:

- HOME is redirected to a throwaway directory, so every module-level path
  built from Path.home() (~/.jarvis state, memory_v2, dashboard state, logs)
  lands in the sandbox instead of the real machine.
- API keys are pre-set to empty strings so load_dotenv(.env) cannot inject
  the real ones (dotenv never overrides existing environment variables).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# ── Hermetic sandbox — MUST run before any prometheus import ─────────────────

LIVE_MODE = (
    os.environ.get("PROMETHEUS_LIVE_TESTS", "").strip().lower() in ("1", "true", "yes")
)
REAL_HOME = os.environ.get("HOME", "")

if not LIVE_MODE:
    _HERMETIC_HOME = tempfile.mkdtemp(prefix="prometheus-test-home-")
    os.environ["HOME"] = _HERMETIC_HOME
    for _secret in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "HOME_ASSISTANT_API_KEY",
        "PORCUPINE_ACCESS_KEY",
        "VITE_GUARDIAN_API_KEY",
    ):
        os.environ[_secret] = ""

# Ensure the project root is importable regardless of how pytest is invoked.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def pytest_collection_modifyitems(config, items):
    """Keep the two suites separate:

    - default (hermetic) run: live smoke tests are skipped
    - live run (PROMETHEUS_LIVE_TESTS=1): ONLY live smoke tests run, so the
      hermetic suite can never touch real state by accident
    """
    if LIVE_MODE:
        skip_hermetic = pytest.mark.skip(
            reason="hermetic test — not run in live mode (unset PROMETHEUS_LIVE_TESTS)"
        )
        for item in items:
            if "live" not in [m.name for m in item.iter_markers()]:
                item.add_marker(skip_hermetic)
        return
    skip_live = pytest.mark.skip(reason="live smoke test — set PROMETHEUS_LIVE_TESTS=1 to run")
    for item in items:
        if "live" in [m.name for m in item.iter_markers()]:
            item.add_marker(skip_live)


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

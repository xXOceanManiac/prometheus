"""
git_safety.py — Git checkpoint, rollback, and diff utilities for Prometheus.

Used by CodingAgent to create safe restore points before autonomous coding runs.
All methods operate on the repo containing this file. Never raises — logs and
returns gracefully on any failure.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from utils import log_event

# Root of the repo — git_safety.py lives at the project root
_REPO_ROOT = Path(__file__).resolve().parent


def _git(*args: str, cwd: Path = _REPO_ROOT) -> subprocess.CompletedProcess:
    """Run a git command, capturing output. Returns CompletedProcess regardless of exit code."""
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
    )


class GitSafety:
    """
    Thin wrapper around git for safe checkpointing and rollback.
    All methods are synchronous and safe to call from any thread.

    repo_root defaults to the Prometheus repo; tests must pass a
    throwaway repo so checkpoints never land in real history.
    """

    def __init__(self, repo_root: Path | None = None) -> None:
        self._root = Path(repo_root) if repo_root else _REPO_ROOT

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return _git(*args, cwd=self._root)

    def checkpoint(self, label: str) -> str:
        """
        Stage all changes and create a checkpoint commit.

        Returns the short (8-char) SHA of the created commit.
        If nothing to commit (clean tree), creates an empty commit.
        Returns "" on unexpected failure.
        """
        safe_label = str(label or "checkpoint")[:80].strip()
        message = f"prometheus-checkpoint: {safe_label} [{time.strftime('%Y-%m-%dT%H:%M:%S')}]"

        try:
            # Stage everything that is tracked or untracked
            self._git("add", "-A")

            # Attempt a real commit; if nothing to stage, do an empty commit
            result = self._git("commit", "-m", message)
            if result.returncode != 0 and "nothing to commit" in result.stdout + result.stderr:
                result = self._git("commit", "--allow-empty", "-m", message)

            if result.returncode != 0:
                log_event("git_checkpoint_error", {
                    "label": safe_label,
                    "stderr": result.stderr.strip()[:200],
                })
                return ""

            sha = self.current_sha()
            log_event("git_checkpoint_created", {"sha": sha, "label": safe_label})
            return sha

        except Exception as exc:
            log_event("git_checkpoint_exception", {"label": safe_label, "error": str(exc)[:200]})
            return ""

    def rollback(self, sha: str) -> bool:
        """
        Hard-reset HEAD to the given SHA, discarding all subsequent changes.

        Returns True on success, False on failure.
        """
        sha = str(sha or "").strip()
        if not sha:
            log_event("git_rollback_error", {"reason": "empty sha"})
            return False

        try:
            result = self._git("reset", "--hard", sha)
            ok = result.returncode == 0
            log_event("git_rollback", {
                "sha": sha,
                "success": ok,
                "output": (result.stdout + result.stderr).strip()[:200],
            })
            return ok
        except Exception as exc:
            log_event("git_rollback_exception", {"sha": sha, "error": str(exc)[:200]})
            return False

    def current_sha(self) -> str:
        """Return the short (8-char) SHA of the current HEAD. Returns '' on failure."""
        try:
            result = self._git("rev-parse", "--short=8", "HEAD")
            if result.returncode == 0:
                return result.stdout.strip()
            return ""
        except Exception:
            return ""

    def diff_since(self, sha: str) -> str:
        """
        Return a --stat summary of changes between sha and HEAD.
        Safe to call even if sha == HEAD (returns empty string).
        """
        sha = str(sha or "").strip()
        if not sha:
            return ""
        try:
            result = self._git("diff", "--stat", sha, "HEAD")
            if result.returncode == 0:
                return result.stdout.strip()
            return ""
        except Exception:
            return ""

    def last_checkpoint_sha(self) -> str | None:
        """
        Return the SHA of the most recent prometheus-checkpoint commit.
        Returns None if no such commits exist.
        """
        try:
            result = self._git(
                "log",
                "--oneline",
                "--all",
                "--grep=prometheus-checkpoint:",
                "-1",
            )
            if result.returncode == 0 and result.stdout.strip():
                # Output: "abc12345 prometheus-checkpoint: ..."
                return result.stdout.strip().split()[0]
            return None
        except Exception:
            return None

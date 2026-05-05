"""
coding_agent.py — Autonomous coding loop for Prometheus.

CodingAgent runs Claude Code headlessly, evaluates success criteria,
retries on failure with accumulated context, and rolls back on exhaustion.
"""
from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from git_safety import GitSafety
from success_criteria import SuccessCriteria, SuccessCriteriaEngine
from utils import log_event
from working_memory import WorkingMemory

# Path to today's Prometheus log file — used for "log" type criteria
_LOG_DIR = Path.home() / ".jarvis" / "logs"


def _today_log_path() -> str:
    path = _LOG_DIR / f"{time.strftime('%Y-%m-%d')}.jsonl"
    return str(path)


@dataclass
class CodingResult:
    success: bool
    attempts: int
    diff: str = ""
    output: str = ""
    rolled_back: bool = False
    checkpoint_sha: str = ""


class CodingAgent:
    """
    Autonomous coding agent that uses Claude Code to fulfil a goal.

    Lifecycle per run():
      1. Git checkpoint
      2. Build prompt from goal + context + criteria
      3. Run claude CLI headlessly
      4. Evaluate success criteria
      5. If success → return result
      6. If failure and retries remain → append failure context, retry
      7. If retries exhausted → rollback + return failure
    """

    def __init__(
        self,
        git_safety: GitSafety | None = None,
        max_retries: int = 3,
        timeout: int = 300,
    ) -> None:
        self.git_safety = git_safety or GitSafety()
        self.max_retries = max_retries
        self.timeout = timeout
        self._criteria_engine = SuccessCriteriaEngine()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        goal: str,
        criteria: SuccessCriteria,
        context: str = "",
    ) -> CodingResult:
        """
        Execute the autonomous coding loop.

        Args:
            goal:     Natural language description of what to accomplish.
            criteria: How to determine success.
            context:  Optional extra context to include in the Claude prompt.

        Returns a CodingResult describing outcome, attempts, and diff.
        """
        checkpoint_sha = self.git_safety.checkpoint(label=goal[:50])
        log_event("coding_agent_run_start", {
            "goal": goal[:80],
            "criteria_type": criteria.check_type,
            "checkpoint_sha": checkpoint_sha,
        })

        prompt = self._build_prompt(goal, criteria, context)
        last_output = ""

        for attempt in range(1, self.max_retries + 1):
            log_event("coding_agent_attempt", {
                "attempt": attempt,
                "goal": goal[:80],
                "sha": self.git_safety.current_sha(),
            })

            output = self._run_claude(prompt)
            last_output = output

            if self._evaluate(criteria, output):
                diff = self.git_safety.diff_since(checkpoint_sha)
                log_event("coding_agent_success", {
                    "attempts": attempt,
                    "diff_stat": diff[:200],
                })
                return CodingResult(
                    success=True,
                    attempts=attempt,
                    diff=diff,
                    output=output,
                    checkpoint_sha=checkpoint_sha,
                )

            # Failed this attempt
            log_event("coding_agent_failure", {
                "attempt": attempt,
                "reason": output[-500:] if output else "no output",
            })

            if attempt < self.max_retries:
                # Enrich prompt with failure context for the next attempt
                failure_summary = output[-2000:] if output else "(no output)"
                prompt = self._build_prompt(
                    goal, criteria, context,
                    failure_context=f"Previous attempt {attempt} failed. Output was:\n{failure_summary}",
                )
                log_event("coding_agent_retry", {
                    "attempt": attempt,
                    "reason": output[-200:] if output else "no output",
                })

        # Exhausted retries
        log_event("coding_agent_max_retries_hit", {
            "goal": goal[:80],
            "attempts": self.max_retries,
        })

        rolled_back = False
        if checkpoint_sha:
            rolled_back = self.git_safety.rollback(checkpoint_sha)
            if rolled_back:
                log_event("coding_agent_rolled_back", {"sha": checkpoint_sha})

        return CodingResult(
            success=False,
            attempts=self.max_retries,
            output=last_output,
            rolled_back=rolled_back,
            checkpoint_sha=checkpoint_sha,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        goal: str,
        criteria: SuccessCriteria,
        context: str,
        failure_context: str = "",
    ) -> str:
        parts: list[str] = [
            f"Goal: {goal}",
            "",
            f"Success criteria: {criteria.description or criteria.check_type + ':' + criteria.check_value}",
            "",
        ]
        if context:
            parts.extend(["Context:", context, ""])
        if failure_context:
            parts.extend(["Note:", failure_context, ""])
        parts.append(
            "Complete the goal above. Make only the changes required. "
            "Do not touch unrelated code. When done, verify the change works."
        )
        return "\n".join(parts)

    def _run_claude(self, prompt: str) -> str:
        """
        Invoke the claude CLI in headless mode and return its combined output.
        Returns stdout+stderr combined. Never raises.
        """
        try:
            result = subprocess.run(
                ["claude", "--print", "--dangerously-skip-permissions", prompt],
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            combined = (result.stdout or "") + (result.stderr or "")
            log_event("coding_agent_claude_exit", {"returncode": result.returncode, "output_len": len(combined)})
            return combined
        except subprocess.TimeoutExpired:
            log_event("coding_agent_timeout", {"timeout": self.timeout})
            return f"[TIMEOUT after {self.timeout}s]"
        except FileNotFoundError:
            log_event("coding_agent_claude_not_found", {})
            return "[ERROR: claude CLI not found on PATH]"
        except Exception as exc:
            log_event("coding_agent_claude_error", {"error": str(exc)[:200]})
            return f"[ERROR: {exc}]"

    def _evaluate(self, criteria: SuccessCriteria, _output: str) -> bool:
        """Delegate to SuccessCriteriaEngine. _output unused directly but available for future hooks."""
        return self._criteria_engine.evaluate(criteria, log_path=_today_log_path())


# ------------------------------------------------------------------
# Background dispatch helpers (used by the tools layer)
# ------------------------------------------------------------------

def _run_coding_task_background(goal: str, context: str) -> None:
    """
    Entry point for the background thread. Runs CodingAgent and stores
    the result in WorkingMemory under "last_coding_result".
    """
    try:
        engine = SuccessCriteriaEngine()
        criteria = engine.infer_from_goal(goal)
        agent = CodingAgent()
        result = agent.run(goal=goal, criteria=criteria, context=context)

        wm_data: dict[str, Any] = {
            "success": result.success,
            "attempts": result.attempts,
            "diff": result.diff[:1000],
            "output_tail": result.output[-1000:] if result.output else "",
            "rolled_back": result.rolled_back,
            "checkpoint_sha": result.checkpoint_sha,
            "goal": goal[:120],
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        WorkingMemory().write({"last_coding_result": wm_data})
        log_event("coding_task_complete", {
            "goal": goal[:80],
            "success": result.success,
            "attempts": result.attempts,
        })
    except Exception as exc:
        log_event("coding_task_background_error", {"error": str(exc)[:200], "goal": goal[:80]})
        WorkingMemory().write({
            "last_coding_result": {
                "success": False,
                "error": str(exc)[:200],
                "goal": goal[:120],
                "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        })


def start_coding_task(goal: str, context: str = "") -> dict[str, Any]:
    """
    Dispatch a coding task to a background thread.
    Returns immediately with {"status": "started", "goal": goal, "criteria": description}.
    """
    goal = str(goal or "").strip()
    if not goal:
        return {"status": "error", "reason": "goal is required"}

    criteria = SuccessCriteriaEngine().infer_from_goal(goal)
    log_event("coding_task_dispatched", {"goal": goal[:80], "criteria_type": criteria.check_type})

    t = threading.Thread(
        target=_run_coding_task_background,
        args=(goal, context),
        daemon=True,
        name="coding-agent",
    )
    t.start()

    return {
        "status": "started",
        "goal": goal,
        "criteria": criteria.description or f"{criteria.check_type}: {criteria.check_value}",
    }


def get_coding_status() -> dict[str, Any]:
    """Return the most recent coding task result from WorkingMemory."""
    wm = WorkingMemory().read()
    return wm.get("last_coding_result", {"status": "no task running"})

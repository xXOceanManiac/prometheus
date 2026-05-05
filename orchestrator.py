"""
orchestrator.py — Prometheus build orchestrator.

Orchestrates Architect → Coder → Tester → Debugger loop to autonomously
build features with automated verification and git safety.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from agent_base import AgentTask
from agents.architect import ArchitectAgent
from agents.coder import CoderAgent
from agents.tester import TesterAgent
from agents.debugger import DebuggerAgent
from git_safety import GitSafety
from success_criteria import SuccessCriteria
from utils import log_event
from working_memory import WorkingMemory


@dataclass
class OrchestrationResult:
    """Full outcome of an orchestrated build run."""
    goal: str
    success: bool
    phases_completed: list[str] = field(default_factory=list)
    test_results: dict[str, int] = field(default_factory=dict)
    diff: str = ""
    duration_seconds: float = 0.0
    needs_human: bool = False
    checkpoint_sha: str = ""
    agent_outputs: dict[str, str] = field(default_factory=dict)


class Orchestrator:
    """
    Coordinates a pipeline of specialized agents to build features autonomously.

    Pipeline:
        1. Git checkpoint
        2. ArchitectAgent  → JSON build plan
        3. CoderAgent      → implements the plan (receives plan as context)
        4. TesterAgent     → writes + runs tests (receives coder output as context)
        5. Debug loop (max max_debug_cycles):
               DebuggerAgent  → fixes failures
               TesterAgent    → re-runs tests
        6. OrchestrationResult

    Rollback policy:
        - If Architect fails (no valid plan), rollback immediately.
        - If debug limit is exhausted, do NOT rollback (partial progress preserved).
          Set needs_human=True instead.
    """

    def __init__(
        self,
        max_debug_cycles: int = 3,
        architect: ArchitectAgent | None = None,
        coder: CoderAgent | None = None,
        tester: TesterAgent | None = None,
        debugger: DebuggerAgent | None = None,
        git_safety: GitSafety | None = None,
        cost_tracker: Any | None = None,
    ) -> None:
        self.max_debug_cycles = max_debug_cycles
        self._architect = architect or ArchitectAgent()
        self._coder = coder or CoderAgent()
        self._tester = tester or TesterAgent()
        self._debugger = debugger or DebuggerAgent()
        self._git = git_safety or GitSafety()
        self._cost_tracker = cost_tracker

    def run(self, goal: str, context: str = "") -> OrchestrationResult:
        """
        Execute the full build pipeline for the given goal.

        Returns OrchestrationResult — never raises.
        """
        t0 = time.monotonic()
        phases: list[str] = []
        agent_outputs: dict[str, str] = {}

        # ── Phase 0: Git checkpoint ──────────────────────────────────────
        checkpoint_sha = self._git.checkpoint(label=goal[:50])
        log_event("orchestrator_start", {
            "goal": goal[:80],
            "checkpoint_sha": checkpoint_sha,
        })

        # ── Phase 1: Architect ───────────────────────────────────────────
        cost_abort = self._check_cost()
        if cost_abort:
            return OrchestrationResult(
                goal=goal,
                success=False,
                phases_completed=["cost_limit_abort"],
                checkpoint_sha=checkpoint_sha,
                duration_seconds=round(time.monotonic() - t0, 1),
                agent_outputs={"abort": cost_abort},
            )
        arch_task = AgentTask(goal=goal, context=context)
        arch_result = self._architect.run(arch_task)
        agent_outputs["architect"] = arch_result.output

        if not arch_result.success:
            # Plan parse failure — rollback immediately
            log_event("orchestrator_architect_failed", {"goal": goal[:80]})
            if checkpoint_sha:
                self._git.rollback(checkpoint_sha)
            return OrchestrationResult(
                goal=goal,
                success=False,
                phases_completed=["architect_failed"],
                checkpoint_sha=checkpoint_sha,
                duration_seconds=round(time.monotonic() - t0, 1),
                agent_outputs=agent_outputs,
            )

        phases.append("architect")
        plan = self._architect.parse_plan(arch_result.output)
        plan_json = arch_result.output  # pass raw output as context to coder

        files_of_interest: list[str] = []
        test_command = "python -m pytest tests/ -x -q --tb=short"
        if plan:
            files_of_interest = plan.get("files_of_interest", [])
            test_command = plan.get("test_command", test_command)

        log_event("orchestrator_architect_ok", {
            "steps": len(plan.get("steps", [])) if plan else 0,
            "files": files_of_interest,
        })

        # ── Phase 2: Coder ───────────────────────────────────────────────
        coder_task = AgentTask(
            goal=goal,
            context=plan_json,
            files_of_interest=files_of_interest,
            success_criteria=f"All steps in plan implemented and tests pass via: {test_command}",
        )
        coder_result = self._coder.run(coder_task)
        agent_outputs["coder"] = coder_result.output
        phases.append("coder")

        log_event("orchestrator_coder_done", {
            "success": coder_result.success,
            "duration": coder_result.duration_seconds,
        })

        # ── Phase 3: Tester (initial run) ────────────────────────────────
        tester_task = AgentTask(
            goal=goal,
            context=coder_result.output,
            files_of_interest=files_of_interest,
            success_criteria=test_command,
        )
        tester_result = self._tester.run(tester_task)
        agent_outputs["tester"] = tester_result.output
        phases.append("tester")
        test_counts = self._tester.parse_results(tester_result.output)

        log_event("orchestrator_tester_done", {
            "passed": test_counts["passed"],
            "failed": test_counts["failed"],
            "errors": test_counts["errors"],
        })

        if tester_result.success:
            diff = self._git.diff_since(checkpoint_sha)
            log_event("orchestrator_success", {
                "goal": goal[:80],
                "phases": phases,
                "passed": test_counts["passed"],
            })
            return OrchestrationResult(
                goal=goal,
                success=True,
                phases_completed=phases,
                test_results=test_counts,
                diff=diff,
                duration_seconds=round(time.monotonic() - t0, 1),
                checkpoint_sha=checkpoint_sha,
                agent_outputs=agent_outputs,
            )

        # ── Phase 4: Debug loop ──────────────────────────────────────────
        last_tester_output = tester_result.output
        last_test_counts = test_counts

        for cycle in range(1, self.max_debug_cycles + 1):
            log_event("orchestrator_debug_cycle", {
                "cycle": cycle,
                "failed": last_test_counts["failed"],
                "errors": last_test_counts["errors"],
            })

            debug_task = AgentTask(
                goal=goal,
                context=last_tester_output,
                files_of_interest=files_of_interest,
            )
            debug_result = self._debugger.run(debug_task)
            agent_outputs[f"debugger_{cycle}"] = debug_result.output
            phases.append(f"debugger_{cycle}")

            # Re-run tester after fix
            retest_task = AgentTask(
                goal=goal,
                context=debug_result.output,
                files_of_interest=files_of_interest,
                success_criteria=test_command,
            )
            retest_result = self._tester.run(retest_task)
            agent_outputs[f"tester_{cycle}"] = retest_result.output
            phases.append(f"tester_{cycle}")
            last_test_counts = self._tester.parse_results(retest_result.output)
            last_tester_output = retest_result.output

            log_event("orchestrator_retest", {
                "cycle": cycle,
                "passed": last_test_counts["passed"],
                "failed": last_test_counts["failed"],
            })

            if retest_result.success:
                diff = self._git.diff_since(checkpoint_sha)
                log_event("orchestrator_success_after_debug", {
                    "goal": goal[:80],
                    "cycles": cycle,
                    "passed": last_test_counts["passed"],
                })
                return OrchestrationResult(
                    goal=goal,
                    success=True,
                    phases_completed=phases,
                    test_results=last_test_counts,
                    diff=diff,
                    duration_seconds=round(time.monotonic() - t0, 1),
                    checkpoint_sha=checkpoint_sha,
                    agent_outputs=agent_outputs,
                )

        # ── Debug limit exhausted ────────────────────────────────────────
        log_event("orchestrator_debug_limit", {
            "goal": goal[:80],
            "failed": last_test_counts["failed"],
        })
        # Do NOT rollback — preserve partial progress
        diff = self._git.diff_since(checkpoint_sha)
        return OrchestrationResult(
            goal=goal,
            success=False,
            phases_completed=phases,
            test_results=last_test_counts,
            diff=diff,
            duration_seconds=round(time.monotonic() - t0, 1),
            needs_human=True,
            checkpoint_sha=checkpoint_sha,
            agent_outputs=agent_outputs,
        )

    def _check_cost(self) -> str | None:
        """
        Check cost limits before a claude invocation.
        Returns an abort reason string if limits are exceeded, or None if ok.
        """
        if self._cost_tracker is None:
            return None
        try:
            result = self._cost_tracker.check_limits()
            if not result.get("ok", True):
                reason = result.get("reason", "cost limit reached")
                log_event("cost_limit_abort", {"reason": reason})
                return reason
        except Exception:
            pass
        return None


# ------------------------------------------------------------------
# Background dispatch helpers (used by tools layer)
# ------------------------------------------------------------------

def _run_build_background(goal: str, context: str) -> None:
    """
    Entry point for the background thread. Runs Orchestrator and stores
    the result in WorkingMemory under "last_orchestration_result".
    """
    wm = WorkingMemory()
    wm.write({
        "last_orchestration_result": {
            "status": "running",
            "goal": goal[:120],
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
    })
    try:
        orc = Orchestrator()
        result = orc.run(goal=goal, context=context)

        wm_data: dict[str, Any] = {
            "status": "complete",
            "success": result.success,
            "goal": goal[:120],
            "phases_completed": result.phases_completed,
            "test_results": result.test_results,
            "diff": result.diff[:1000],
            "duration_seconds": result.duration_seconds,
            "needs_human": result.needs_human,
            "checkpoint_sha": result.checkpoint_sha,
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        wm.write({"last_orchestration_result": wm_data})
        log_event("build_complete", {
            "goal": goal[:80],
            "success": result.success,
            "phases": len(result.phases_completed),
        })
    except Exception as exc:
        log_event("build_background_error", {"error": str(exc)[:200], "goal": goal[:80]})
        wm.write({
            "last_orchestration_result": {
                "status": "error",
                "success": False,
                "error": str(exc)[:200],
                "goal": goal[:120],
                "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        })


def start_build(goal: str, context: str = "") -> dict[str, Any]:
    """
    Dispatch an orchestrated build to a background thread.
    Returns immediately with {"status": "started", "goal": goal}.
    """
    goal = str(goal or "").strip()
    if not goal:
        return {"status": "error", "reason": "goal is required"}

    log_event("build_dispatched", {"goal": goal[:80]})

    t = threading.Thread(
        target=_run_build_background,
        args=(goal, context),
        daemon=True,
        name="orchestrator",
    )
    t.start()

    return {
        "status": "started",
        "goal": goal,
        "message": "Build started. Architect → Coder → Tester pipeline running in background.",
    }


def get_build_status() -> dict[str, Any]:
    """Return the most recent orchestration result from WorkingMemory."""
    wm = WorkingMemory().read()
    result = wm.get("last_orchestration_result")
    if not result or not isinstance(result, dict):
        return {"status": "no build running"}
    return result

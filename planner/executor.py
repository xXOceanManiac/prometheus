from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from utils import log_event
from working_memory import WorkingMemory


@dataclass
class StepResult:
    step_index: int
    action: str
    ok: bool
    message: str
    data: dict[str, Any] | None
    attempts: int


@dataclass
class ExecutionResult:
    steps: list[StepResult] = field(default_factory=list)

    @property
    def total_steps(self) -> int:
        return len(self.steps)

    @property
    def all_ok(self) -> bool:
        return bool(self.steps) and all(s.ok for s in self.steps)

    @property
    def summary(self) -> str:
        ok = sum(1 for s in self.steps if s.ok)
        return f"{ok}/{len(self.steps)} steps succeeded."

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps": [
                {
                    "step_index": s.step_index,
                    "action": s.action,
                    "ok": s.ok,
                    "message": s.message,
                    "attempts": s.attempts,
                }
                for s in self.steps
            ],
            "all_ok": self.all_ok,
            "summary": self.summary,
        }

    def accumulated_context(self) -> dict[str, Any]:
        """Merge all step data into a flat context dict for use by subsequent steps."""
        ctx: dict[str, Any] = {}
        for s in self.steps:
            if s.data:
                ctx.update(s.data)
            ctx[f"step_{s.step_index}_ok"] = s.ok
            ctx[f"step_{s.step_index}_message"] = s.message
        return ctx


class Executor:
    """
    Runs each PlanStep via ToolRegistry.execute(), with per-step retry and
    intermediate state written to WorkingMemory under background_task_state.
    """

    MAX_RETRIES = 3
    RETRY_DELAY = 1.0  # seconds between retries

    def __init__(self, tools: Any) -> None:
        self.tools = tools
        self.working = WorkingMemory()

    def run(
        self,
        plan: Any,  # planner.Plan
        context: dict[str, Any] | None = None,
        on_step: Any | None = None,  # callable(idx, StepResult, ExecutionResult)
    ) -> ExecutionResult:
        from planner.planner import Plan

        if not isinstance(plan, Plan):
            log_event("executor_invalid_plan", {"type": type(plan).__name__})
            return ExecutionResult()

        result = ExecutionResult()
        accumulated: dict[str, Any] = dict(context or {})

        for idx, step in enumerate(plan.steps):
            step_result = self._run_step(idx, step, accumulated)
            result.steps.append(step_result)

            self._write_intermediate(plan.intent, idx, step_result)

            if on_step is not None:
                try:
                    on_step(idx, step_result, result)
                except Exception:
                    pass

            if step_result.ok and step_result.data:
                accumulated.update(step_result.data)

            if not step_result.ok:
                log_event(
                    "executor_step_failed",
                    {
                        "step": idx,
                        "action": step.action,
                        "message": step_result.message,
                        "attempts": step_result.attempts,
                    },
                )

        return result

    def _run_step(self, idx: int, step: Any, context: dict[str, Any]) -> StepResult:
        payload = step.to_payload()

        last_message = ""
        last_data: dict[str, Any] | None = None

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                tool_result = self.tools.execute(payload)
                ok = bool(getattr(tool_result, "ok", False))
                msg = str(getattr(tool_result, "message", ""))
                data = getattr(tool_result, "data", None) or {}

                if ok:
                    return StepResult(
                        step_index=idx,
                        action=step.action,
                        ok=True,
                        message=msg,
                        data=data,
                        attempts=attempt,
                    )

                last_message = msg
                last_data = data
                log_event(
                    "executor_step_retry",
                    {
                        "step": idx,
                        "action": step.action,
                        "attempt": attempt,
                        "message": msg[:120],
                    },
                )

            except Exception as exc:
                last_message = f"Exception: {exc}"
                log_event(
                    "executor_step_exception",
                    {"step": idx, "action": step.action, "attempt": attempt, "error": str(exc)[:200]},
                )

            if attempt < self.MAX_RETRIES:
                time.sleep(self.RETRY_DELAY)

        return StepResult(
            step_index=idx,
            action=step.action,
            ok=False,
            message=last_message,
            data=last_data,
            attempts=self.MAX_RETRIES,
        )

    def _write_intermediate(
        self, intent: str, step_idx: int, step_result: StepResult
    ) -> None:
        try:
            self.working.write(
                {
                    "background_task_state": {
                        "intent": intent,
                        "current_step": step_idx,
                        "last_action": step_result.action,
                        "last_ok": step_result.ok,
                        "last_message": step_result.message[:200],
                        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    }
                }
            )
        except Exception as exc:
            log_event("executor_wm_write_error", {"error": str(exc)})

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from prometheus.infra.utils import log_event
from prometheus.memory.working_memory import WorkingMemory


@dataclass
class StepResult:
    step_index: int
    action: str
    ok: bool
    message: str
    data: dict[str, Any] | None
    attempts: int
    verified: bool | None = None          # None = not checked; True/False = outcome
    verification_confidence: float = 0.0  # confidence of the verification check
    verification_summary: str = ""        # one-liner from VerificationResult


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
                    "verified": s.verified,
                    "verification_summary": s.verification_summary,
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


def _try_verify(
    action: str,
    expected: str,
    execution_result: dict[str, Any],
    world_snapshot: dict[str, Any] | None,
) -> "VerificationResult | None":
    try:
        from prometheus.execution.verification import verify_action_result
        return verify_action_result(action, expected, execution_result, world_snapshot)
    except Exception:
        return None


def _get_world_snapshot() -> dict[str, Any] | None:
    try:
        from prometheus.context.world_model import build_world_snapshot
        return build_world_snapshot()
    except Exception:
        return None


class Executor:
    """
    Runs each PlanStep via ToolRegistry.execute(), with per-step retry and
    intermediate state written to WorkingMemory under background_task_state.
    Uses verify_action_result() after each successful tool call — if verification
    says the action failed despite ok=True, it retries when retry_recommended=True.
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
        from prometheus.planning.planner import Plan

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
        last_verified: bool | None = None
        last_vconf: float = 0.0
        last_vsummary: str = ""

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                tool_result = self.tools.execute(payload)
                ok = bool(getattr(tool_result, "ok", False))
                msg = str(getattr(tool_result, "message", ""))
                data = getattr(tool_result, "data", None) or {}

                if ok:
                    # Verification runs outside the main try/except so a verify crash
                    # never masks a successful tool call as a failure.
                    try:
                        snap = _get_world_snapshot()
                        vr = _try_verify(
                            step.action, "", {"ok": True, "message": msg, "data": data}, snap
                        )
                    except Exception:
                        vr = None

                    if vr is not None:
                        last_verified = vr.verified
                        last_vconf = vr.confidence
                        last_vsummary = vr.summary
                        log_event("executor_step_verified", {
                            "step": idx,
                            "action": step.action,
                            "verified": vr.verified,
                            "confidence": round(vr.confidence, 2),
                            "summary": vr.summary[:100],
                        })

                        # Verification says this didn't actually work — retry if recommended
                        if not vr.verified and vr.retry_recommended and attempt < self.MAX_RETRIES:
                            last_message = f"Verification failed: {vr.summary}"
                            last_data = data
                            log_event("executor_step_retry", {
                                "step": idx, "action": step.action,
                                "attempt": attempt, "message": last_message[:120],
                                "reason": "verification",
                            })
                            time.sleep(self.RETRY_DELAY)
                            continue

                    return StepResult(
                        step_index=idx,
                        action=step.action,
                        ok=True,
                        message=msg,
                        data=data,
                        attempts=attempt,
                        verified=last_verified,
                        verification_confidence=last_vconf,
                        verification_summary=last_vsummary,
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
                        "reason": "tool_failure",
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
            verified=last_verified,
            verification_confidence=last_vconf,
            verification_summary=last_vsummary,
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

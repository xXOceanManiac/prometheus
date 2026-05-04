from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from utils import log_event


@dataclass
class VerificationResult:
    verified: bool
    reason: str
    correction_context: dict[str, Any] = field(default_factory=dict)


class Verifier:
    """
    Checks whether execution results match the original intent.
    Lightweight — avoids a full LLM call for simple tool actions.
    Returns a correction_context dict on failure so the Executor can retry.
    """

    def verify(
        self,
        intent: str,
        plan: Any,  # planner.Plan
        results: Any,  # executor.ExecutionResult
    ) -> VerificationResult:
        try:
            from planner.executor import ExecutionResult
            from planner.planner import Plan

            if not isinstance(results, ExecutionResult):
                return VerificationResult(
                    verified=False,
                    reason="Invalid result type",
                    correction_context={"error": "ExecutionResult expected"},
                )

            if not results.steps:
                return VerificationResult(
                    verified=False,
                    reason="No steps were executed",
                    correction_context={"hint": "Plan produced no steps — check intent clarity"},
                )

            # All steps must succeed for verification to pass
            if results.all_ok:
                log_event("verifier_pass", {"intent": intent[:80], "steps": results.total_steps})
                return VerificationResult(verified=True, reason=results.summary)

            # Identify failed steps and build correction context
            failed = [s for s in results.steps if not s.ok]
            failed_actions = [s.action for s in failed]
            failed_messages = [s.message for s in failed]

            correction_context: dict[str, Any] = {
                "failed_actions": failed_actions,
                "failed_messages": failed_messages[:3],
                "hint": self._correction_hint(failed_actions, failed_messages),
            }

            log_event(
                "verifier_fail",
                {
                    "intent": intent[:80],
                    "failed_steps": len(failed),
                    "total_steps": results.total_steps,
                    "failed_actions": failed_actions,
                },
            )

            return VerificationResult(
                verified=False,
                reason=f"{len(failed)}/{results.total_steps} steps failed: {', '.join(failed_actions)}",
                correction_context=correction_context,
            )

        except Exception as exc:
            log_event("verifier_error", {"error": str(exc)[:200]})
            return VerificationResult(
                verified=False,
                reason=f"Verifier error: {exc}",
                correction_context={"error": str(exc)},
            )

    def _correction_hint(
        self, failed_actions: list[str], failed_messages: list[str]
    ) -> str:
        combined = " ".join(failed_messages).lower()

        if "not found" in combined or "no such file" in combined:
            return "Path may be wrong — verify the file or directory exists before retrying."
        if "permission" in combined:
            return "Permission denied — the action may require elevated access."
        if "timeout" in combined or "connection" in combined:
            return "Network or service timeout — retry after a brief delay."
        if "unknown action" in combined:
            return "Action name not recognized — use only allowed action names."
        if failed_actions:
            return f"Step '{failed_actions[0]}' failed — check arguments and retry."
        return "One or more steps failed — retry with corrected context."

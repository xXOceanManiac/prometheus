"""
planner/decision_router.py — Typed decision parsing and routing for GPT-4o responses.

Replaces freeform JSON parsing with a structured Decision schema.
DecisionRouter.parse() extracts a Decision from raw LLM output.
DecisionRouter.to_plan() converts a Decision to a Plan and writes state updates.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from utils import log_event


@dataclass
class Decision:
    decision_type: str  # tool_call | user_response | status_update | alert | clarification
    reasoning: str
    action: dict[str, Any] = field(default_factory=dict)
    state_updates: dict[str, Any] = field(default_factory=dict)
    voice_response: str = ""


_VALID_TYPES = frozenset({"tool_call", "user_response", "status_update", "alert", "clarification"})


class DecisionRouter:
    """
    Parses structured Decision JSON from GPT-4o and converts to Plan + state writes.
    """

    def parse(self, raw: str, intent: str) -> Decision:
        """Parse raw LLM output into a Decision. Never raises."""
        try:
            m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
            json_str = m.group(1) if m else raw.strip()
            start = json_str.find("{")
            end = json_str.rfind("}") + 1
            if start >= 0 and end > start:
                json_str = json_str[start:end]
            data = json.loads(json_str)
        except Exception as exc:
            log_event("decision_parse_error", {"error": str(exc)[:120], "raw": raw[:200]})
            return Decision(
                decision_type="clarification",
                reasoning="Failed to parse LLM decision",
                voice_response="I couldn't generate a clear plan. Can you be more specific?",
            )

        dtype = str(data.get("decision_type", "clarification")).strip()
        if dtype not in _VALID_TYPES:
            dtype = "clarification"

        return Decision(
            decision_type=dtype,
            reasoning=str(data.get("reasoning", ""))[:200],
            action=data.get("action") if isinstance(data.get("action"), dict) else {},
            state_updates=data.get("state_updates") if isinstance(data.get("state_updates"), dict) else {},
            voice_response=str(data.get("voice_response", ""))[:400],
        )

    def apply_state_updates(self, updates: dict[str, Any]) -> None:
        """Write state_updates back to MissionState. Never raises."""
        if not updates:
            return
        try:
            from mission_state import MissionState
            ms = MissionState()
            if updates.get("task_completed"):
                ms.complete_subtask(str(updates["task_completed"]))
            if updates.get("new_blocker"):
                ms.add_blocker(str(updates["new_blocker"]))
            if updates.get("next_action"):
                ms.set_next_action(str(updates["next_action"]))
            if updates.get("blocker_cleared"):
                ms.clear_blocker(str(updates["blocker_cleared"]))
            if updates.get("mission"):
                ms.set_mission(str(updates["mission"]))
        except Exception as exc:
            log_event("decision_state_update_error", {"error": str(exc)[:120]})

    def to_plan(self, decision: Decision, intent: str) -> Any:
        """
        Convert a Decision to a Plan and apply state updates.
        Imports Plan locally to avoid circular imports.
        """
        from planner.planner import Plan, PlanStep

        self.apply_state_updates(decision.state_updates)

        if decision.decision_type == "tool_call":
            action_data = decision.action
            confidence = float(action_data.get("confidence", 0.75))
            clarification_needed = bool(action_data.get("clarification_needed", False)) or confidence < 0.6
            steps: list[PlanStep] = []
            if not clarification_needed:
                for raw_step in (action_data.get("steps") or []):
                    if not isinstance(raw_step, dict):
                        continue
                    action = str(raw_step.get("action", "")).strip()
                    if not action:
                        continue
                    steps.append(PlanStep(
                        action=action,
                        args={k: v for k, v in raw_step.items() if k != "action"},
                    ))
            return Plan(
                intent=str(action_data.get("intent", intent)),
                confidence=confidence,
                reason=str(action_data.get("reason", decision.reasoning)),
                steps=steps,
                clarification_needed=clarification_needed,
                clarification_question=str(action_data.get("clarification_question", "")),
                voice_hint=decision.voice_response,
            )

        elif decision.decision_type == "clarification":
            q = decision.voice_response or "Can you be more specific about what you want?"
            return Plan(
                intent=intent,
                confidence=0.3,
                reason=decision.reasoning or "Clarification needed",
                clarification_needed=True,
                clarification_question=q,
                voice_hint=q,
            )

        elif decision.decision_type in {"user_response", "alert", "status_update"}:
            return Plan(
                intent=intent,
                confidence=0.9,
                reason=decision.reasoning,
                steps=[],
                clarification_needed=False,
                voice_hint=decision.voice_response,
            )

        else:
            return Plan(
                intent=intent,
                confidence=0.3,
                reason="Unknown decision type",
                clarification_needed=True,
                clarification_question="I'm not sure how to handle that.",
                voice_hint="",
            )

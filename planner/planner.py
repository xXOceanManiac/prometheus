from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from utils import log_event

# Subset of ACTION_ENUM available to background workers (no destructive actions)
_SAFE_ACTIONS = [
    "open_app", "close_app", "open_url_key", "open_url_raw", "web_search",
    "open_code_folder", "open_terminal_here", "smart_action", "summarize_screen",
    "list_files", "read_file", "write_file", "run_ha_script", "tell_time",
    "screenshot", "desktop_state", "list_windows", "get_active_window",
    "run_routine", "shell_command",
    "run_python", "run_shell", "search_codebase", "git_status", "git_diff", "git_commit",
]

# Base system prompt — operational_state block is prepended at call time
_BASE_SYSTEM_PROMPT = f"""You are a task planner for a desktop AI assistant named Prometheus.
Given a user intent and operational context, produce a typed JSON decision.

Available tool actions: {", ".join(_SAFE_ACTIONS)}

Step argument shapes:
- open_app:         {{"app": "appname"}}
- web_search:       {{"query": "search terms"}}
- list_files:       {{"path": "/absolute/path"}}
- read_file:        {{"path": "/absolute/path"}}
- write_file:       {{"path": "/absolute/path", "content": "..."}}
- shell_command:    {{"command": "bash command string"}}
- run_ha_script:    {{"script_name": "jarvis_..."}}
- open_url_raw:     {{"url": "https://..."}}
- open_code_folder: {{"project_path": "/absolute/path"}}
- run_python:       {{"command": "python3 snippet or filename", "project_path": "/path"}}
- run_shell:        {{"command": "whitelisted shell command"}}
- search_codebase:  {{"query": "search terms", "project_path": "/path"}}
- git_status:       {{"project_path": "/path"}}
- git_diff:         {{"project_path": "/path", "file": "optional specific file"}}
- git_commit:       {{"project_path": "/path", "message": "commit message"}}

Respond with ONLY valid JSON matching this exact schema:
{{
  "decision_type": "tool_call" | "user_response" | "status_update" | "alert" | "clarification",
  "reasoning": "<one sentence max>",
  "voice_response": "<text for TTS — required when decision_type is user_response or alert>",
  "action": {{
    // For tool_call: intent, confidence (0.0-1.0), reason, clarification_needed, clarification_question, steps[]
    // For other types: empty object {{}}
  }},
  "state_updates": {{
    // Optional — any subset of:
    // "task_completed": "task description or id",
    // "new_blocker": "description",
    // "next_action": "what to do next",
    // "blocker_cleared": "description fragment",
    // "mission": "new mission string"
  }}
}}

Decision type rules:
- "tool_call": Execute one or more tool actions (steps in action.steps)
- "user_response": Answer the user directly without tool execution (answer in voice_response)
- "status_update": Apply state changes only, no tool execution, no spoken response needed
- "alert": Something the user should know right now (message in voice_response)
- "clarification": Intent is unclear — ask a specific question (question in voice_response)

For tool_call, action must include:
{{
  "intent": "<original intent>",
  "confidence": 0.85,
  "reason": "<one sentence>",
  "clarification_needed": false,
  "clarification_question": "",
  "steps": [{{"action": "action_name", "arg_key": "value"}}]
}}

Rules:
- If confidence < 0.6, use decision_type="clarification" instead of tool_call
- Only use action names from the available list above
- Provide absolute paths when project_path is in context
- Use run_shell for tasks that do not map to built-in actions (first token must be whitelisted)
- Produce the minimal steps needed — do not pad
- git_commit always requires confirmed=true in args"""


@dataclass
class PlanStep:
    action: str
    args: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {"action": self.action, **self.args}


@dataclass
class Plan:
    intent: str
    confidence: float
    reason: str
    steps: list[PlanStep] = field(default_factory=list)
    clarification_needed: bool = False
    clarification_question: str = ""
    voice_hint: str = ""  # TTS suggestion from decision router; empty = let downstream decide

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "confidence": self.confidence,
            "reason": self.reason,
            "clarification_needed": self.clarification_needed,
            "clarification_question": self.clarification_question,
            "voice_hint": self.voice_hint,
            "steps": [s.to_payload() for s in self.steps],
        }


class Planner:
    """
    Builds execution plans from natural-language intent.
    Fast-path: rule-based matching for common patterns.
    LLM path: GPT-4o with operational context injection and typed Decision output.
    """

    def build(self, intent: str, context: dict[str, Any] | None = None) -> Plan:
        context = context or {}
        intent = str(intent).strip()

        if not intent:
            return Plan(
                intent="",
                confidence=0.0,
                reason="Empty intent",
                clarification_needed=True,
                clarification_question="What would you like me to do?",
            )

        # WorkflowSelector runs first — before rule-based and LLM paths
        workflow = self._workflow_select(intent, context)
        if workflow is not None:
            log_event("planner_workflow_match", {
                "intent": intent[:80],
                "workflow": workflow.voice_hint,
                "confidence": workflow.confidence,
            })
            return workflow

        fast = self._rule_based(intent, context)
        if fast is not None:
            log_event("planner_fast_path", {"intent": intent[:80], "steps": len(fast.steps)})
            return fast

        return self._llm_plan(intent, context)

    def _workflow_select(self, intent: str, context: dict[str, Any]) -> Plan | None:
        """
        Run WorkflowSelector. Returns a Plan if confidence ≥ 0.90, else None.
        Attaches workflow context as voice_hint. High-risk workflows set clarification_needed.
        """
        try:
            from prometheus.planning.workflow_selector import resolve_workflow
            from prometheus.planning.workflow_registry import WORKFLOWS

            snapshot = {
                "active_project": context.get("active_project") or context.get("active_workspace"),
                "active_project_path": context.get("project_path") or context.get("active_project_path"),
                "recent_errors": context.get("recent_errors", []),
                "open_windows": context.get("open_windows", []),
                "active_window": context.get("active_window", {}),
                "last_tool_action": context.get("last_tool_action", ""),
            }
            mission_summary = str(context.get("mission_summary", "") or "")

            resolution = resolve_workflow(intent, snapshot, mission_summary)

            if not resolution.matched or resolution.confidence < 0.90:
                return None

            if resolution.requires_clarification:
                return Plan(
                    intent=intent,
                    confidence=resolution.confidence,
                    reason=resolution.reasoning,
                    clarification_needed=True,
                    clarification_question=resolution.clarification_question or "Can you be more specific?",
                    voice_hint=resolution.clarification_question or "",
                )

            wf_def = WORKFLOWS.get(resolution.workflow_name)
            preferred = resolution.preferred_tools or (wf_def.preferred_tools if wf_def else [])
            steps = [PlanStep(t, {}) for t in preferred[:3]]  # first 3 as starter steps

            plan = Plan(
                intent=intent,
                confidence=resolution.confidence,
                reason=resolution.reasoning,
                steps=steps,
                clarification_needed=resolution.requires_confirmation,
                clarification_question=(
                    f"This will {resolution.workflow_name.replace('_', ' ')}. Confirm?"
                    if resolution.requires_confirmation else ""
                ),
                voice_hint=f"workflow:{resolution.workflow_name}",
            )

            # Attach workflow metadata for downstream use
            plan.__dict__["_workflow"] = resolution

            return plan

        except Exception as exc:
            log_event("planner_workflow_select_error", {"error": str(exc)[:120]})
            return None

    # ------------------------------------------------------------------
    # Rule-based fast path — no LLM, no latency
    # ------------------------------------------------------------------

    # Words that indicate an intent has no actionable referent
    _GENERIC_WORDS = frozenset({
        "thing", "things", "it", "this", "that", "those", "these",
        "them", "stuff", "something", "anything", "whatever",
    })
    _FILLER_WORDS = frozenset({
        "do", "the", "a", "an", "some", "any", "my", "your", "with",
        "and", "or", "about", "for", "on", "in", "of",
    })

    def _is_ambiguous(self, text: str) -> bool:
        """
        Return True for intents that are clearly too vague to action without clarification.
        These are caught before the LLM call to avoid wasting a round-trip.
        """
        words = text.lower().split()
        if not words:
            return True
        combined = self._GENERIC_WORDS | self._FILLER_WORDS
        generic_count = sum(1 for w in words if w in self._GENERIC_WORDS)
        all_generic = all(w in combined for w in words)
        if all_generic:
            return True
        if len(words) <= 6 and generic_count >= 2:
            return True
        return False

    def _rule_based(self, intent: str, context: dict[str, Any]) -> Plan | None:
        text = intent.lower().strip()

        if self._is_ambiguous(text):
            return Plan(
                intent=intent,
                confidence=0.2,
                reason="Intent is too vague to action without clarification",
                clarification_needed=True,
                clarification_question="Can you be more specific about what you'd like me to do?",
            )

        project_path = str(
            context.get("project_path")
            or context.get("active_project_path")
            or ""
        ).strip()

        if re.search(r"summar[iy]", text) and ("project" in text or project_path):
            steps: list[PlanStep] = []
            if project_path:
                steps.append(PlanStep("list_files", {"path": project_path}))
                steps.append(PlanStep("read_file", {"path": f"{project_path}/README.md"}))
            else:
                steps.append(PlanStep("desktop_state", {}))
            return Plan(
                intent=intent,
                confidence=0.82,
                reason="Summarize active project — list files and read README",
                steps=steps,
            )

        m = re.match(
            r"(?:search(?:\s+(?:for|the\s+web\s+for))?|look\s+up|find)\s+(.+)", text
        )
        if m:
            query = m.group(1).strip().rstrip(".")
            return Plan(
                intent=intent,
                confidence=0.90,
                reason="Web search request",
                steps=[PlanStep("web_search", {"query": query})],
            )

        m = re.match(r"open\s+(\w[\w\s]*?)(?:\s+app)?\s*$", text)
        if m:
            return Plan(
                intent=intent,
                confidence=0.85,
                reason="Open application",
                steps=[PlanStep("open_app", {"app": m.group(1).strip()})],
            )

        m = re.search(r"read\s+(/.+|~/.+)", text)
        if m:
            return Plan(
                intent=intent,
                confidence=0.88,
                reason="Read file",
                steps=[PlanStep("read_file", {"path": m.group(1).strip()})],
            )

        return None

    # ------------------------------------------------------------------
    # GPT-4o planning — structured decision output with operational context
    # ------------------------------------------------------------------

    def _llm_plan(self, intent: str, context: dict[str, Any]) -> Plan:
        try:
            from llm_router import get_planning_llm
            from cognition import build_safe_snapshot, format_operational_state_block
            from planner.decision_router import DecisionRouter

            llm = get_planning_llm()
            if llm is None:
                return Plan(
                    intent=intent,
                    confidence=0.4,
                    reason="LLM unavailable — cannot plan complex intent",
                    clarification_needed=True,
                    clarification_question="I need more detail to plan this task.",
                )

            # Assemble operational context (< 100ms, no caches)
            snapshot = build_safe_snapshot()
            op_block = format_operational_state_block(snapshot)

            system_prompt = f"{op_block}\n\n{_BASE_SYSTEM_PROMPT}"

            ctx_summary = json.dumps(
                {
                    "active_project": context.get("active_project") or context.get("active_workspace", ""),
                    "project_path": (
                        context.get("project_path")
                        or context.get("active_project_path", "")
                    ),
                    "vault_context": [
                        {
                            "title": v.get("title", ""),
                            "text": (v.get("text") or "")[:150],
                        }
                        for v in (context.get("vault_context") or [])[:3]
                    ],
                    "working_memory_summary": {
                        "last_request": context.get("last_user_request", ""),
                        "last_tool": context.get("last_tool_action", ""),
                        "active_goal": context.get("active_goal", ""),
                    },
                    "recent_session_summary": str(context.get("recent_session_summary", ""))[:200],
                    "available_actions": _SAFE_ACTIONS,
                },
                indent=2,
            )

            raw = llm.complete(
                f"Intent: {intent}\n\nContext:\n{ctx_summary}",
                system=system_prompt,
            )

            router = DecisionRouter()
            decision = router.parse(raw, intent)
            log_event("planner_decision", {
                "intent": intent[:80],
                "decision_type": decision.decision_type,
                "reasoning": decision.reasoning[:80],
            })
            return router.to_plan(decision, intent)

        except Exception as exc:
            log_event("planner_llm_error", {"error": str(exc)[:200], "intent": intent[:80]})
            return Plan(
                intent=intent,
                confidence=0.3,
                reason=f"Planning error: {exc}",
                clarification_needed=True,
                clarification_question="I hit an error planning this. Can you be more specific?",
            )

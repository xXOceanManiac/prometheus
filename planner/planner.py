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

_SYSTEM_PROMPT = f"""You are a task planner for a desktop AI assistant named Prometheus.
Given a user intent and context, produce a concrete JSON execution plan.

Available actions: {", ".join(_SAFE_ACTIONS)}

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

Output ONLY valid JSON matching this schema exactly:
{{
  "intent": "<original intent>",
  "confidence": 0.0,
  "reason": "<why this plan>",
  "clarification_needed": false,
  "clarification_question": "",
  "steps": [
    {{"action": "action_name", "argname": "value"}}
  ]
}}

Rules:
- If confidence < 0.6, set clarification_needed=true, steps=[], write clarification_question.
- Only use action names from the allowed list above.
- Provide absolute paths when project_path is in context.
- Use run_shell for tasks that do not map to built-in actions (first token must be whitelisted).
- Produce the minimal steps needed — do not pad.
- git_commit always requires confirmed=true in args."""


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "confidence": self.confidence,
            "reason": self.reason,
            "clarification_needed": self.clarification_needed,
            "clarification_question": self.clarification_question,
            "steps": [s.to_payload() for s in self.steps],
        }


class Planner:
    """
    Builds execution plans from natural-language intent.
    Fast-path: rule-based matching for common patterns.
    Fallback: LLM via llm_router.get_llm("planning").
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

        fast = self._rule_based(intent, context)
        if fast is not None:
            log_event("planner_fast_path", {"intent": intent[:80], "steps": len(fast.steps)})
            return fast

        return self._llm_plan(intent, context)

    # ------------------------------------------------------------------
    # Rule-based fast path
    # ------------------------------------------------------------------

    def _rule_based(self, intent: str, context: dict[str, Any]) -> Plan | None:
        text = intent.lower().strip()
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
    # LLM-based planning
    # ------------------------------------------------------------------

    def _llm_plan(self, intent: str, context: dict[str, Any]) -> Plan:
        try:
            from llm_router import get_llm

            llm = get_llm("planning")
            if llm is None:
                return Plan(
                    intent=intent,
                    confidence=0.4,
                    reason="LLM unavailable — cannot plan complex intent",
                    clarification_needed=True,
                    clarification_question="I need more detail to plan this task.",
                )

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
                system=_SYSTEM_PROMPT,
            )
            return self._parse_llm_response(raw, intent)

        except Exception as exc:
            log_event("planner_llm_error", {"error": str(exc)[:200], "intent": intent[:80]})
            return Plan(
                intent=intent,
                confidence=0.3,
                reason=f"Planning error: {exc}",
                clarification_needed=True,
                clarification_question="I hit an error planning this. Can you be more specific?",
            )

    def _parse_llm_response(self, raw: str, intent: str) -> Plan:
        try:
            m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
            json_str = m.group(1) if m else raw.strip()

            start = json_str.find("{")
            end = json_str.rfind("}") + 1
            if start >= 0 and end > start:
                json_str = json_str[start:end]

            data = json.loads(json_str)
            confidence = float(data.get("confidence", 0.5))
            clarification_needed = (
                bool(data.get("clarification_needed", False)) or confidence < 0.6
            )

            steps: list[PlanStep] = []
            if not clarification_needed:
                for raw_step in data.get("steps") or []:
                    if not isinstance(raw_step, dict):
                        continue
                    action = str(raw_step.get("action", "")).strip()
                    if not action:
                        continue
                    steps.append(
                        PlanStep(
                            action=action,
                            args={k: v for k, v in raw_step.items() if k != "action"},
                        )
                    )

            return Plan(
                intent=str(data.get("intent", intent)),
                confidence=confidence,
                reason=str(data.get("reason", "")),
                steps=steps,
                clarification_needed=clarification_needed,
                clarification_question=str(data.get("clarification_question", "")),
            )

        except Exception as exc:
            log_event("planner_parse_error", {"error": str(exc)[:200], "raw": raw[:300]})
            return Plan(
                intent=intent,
                confidence=0.3,
                reason="Failed to parse LLM response",
                clarification_needed=True,
                clarification_question="Couldn't generate a clear plan. Can you be more specific?",
            )

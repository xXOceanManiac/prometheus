"""
agents/architect.py — ArchitectAgent for Prometheus orchestration.

Given a goal, produces a structured JSON build plan that Coder and Tester
can execute against. parse_plan() extracts the structured plan from raw output.
"""
from __future__ import annotations

import json
import re
from typing import Any

from prometheus.coding.agent_base import BaseAgent, AgentTask, AgentResult


class ArchitectAgent(BaseAgent):
    """
    Produces a structured JSON build plan for a given goal.

    Output format (embedded in claude's response):
    {
      "steps": [
        {"id": 1, "description": "...", "file": "...", "action": "create|modify|delete"},
        ...
      ],
      "files_of_interest": ["file1.py", "file2.py"],
      "test_command": "python -m pytest tests/ -x -q",
      "notes": "..."
    }
    """

    name = "architect"
    role_prompt = (
        "You are a software architect. Given a goal, produce a concise JSON build plan "
        "describing exactly what files to create or modify and in what order. "
        "Be precise, minimal, and actionable."
    )

    def build_prompt(self, task: AgentTask) -> str:
        parts = [
            f"Role: {self.role_prompt}",
            "",
            f"Goal: {task.goal}",
        ]
        if task.context:
            parts += ["", f"Context:\n{task.context}"]
        if task.files_of_interest:
            parts += ["", f"Relevant files: {', '.join(task.files_of_interest)}"]
        parts += [
            "",
            "Produce a JSON build plan using this exact schema (wrap in ```json ... ```):",
            '{"steps": [{"id": 1, "description": "...", "file": "...", "action": "create|modify|delete"}], '
            '"files_of_interest": [...], "test_command": "...", "notes": "..."}',
            "",
            "Include only what is strictly necessary. Do not write code yet.",
        ]
        return "\n".join(parts)

    def parse_plan(self, output: str) -> dict[str, Any] | None:
        """
        Extract and parse the JSON plan from the agent's raw output.
        Returns the plan dict or None if parsing fails.
        """
        if not output:
            return None

        # Try fenced code block first
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", output, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass

        # Fall back to first {...} block
        m = re.search(r"\{.*\}", output, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass

        return None

    def _assess_success(self, output: str, task: AgentTask) -> bool:
        if not super()._assess_success(output, task):
            return False
        plan = self.parse_plan(output)
        if plan is None:
            return False
        steps = plan.get("steps")
        return isinstance(steps, list) and len(steps) > 0

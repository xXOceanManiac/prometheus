"""
agents/coder.py — CoderAgent for Prometheus orchestration.

Receives an Architect plan as context and implements the required code changes.
"""
from __future__ import annotations

import json
from typing import Any

from agent_base import BaseAgent, AgentTask, AgentResult


class CoderAgent(BaseAgent):
    """
    Implements code changes described in an Architect plan.

    Expects task.context to contain the JSON plan produced by ArchitectAgent.
    Writes actual files, makes real changes to the codebase.
    """

    name = "coder"
    role_prompt = (
        "You are an expert Python engineer. Given a build plan and goal, implement "
        "the required code changes precisely. Write production-quality code with full "
        "error handling. Make only the changes described. Do not touch unrelated code."
    )

    def build_prompt(self, task: AgentTask) -> str:
        parts = [
            f"Role: {self.role_prompt}",
            "",
            f"Goal: {task.goal}",
        ]

        if task.context:
            parts += [
                "",
                "Build plan from Architect:",
                task.context,
            ]

        if task.files_of_interest:
            parts += ["", f"Files to work with: {', '.join(task.files_of_interest)}"]

        if task.success_criteria:
            parts += ["", f"Success criteria: {task.success_criteria}"]

        parts += [
            "",
            "Implement ALL steps in the plan above. For each file:",
            "- Create it if the action is 'create'",
            "- Modify the relevant sections if the action is 'modify'",
            "When done, confirm which files were changed.",
        ]
        return "\n".join(parts)

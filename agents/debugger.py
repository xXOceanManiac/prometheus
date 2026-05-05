"""
agents/debugger.py — DebuggerAgent for Prometheus orchestration.

Given test failure output, surgically fixes failing tests without
breaking passing ones.
"""
from __future__ import annotations

from agent_base import BaseAgent, AgentTask, AgentResult


class DebuggerAgent(BaseAgent):
    """
    Fixes test failures produced by TesterAgent.

    task.context should contain the pytest failure output.
    task.goal should describe the original feature goal.
    """

    name = "debugger"
    role_prompt = (
        "You are a surgical debugger. Given test failures, identify the root cause "
        "and fix only the failing code. Do not rewrite passing logic. "
        "Be minimal and precise — one targeted fix per failure."
    )

    def build_prompt(self, task: AgentTask) -> str:
        parts = [
            f"Role: {self.role_prompt}",
            "",
            f"Original goal: {task.goal}",
        ]

        if task.context:
            parts += [
                "",
                "Test failure output:",
                "```",
                task.context,
                "```",
            ]

        if task.files_of_interest:
            parts += ["", f"Files involved: {', '.join(task.files_of_interest)}"]

        parts += [
            "",
            "Instructions:",
            "1. Read the failing test(s) to understand what they expect.",
            "2. Read the implementation to find the bug.",
            "3. Apply the minimal fix — change only what is broken.",
            "4. Do not modify test files unless the test itself has a bug.",
            "5. After fixing, confirm which files you changed and what you changed.",
        ]
        return "\n".join(parts)

"""
agents/tester.py — TesterAgent for Prometheus orchestration.

Writes and runs pytest tests for the implemented code. parse_results() extracts
pass/fail counts from pytest stdout.
"""
from __future__ import annotations

import re
import subprocess
from typing import Any

from prometheus.agents.agent_base import BaseAgent, AgentTask, AgentResult


class TesterAgent(BaseAgent):
    """
    Writes pytest tests and runs them against the current codebase.

    parse_results() parses pytest stdout to extract pass/fail counts.
    """

    name = "tester"
    role_prompt = (
        "You are a test engineer. Given a goal and the code that was implemented, "
        "write pytest tests that verify correctness. Tests must be runnable immediately. "
        "Write tests to tests/test_<feature>.py or run the existing test suite."
    )

    def build_prompt(self, task: AgentTask) -> str:
        parts = [
            f"Role: {self.role_prompt}",
            "",
            f"Goal being tested: {task.goal}",
        ]

        if task.context:
            parts += [
                "",
                "What was implemented:",
                task.context,
            ]

        if task.files_of_interest:
            parts += ["", f"Files implemented: {', '.join(task.files_of_interest)}"]

        parts += [
            "",
            "Steps:",
            "1. Write or update pytest tests for the implemented functionality.",
            "2. Run the tests with: python -m pytest tests/ -x -q --tb=short",
            "3. Print the full pytest output so the result can be evaluated.",
            "Do not modify source files — only write or update test files.",
        ]
        return "\n".join(parts)

    def parse_results(self, output: str) -> dict[str, int]:
        """
        Parse pytest stdout to extract pass/fail/error counts.

        Returns dict with keys: passed, failed, errors, total.
        Returns {"passed": 0, "failed": 0, "errors": 0, "total": 0} on parse failure.
        """
        result = {"passed": 0, "failed": 0, "errors": 0, "total": 0}
        if not output:
            return result

        # Pytest summary line: "5 passed, 1 failed, 2 errors in 0.42s"
        # or "5 passed in 0.42s" or "1 failed in 0.42s"
        summary = re.search(
            r"(\d+)\s+passed|(\d+)\s+failed|(\d+)\s+error",
            output,
        )

        # More robust: scan all occurrences
        passed_m = re.search(r"(\d+)\s+passed", output)
        failed_m = re.search(r"(\d+)\s+failed", output)
        errors_m = re.search(r"(\d+)\s+error", output)

        if passed_m:
            result["passed"] = int(passed_m.group(1))
        if failed_m:
            result["failed"] = int(failed_m.group(1))
        if errors_m:
            result["errors"] = int(errors_m.group(1))

        result["total"] = result["passed"] + result["failed"] + result["errors"]
        return result

    def _assess_success(self, output: str, task: AgentTask) -> bool:
        if not super()._assess_success(output, task):
            return False
        counts = self.parse_results(output)
        return counts["failed"] == 0 and counts["errors"] == 0 and counts["passed"] > 0

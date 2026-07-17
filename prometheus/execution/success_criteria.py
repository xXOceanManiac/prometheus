"""
success_criteria.py — Define and evaluate completion criteria for autonomous coding tasks.

SuccessCriteria describes what "done" looks like for a given goal.
SuccessCriteriaEngine infers criteria from natural language and evaluates them.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from prometheus.infra.utils import log_event


@dataclass
class SuccessCriteria:
    goal: str
    check_type: str          # "log" | "test" | "file_exists" | "shell_exit" | "manual"
    check_value: str         # what to look for, depending on check_type
    timeout_seconds: int = 300
    description: str = ""

    def __post_init__(self) -> None:
        valid = {"log", "test", "file_exists", "shell_exit", "manual"}
        if self.check_type not in valid:
            raise ValueError(f"check_type must be one of {valid}, got {self.check_type!r}")


class SuccessCriteriaEngine:
    """
    Infers SuccessCriteria from natural language goals and evaluates them.
    """

    # (pattern, check_type, check_value_template)
    # check_value_template may reference match groups via {1}, {2}, etc.
    _RULES: list[tuple[str, str, str, str]] = [
        # test / pytest goals
        (r"write tests?\s+for\s+(.+)", "test", "python -m pytest tests/ -x -q",
         "Run pytest and expect all tests to pass"),
        (r"fix.*(test|pytest|failing test)", "test", "python -m pytest tests/ -x -q",
         "Run pytest and expect all tests to pass"),
        (r"make.*(test|pytest).*(pass|green)", "test", "python -m pytest tests/ -x -q",
         "Run pytest and expect all tests to pass"),

        # vault / memory bugs
        (r"fix.*(vault|memory).*(bug|inject|issue|problem)", "log", "vault_context_injected",
         "Prometheus log shows vault_context_injected event on startup"),
        (r"fix.*(inject|injection)", "log", "vault_context_injected",
         "Prometheus log shows vault_context_injected event"),

        # tool registration
        (r"add\s+a?\s*(new\s+)?tool\s+(called\s+)?(\w+)", "log", "tool_registered",
         "Prometheus log shows tool_registered event"),

        # file creation goals — named file with extension
        (r"(?:create|write|generate|output)\s+(?:a\s+)?(?:new\s+)?(?:\w+\s+)?(?:file\s+)?([~/\w.\-]+\.\w+)", "file_exists", "{1}",
         "Confirm the file exists after the agent runs"),
        # generic file creation intent without a specific path
        (r"(?:create|write|generate|output)\s+(?:a\s+)?(?:new\s+)?(?:\w+\s+)*(?:report|file|output|result|document|summary)\b", "file_exists", "output_report.txt",
         "Confirm an output file exists after the agent runs"),

        # shell exit code checks — goal mentions exit N or verifying a process exits correctly
        (r".*\breturns?\s+exit\s+\d", "shell_exit",
         "python3 -c 'import main' && echo ok",
         "Verify shell command returns expected exit code"),
        (r"make\s+sure\b.*\b(?:exit|return|returns|works?|runs?)\b", "shell_exit",
         "python3 -c 'import main' && echo ok",
         "Verify shell command succeeds"),

        # import / startup errors
        (r"fix.*(import|startup|crash|start)", "shell_exit",
         "python3 -c 'import main' && echo ok",
         "python3 import main succeeds with exit 0"),

        # generic "make it work" shell check
        (r"fix.*(error|bug|broken|fail)", "shell_exit",
         "python3 -c 'import main' && echo ok",
         "Main module imports without error"),
    ]

    def infer_from_goal(self, goal: str) -> SuccessCriteria:
        """
        Infer a SuccessCriteria from a natural language goal string.
        Falls back to check_type="manual" if no rule matches.
        """
        text = goal.strip().lower()

        for pattern, check_type, check_value_tmpl, description in self._RULES:
            m = re.search(pattern, text)
            if m:
                # Substitute match groups into check_value
                check_value = check_value_tmpl
                for i, group in enumerate(m.groups(), start=1):
                    if group:
                        check_value = check_value.replace(f"{{{i}}}", group.strip())

                log_event("success_criteria_inferred", {
                    "goal": goal[:80],
                    "check_type": check_type,
                    "check_value": check_value[:80],
                })
                return SuccessCriteria(
                    goal=goal,
                    check_type=check_type,
                    check_value=check_value,
                    description=description,
                )

        # Default: manual review
        log_event("success_criteria_inferred", {
            "goal": goal[:80],
            "check_type": "manual",
            "check_value": "",
        })
        return SuccessCriteria(
            goal=goal,
            check_type="manual",
            check_value="",
            description="Manual confirmation required — Prometheus will ask Tate if the task is complete",
        )

    def evaluate(self, criteria: SuccessCriteria, log_path: str = "") -> bool:
        """
        Evaluate whether the success criteria is met.

        Args:
            criteria:  The SuccessCriteria to evaluate.
            log_path:  Path to the Prometheus log file (used for "log" check type).

        Returns True if criteria is satisfied, False otherwise.
        Never raises.
        """
        try:
            result = self._evaluate_inner(criteria, log_path)
            log_event("success_criteria_check", {
                "check_type": criteria.check_type,
                "check_value": criteria.check_value[:80],
                "result": result,
            })
            return result
        except Exception as exc:
            log_event("success_criteria_error", {
                "check_type": criteria.check_type,
                "error": str(exc)[:200],
            })
            return False

    def _evaluate_inner(self, criteria: SuccessCriteria, log_path: str) -> bool:
        ct = criteria.check_type
        cv = criteria.check_value

        if ct == "log":
            if not log_path:
                return False
            p = Path(log_path)
            if not p.exists():
                return False
            content = p.read_text(encoding="utf-8", errors="ignore")
            return cv.strip() in content

        if ct == "test":
            r = subprocess.run(
                cv,
                shell=True,
                capture_output=True,
                text=True,
                timeout=criteria.timeout_seconds,
            )
            return r.returncode == 0

        if ct == "file_exists":
            return Path(cv).expanduser().exists()

        if ct == "shell_exit":
            # check_value format: "command [expected_exit]"
            # If no integer suffix, expect 0
            parts = cv.rsplit(" ", 1)
            try:
                expected = int(parts[-1])
                command = " ".join(parts[:-1])
            except ValueError:
                expected = 0
                command = cv
            r = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=criteria.timeout_seconds,
            )
            return r.returncode == expected

        if ct == "manual":
            return False  # Always requires Tate's confirmation

        return False

"""
agent_base.py — Base class and shared dataclasses for Prometheus specialized agents.

All agents (Architect, Coder, Tester, Debugger) inherit from BaseAgent.
"""
from __future__ import annotations

import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from git_safety import GitSafety
from utils import log_event
from working_memory import WorkingMemory


@dataclass
class AgentTask:
    """Describes what an agent must accomplish."""
    goal: str
    context: str = ""
    files_of_interest: list[str] = field(default_factory=list)
    success_criteria: str = ""


@dataclass
class AgentResult:
    """Outcome from a single agent run."""
    agent_name: str
    success: bool
    output: str = ""
    duration_seconds: float = 0.0
    artifacts: dict[str, Any] = field(default_factory=dict)


class BaseAgent(ABC):
    """
    Abstract base for all Prometheus specialized agents.

    Subclasses must implement:
        name        — unique agent identifier string
        role_prompt — system-level role description injected into prompts
        build_prompt(task) → str

    The shared run() method handles logging, timing, and subprocess execution.
    Override _run_claude() in tests to avoid spawning real claude processes.
    """

    name: str = "base"
    role_prompt: str = "You are a helpful coding assistant."

    def __init__(
        self,
        timeout: int = 300,
        git_safety: GitSafety | None = None,
        working_memory: WorkingMemory | None = None,
    ) -> None:
        self.timeout = timeout
        self.git_safety = git_safety or GitSafety()
        self.working_memory = working_memory or WorkingMemory()

    @abstractmethod
    def build_prompt(self, task: AgentTask) -> str:
        """Construct the full prompt string for this agent and task."""

    def run(self, task: AgentTask) -> AgentResult:
        """
        Execute the agent on the given task.
        Returns an AgentResult with output and timing.
        Never raises.
        """
        t0 = time.monotonic()
        log_event("agent_run_start", {
            "agent": self.name,
            "goal": task.goal[:80],
        })
        try:
            prompt = self.build_prompt(task)
            output = self._run_claude(prompt)
            duration = time.monotonic() - t0
            success = self._assess_success(output, task)
            log_event("agent_run_complete", {
                "agent": self.name,
                "success": success,
                "duration": round(duration, 1),
                "output_len": len(output),
            })
            return AgentResult(
                agent_name=self.name,
                success=success,
                output=output,
                duration_seconds=round(duration, 2),
            )
        except Exception as exc:
            duration = time.monotonic() - t0
            log_event("agent_run_error", {
                "agent": self.name,
                "error": str(exc)[:200],
            })
            return AgentResult(
                agent_name=self.name,
                success=False,
                output=f"[ERROR: {exc}]",
                duration_seconds=round(duration, 2),
            )

    def _run_claude(self, prompt: str) -> str:
        """
        Invoke the claude CLI in headless mode and return combined output.
        Override in subclasses or tests to skip real subprocess execution.
        """
        try:
            result = subprocess.run(
                ["claude", "--print", "--dangerously-skip-permissions", prompt],
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            combined = (result.stdout or "") + (result.stderr or "")
            log_event("agent_claude_exit", {
                "agent": self.name,
                "returncode": result.returncode,
                "output_len": len(combined),
            })
            return combined
        except subprocess.TimeoutExpired:
            log_event("agent_claude_timeout", {"agent": self.name, "timeout": self.timeout})
            return f"[TIMEOUT after {self.timeout}s]"
        except FileNotFoundError:
            log_event("agent_claude_not_found", {"agent": self.name})
            return "[ERROR: claude CLI not found on PATH]"
        except Exception as exc:
            log_event("agent_claude_error", {"agent": self.name, "error": str(exc)[:200]})
            return f"[ERROR: {exc}]"

    def _assess_success(self, output: str, task: AgentTask) -> bool:
        """
        Default success heuristic: output is non-empty and doesn't start with [ERROR or [TIMEOUT.
        Subclasses may override for richer evaluation.
        """
        if not output or not output.strip():
            return False
        if output.startswith("[ERROR") or output.startswith("[TIMEOUT"):
            return False
        return True

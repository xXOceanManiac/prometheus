"""
tests/test_orchestrator.py — BaseAgent contract, sub-agents, and Orchestrator.

All git operations run against a throwaway repository (temp_git_repo fixture)
and the claude CLI is never invoked — _run_claude is always mocked.
"""
from __future__ import annotations

import time

import pytest

from prometheus.coding.agent_base import BaseAgent, AgentTask, AgentResult
from prometheus.coding.architect import ArchitectAgent
from prometheus.coding.coder import CoderAgent
from prometheus.coding.tester import TesterAgent
from prometheus.coding.debugger import DebuggerAgent
from prometheus.coding.git_safety import GitSafety
from prometheus.coding.orchestrator import (
    Orchestrator,
    OrchestrationResult,
    start_build,
    get_build_status,
)


# ── BaseAgent contract ────────────────────────────────────────────────────────

class TestBaseAgent:
    def test_abstract_contract(self):
        with pytest.raises(TypeError):
            BaseAgent()

        class _ConcreteAgent(BaseAgent):
            name = "test_agent"
            role_prompt = "test role"

            def build_prompt(self, task: AgentTask) -> str:
                return f"Prompt for: {task.goal}"

        agent = _ConcreteAgent()
        prompt = agent.build_prompt(AgentTask(goal="test goal"))
        assert "test goal" in prompt

    def test_agent_task_dataclass(self):
        task = AgentTask(
            goal="implement feature X",
            context="some context",
            files_of_interest=["foo.py", "bar.py"],
            success_criteria="all tests pass",
        )
        assert task.goal == "implement feature X"
        assert task.files_of_interest == ["foo.py", "bar.py"]

        task2 = AgentTask(goal="minimal task")
        assert task2.context == ""
        assert task2.files_of_interest == []
        assert task2.success_criteria == ""

    def test_mock_agent_run_returns_result(self):
        class _MockSuccessAgent(BaseAgent):
            name = "mock_success"
            role_prompt = "mock"

            def build_prompt(self, task: AgentTask) -> str:
                return f"mock prompt: {task.goal}"

            def _run_claude(self, prompt: str) -> str:
                return "Task completed successfully. All done."

        result = _MockSuccessAgent().run(AgentTask(goal="mock goal"))
        assert isinstance(result, AgentResult)
        assert result.agent_name == "mock_success"
        assert result.success is True
        assert result.duration_seconds >= 0


# ── Sub-agent parsing ─────────────────────────────────────────────────────────

class TestAgentParsing:
    def test_architect_parse_plan(self):
        agent = ArchitectAgent()
        plan_json = """{
          "steps": [
            {"id": 1, "description": "Create new module", "file": "new_module.py", "action": "create"}
          ],
          "files_of_interest": ["new_module.py"],
          "test_command": "python -m pytest tests/ -x -q",
          "notes": "minimal implementation"
        }"""

        plan = agent.parse_plan(f"Here is the plan:\n```json\n{plan_json}\n```\n")
        assert plan is not None
        assert plan["steps"][0]["file"] == "new_module.py"

        plan2 = agent.parse_plan(f"Here is the plan:\n{plan_json}\n")
        assert plan2 is not None and len(plan2["steps"]) == 1

        assert agent.parse_plan("No JSON here, just text.") is None

    def test_tester_parse_results(self):
        agent = TesterAgent()

        r = agent.parse_results("5 passed in 0.42s")
        assert (r["passed"], r["failed"], r["total"]) == (5, 0, 5)

        r2 = agent.parse_results("3 passed, 2 failed, 1 error in 1.0s")
        assert (r2["passed"], r2["failed"], r2["errors"], r2["total"]) == (3, 2, 1, 6)

        r3 = agent.parse_results("4 failed in 0.8s")
        assert (r3["failed"], r3["passed"]) == (4, 0)

        assert agent.parse_results("")["total"] == 0


# ── Orchestrator (all agents mocked, temp repo) ──────────────────────────────

class _MockArchitectSuccess(ArchitectAgent):
    def _run_claude(self, prompt: str) -> str:
        return (
            '```json\n'
            '{"steps": [{"id": 1, "description": "create module", "file": "x.py", "action": "create"}],'
            '"files_of_interest": ["x.py"], "test_command": "true", "notes": ""}'
            '\n```'
        )


class _MockCoderSuccess(CoderAgent):
    def _run_claude(self, prompt: str) -> str:
        return "Created x.py with the required implementation."


class _MockTesterAllPass(TesterAgent):
    def _run_claude(self, prompt: str) -> str:
        return "3 passed in 0.11s"


class _MockTesterFailThenPass(TesterAgent):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._call_count = 0

    def _run_claude(self, prompt: str) -> str:
        self._call_count += 1
        if self._call_count < 3:
            return "2 failed in 0.5s"
        return "3 passed in 0.5s"


class _MockTesterAlwaysFail(TesterAgent):
    def _run_claude(self, prompt: str) -> str:
        return "3 failed in 0.5s"


class _MockDebuggerNoop(DebuggerAgent):
    def _run_claude(self, prompt: str) -> str:
        return "No fix needed."


class TestOrchestrator:
    def test_success_path(self, temp_git_repo):
        orc = Orchestrator(
            architect=_MockArchitectSuccess(),
            coder=_MockCoderSuccess(),
            tester=_MockTesterAllPass(),
            debugger=_MockDebuggerNoop(),
            git_safety=GitSafety(repo_root=temp_git_repo),
        )
        result = orc.run(goal="create module x")

        assert isinstance(result, OrchestrationResult)
        assert result.success is True
        for phase in ("architect", "coder", "tester"):
            assert phase in result.phases_completed
        assert result.test_results.get("passed", 0) >= 1
        assert len(result.checkpoint_sha) == 8
        assert result.needs_human is False

    def test_debug_loop_recovers(self, temp_git_repo):
        orc = Orchestrator(
            architect=_MockArchitectSuccess(),
            coder=_MockCoderSuccess(),
            tester=_MockTesterFailThenPass(),
            debugger=_MockDebuggerNoop(),
            git_safety=GitSafety(repo_root=temp_git_repo),
            max_debug_cycles=3,
        )
        result = orc.run(goal="create module with debug cycles")

        assert result.success is True
        debug_phases = [p for p in result.phases_completed if p.startswith("debugger_")]
        assert len(debug_phases) >= 1
        assert result.needs_human is False

    def test_debug_limit_sets_needs_human(self, temp_git_repo):
        orc = Orchestrator(
            architect=_MockArchitectSuccess(),
            coder=_MockCoderSuccess(),
            tester=_MockTesterAlwaysFail(),
            debugger=_MockDebuggerNoop(),
            git_safety=GitSafety(repo_root=temp_git_repo),
            max_debug_cycles=2,
        )
        result = orc.run(goal="feature that always fails tests")

        assert result.success is False
        assert result.needs_human is True
        debug_phases = [p for p in result.phases_completed if p.startswith("debugger_")]
        assert len(debug_phases) == 2
        assert len(result.checkpoint_sha) == 8


# ── Background dispatch (no real CLI, no real repo) ──────────────────────────

class TestBackgroundDispatch:
    def test_start_build_is_immediate_and_tracked(self, temp_git_repo, monkeypatch):
        import prometheus.coding.agent_base as ab_mod
        import prometheus.coding.git_safety as gs_mod

        # Isolate: checkpoints land in the temp repo, claude never runs
        monkeypatch.setattr(gs_mod, "_REPO_ROOT", temp_git_repo)
        monkeypatch.setattr(
            ab_mod.BaseAgent, "_run_claude",
            lambda self, prompt: "mocked output", raising=True,
        )

        from prometheus.memory.working_memory import WorkingMemory
        WorkingMemory().write({"last_orchestration_result": None})

        t0 = time.time()
        result = start_build("write a hello world module")
        elapsed = time.time() - t0

        assert elapsed < 1.0, f"start_build took {elapsed:.2f}s"
        assert result.get("status") == "started"
        assert "goal" in result

        deadline = time.time() + 5.0
        status: dict = {}
        while time.time() < deadline:
            time.sleep(0.1)
            status = get_build_status()
            if isinstance(status, dict) and status.get("status") not in (
                "no build running", None,
            ):
                break

        assert isinstance(status, dict)
        assert status.get("status") not in ("no build running", None)
        assert status.get("goal") or status.get("started_at")


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))

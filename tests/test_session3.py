"""
tests/test_session3.py — Session 3 test suite.

Tests BaseAgent, ArchitectAgent, TesterAgent, DebuggerAgent, CoderAgent,
and Orchestrator (mocked). All tests must pass.
Run with:  python3 tests/test_session3.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_base import BaseAgent, AgentTask, AgentResult
from agents.architect import ArchitectAgent
from agents.coder import CoderAgent
from agents.tester import TesterAgent
from agents.debugger import DebuggerAgent
from orchestrator import Orchestrator, OrchestrationResult, start_build, get_build_status

_PASS = "✅"
_FAIL = "❌"


def _run_test(name: str, fn) -> bool:
    try:
        fn()
        return True
    except AssertionError as exc:
        print(f"{_FAIL} {name} — AssertionError: {exc}")
        return False
    except Exception as exc:
        print(f"{_FAIL} {name} — {type(exc).__name__}: {exc}")
        return False


# ──────────────────────────────────────────────────────────
# Test 1 — BaseAgent contract: subclass must implement build_prompt
# ──────────────────────────────────────────────────────────

def test_1_base_agent_abstract():
    """BaseAgent cannot be instantiated directly without build_prompt."""
    try:
        agent = BaseAgent()
        raise AssertionError("BaseAgent should not be instantiable without build_prompt")
    except TypeError:
        pass  # expected — it's abstract

    # Concrete subclass works
    class _ConcreteAgent(BaseAgent):
        name = "test_agent"
        role_prompt = "test role"

        def build_prompt(self, task: AgentTask) -> str:
            return f"Prompt for: {task.goal}"

    agent = _ConcreteAgent()
    task = AgentTask(goal="test goal")
    prompt = agent.build_prompt(task)
    assert "test goal" in prompt, f"build_prompt did not use task.goal: {prompt!r}"
    print(f"{_PASS} Test 1 — BaseAgent abstract contract enforced, concrete subclass works")


# ──────────────────────────────────────────────────────────
# Test 2 — AgentTask dataclass fields
# ──────────────────────────────────────────────────────────

def test_2_agent_task_dataclass():
    task = AgentTask(
        goal="implement feature X",
        context="some context",
        files_of_interest=["foo.py", "bar.py"],
        success_criteria="all tests pass",
    )
    assert task.goal == "implement feature X"
    assert task.context == "some context"
    assert task.files_of_interest == ["foo.py", "bar.py"]
    assert task.success_criteria == "all tests pass"

    # Defaults
    task2 = AgentTask(goal="minimal task")
    assert task2.context == ""
    assert task2.files_of_interest == []
    assert task2.success_criteria == ""
    print(f"{_PASS} Test 2 — AgentTask dataclass fields and defaults correct")


# ──────────────────────────────────────────────────────────
# Test 3 — ArchitectAgent.parse_plan parses JSON from fenced block
# ──────────────────────────────────────────────────────────

def test_3_architect_parse_plan():
    agent = ArchitectAgent()

    plan_json = """{
      "steps": [
        {"id": 1, "description": "Create new module", "file": "new_module.py", "action": "create"}
      ],
      "files_of_interest": ["new_module.py"],
      "test_command": "python -m pytest tests/ -x -q",
      "notes": "minimal implementation"
    }"""

    # In fenced block
    fenced_output = f"Here is the plan:\n```json\n{plan_json}\n```\n"
    plan = agent.parse_plan(fenced_output)
    assert plan is not None, "parse_plan returned None for fenced JSON"
    assert isinstance(plan.get("steps"), list), f"expected steps list, got: {plan}"
    assert len(plan["steps"]) == 1
    assert plan["steps"][0]["file"] == "new_module.py"

    # Bare JSON (no fence)
    bare_output = f"Here is the plan:\n{plan_json}\n"
    plan2 = agent.parse_plan(bare_output)
    assert plan2 is not None, "parse_plan returned None for bare JSON"
    assert len(plan2["steps"]) == 1

    # No JSON → None
    plan3 = agent.parse_plan("No JSON here, just text.")
    assert plan3 is None, f"parse_plan should return None for non-JSON output, got: {plan3}"

    print(f"{_PASS} Test 3 — ArchitectAgent.parse_plan handles fenced, bare, and missing JSON")


# ──────────────────────────────────────────────────────────
# Test 4 — TesterAgent.parse_results extracts pass/fail counts
# ──────────────────────────────────────────────────────────

def test_4_tester_parse_results():
    agent = TesterAgent()

    # Typical pytest output
    output_pass = "5 passed in 0.42s"
    r = agent.parse_results(output_pass)
    assert r["passed"] == 5, f"expected 5 passed, got {r}"
    assert r["failed"] == 0
    assert r["total"] == 5

    # Mixed
    output_mixed = "3 passed, 2 failed, 1 error in 1.0s"
    r2 = agent.parse_results(output_mixed)
    assert r2["passed"] == 3, f"expected 3 passed, got {r2}"
    assert r2["failed"] == 2
    assert r2["errors"] == 1
    assert r2["total"] == 6

    # All fail
    output_fail = "4 failed in 0.8s"
    r3 = agent.parse_results(output_fail)
    assert r3["failed"] == 4, f"expected 4 failed, got {r3}"
    assert r3["passed"] == 0

    # Empty
    r4 = agent.parse_results("")
    assert r4["total"] == 0

    print(f"{_PASS} Test 4 — TesterAgent.parse_results handles pass, mixed, fail, and empty output")


# ──────────────────────────────────────────────────────────
# Test 5 — Mock agent run() returns correct AgentResult
# ──────────────────────────────────────────────────────────

class _MockSuccessAgent(BaseAgent):
    name = "mock_success"
    role_prompt = "mock"

    def build_prompt(self, task: AgentTask) -> str:
        return f"mock prompt: {task.goal}"

    def _run_claude(self, prompt: str) -> str:
        return "Task completed successfully. All done."


def test_5_mock_agent_run():
    agent = _MockSuccessAgent()
    task = AgentTask(goal="mock goal")
    result = agent.run(task)

    assert isinstance(result, AgentResult), f"expected AgentResult, got {type(result)}"
    assert result.agent_name == "mock_success"
    assert result.success is True, f"expected success=True, got {result.success}"
    assert result.duration_seconds >= 0
    assert "completed" in result.output.lower()
    print(f"{_PASS} Test 5 — Mock agent run() returns correct AgentResult (success path)")


# ──────────────────────────────────────────────────────────
# Test 6 — Orchestrator success path (all mocked)
# ──────────────────────────────────────────────────────────

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


class _MockDebuggerNoop(DebuggerAgent):
    def _run_claude(self, prompt: str) -> str:
        return "No fix needed."


def test_6_orchestrator_success():
    orc = Orchestrator(
        architect=_MockArchitectSuccess(),
        coder=_MockCoderSuccess(),
        tester=_MockTesterAllPass(),
        debugger=_MockDebuggerNoop(),
    )
    result = orc.run(goal="create module x")

    assert isinstance(result, OrchestrationResult), f"expected OrchestrationResult, got {type(result)}"
    assert result.success is True, f"expected success, got {result}"
    assert "architect" in result.phases_completed, f"architect not in phases: {result.phases_completed}"
    assert "coder" in result.phases_completed
    assert "tester" in result.phases_completed
    assert result.test_results.get("passed", 0) >= 1
    assert len(result.checkpoint_sha) == 8, f"bad checkpoint_sha: {result.checkpoint_sha!r}"
    assert result.needs_human is False
    print(f"{_PASS} Test 6 — Orchestrator success path: {result.phases_completed}")


# ──────────────────────────────────────────────────────────
# Test 7 — Orchestrator debug loop path (mocked)
# ──────────────────────────────────────────────────────────

class _MockTesterFailThenPass(TesterAgent):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._call_count = 0

    def _run_claude(self, prompt: str) -> str:
        self._call_count += 1
        if self._call_count < 3:
            return "2 failed in 0.5s"
        return "3 passed in 0.5s"


def test_7_orchestrator_debug_loop():
    tester = _MockTesterFailThenPass()
    orc = Orchestrator(
        architect=_MockArchitectSuccess(),
        coder=_MockCoderSuccess(),
        tester=tester,
        debugger=_MockDebuggerNoop(),
        max_debug_cycles=3,
    )
    result = orc.run(goal="create module with debug cycles")

    assert result.success is True, f"expected success after debug cycles, got {result}"
    # Should have gone through at least one debug cycle
    debug_phases = [p for p in result.phases_completed if p.startswith("debugger_")]
    assert len(debug_phases) >= 1, f"expected debug phases, got: {result.phases_completed}"
    assert result.needs_human is False
    print(f"{_PASS} Test 7 — Orchestrator debug loop: succeeded after {len(debug_phases)} debug cycle(s)")


# ──────────────────────────────────────────────────────────
# Test 8 — Orchestrator debug limit exhausted → needs_human=True, no rollback
# ──────────────────────────────────────────────────────────

class _MockTesterAlwaysFail(TesterAgent):
    def _run_claude(self, prompt: str) -> str:
        return "3 failed in 0.5s"


def test_8_orchestrator_debug_limit():
    orc = Orchestrator(
        architect=_MockArchitectSuccess(),
        coder=_MockCoderSuccess(),
        tester=_MockTesterAlwaysFail(),
        debugger=_MockDebuggerNoop(),
        max_debug_cycles=2,
    )
    result = orc.run(goal="feature that always fails tests")

    assert result.success is False, f"expected failure, got success={result.success}"
    assert result.needs_human is True, f"expected needs_human=True, got {result.needs_human}"
    # Verify debug cycles ran
    debug_phases = [p for p in result.phases_completed if p.startswith("debugger_")]
    assert len(debug_phases) == 2, f"expected 2 debug cycles, got {debug_phases}"
    assert len(result.checkpoint_sha) == 8, f"bad checkpoint_sha: {result.checkpoint_sha!r}"
    print(
        f"{_PASS} Test 8 — Orchestrator debug limit: "
        f"needs_human=True, {len(debug_phases)} cycles, checkpoint={result.checkpoint_sha}"
    )


# ──────────────────────────────────────────────────────────
# Test 9 — start_build dispatches to background thread
# ──────────────────────────────────────────────────────────

def test_9_background_dispatch():
    from working_memory import WorkingMemory
    WorkingMemory().write({"last_orchestration_result": None})

    t0 = time.time()
    result = start_build("write a hello world module")
    elapsed = time.time() - t0

    assert elapsed < 1.0, f"start_build took {elapsed:.2f}s — should be immediate"
    assert result.get("status") == "started", f"expected 'started', got {result}"
    assert "goal" in result, f"result missing 'goal': {result}"

    # Background thread writes "running" status almost immediately
    deadline = time.time() + 2.0
    status: dict = {}
    while time.time() < deadline:
        time.sleep(0.1)
        status = get_build_status()
        if isinstance(status, dict) and status.get("status") not in ("no build running", None):
            break

    assert isinstance(status, dict), f"get_build_status() returned non-dict: {status!r}"
    assert status.get("status") not in ("no build running", None), (
        f"WorkingMemory not populated after 2s: {status}"
    )
    assert status.get("goal") or status.get("started_at"), (
        f"result missing goal/started_at: {status}"
    )
    print(f"{_PASS} Test 9 — start_build dispatched to background, status: {status.get('status')!r}")


# ──────────────────────────────────────────────────────────
# Runner
# ──────────────────────────────────────────────────────────

def main():
    print("\n═══════════════════════════════════════════")
    print("  Prometheus Session 3 — Test Suite")
    print("═══════════════════════════════════════════\n")

    tests = [
        ("Test 1 — BaseAgent abstract contract", test_1_base_agent_abstract),
        ("Test 2 — AgentTask dataclass", test_2_agent_task_dataclass),
        ("Test 3 — ArchitectAgent.parse_plan", test_3_architect_parse_plan),
        ("Test 4 — TesterAgent.parse_results", test_4_tester_parse_results),
        ("Test 5 — Mock agent run() AgentResult", test_5_mock_agent_run),
        ("Test 6 — Orchestrator success path", test_6_orchestrator_success),
        ("Test 7 — Orchestrator debug loop", test_7_orchestrator_debug_loop),
        ("Test 8 — Orchestrator debug limit → needs_human", test_8_orchestrator_debug_limit),
        ("Test 9 — Background dispatch", test_9_background_dispatch),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        ok = _run_test(name, fn)
        if ok:
            passed += 1
        else:
            failed += 1

    print()
    print("═══════════════════════════════════════════")
    print(f"  Results: {passed}/{len(tests)} passed", ("✅" if failed == 0 else "❌"))
    print("═══════════════════════════════════════════\n")

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()

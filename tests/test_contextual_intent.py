"""
tests/test_contextual_intent.py — Regression tests for contextual intent resolver.

All tests use synthetic world snapshots — no LLM calls, no live filesystem reads.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from prometheus.context.contextual_intent import ContextualIntentResolver, _is_vague, resolve_command


def _resolver() -> ContextualIntentResolver:
    return ContextualIntentResolver()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _snap(**kwargs) -> dict:
    """Build a minimal world snapshot with overrides."""
    base = {
        "timestamp": "2026-05-12T10:00:00",
        "current_mission": "",
        "active_goal": "",
        "subtasks": [],
        "blockers": [],
        "next_action": "",
        "recent_activity": [],
        "recent_errors": [],
        "active_window_title": "",
        "active_app": "",
        "current_workspace": "armed",
        "focused_project": "",
        "focused_project_path": "",
        "terminal_cwd": "",
        "visible_screen_summary": "",
        "recent_files_changed": [],
        "git_branch": "",
        "git_status_short": "",
        "git_has_changes": False,
        "running_dev_servers": [],
        "calendar_now_context": None,
        "home_assistant_state": None,
    }
    base.update(kwargs)
    return base


# ── 1. "fix that" with terminal error context → debug intent ──────────────────

class Test01FixThatWithError(unittest.TestCase):
    def setUp(self):
        self.snap = _snap(
            active_app="terminal",
            active_window_title="Konsole — bash",
            focused_project="prometheus",
            focused_project_path="/home/tatel/Desktop/Jarvis.v5.1",
            recent_errors=[{
                "ts": "2026-05-12T09:55:00",
                "kind": "executor_step_failed",
                "description": "TypeError: 'NoneType' object is not subscriptable at main.py:42",
            }],
        )

    def test_fix_that_returns_result(self):
        r = _resolver().resolve("fix that", self.snap)
        self.assertIsNotNone(r)

    def test_fix_that_intent_is_debug(self):
        r = _resolver().resolve("fix that", self.snap)
        self.assertIn("error", r["intent"].lower())

    def test_fix_that_infers_error_target(self):
        r = _resolver().resolve("fix that", self.snap)
        self.assertIn("TypeError", r["inferred_target"])

    def test_fix_that_assumption_mentions_error(self):
        r = _resolver().resolve("fix that", self.snap)
        self.assertIn("error", r["user_facing_assumption"].lower())

    def test_fix_that_confidence_adequate(self):
        r = _resolver().resolve("fix that", self.snap)
        self.assertGreaterEqual(r["confidence"], 0.80)

    def test_fix_that_risk_not_dangerous(self):
        r = _resolver().resolve("fix that", self.snap)
        self.assertNotIn(r["risk"], ("dangerous",))

    def test_fix_that_no_clarification(self):
        r = _resolver().resolve("fix that", self.snap)
        self.assertFalse(r["requires_clarification"])

    def test_fix_this_also_resolves(self):
        r = _resolver().resolve("fix this", self.snap)
        self.assertIsNotNone(r)
        self.assertFalse(r["requires_clarification"])

    def test_debug_it_resolves(self):
        r = _resolver().resolve("debug it", self.snap)
        self.assertIsNotNone(r)


# ── 2. "summarize this" with screen summary → summarize_screen ───────────────

class Test02SummarizeThis(unittest.TestCase):
    def test_summarize_this_with_window(self):
        snap = _snap(
            active_window_title="Firefox — Dashboard | Vercel",
            active_app="firefox",
            visible_screen_summary="Deployment dashboard showing 3 services",
        )
        r = _resolver().resolve("summarize this", snap)
        self.assertIsNotNone(r)
        self.assertEqual(r["intent"], "summarize_screen")

    def test_summarize_this_confidence_high(self):
        snap = _snap(active_window_title="VS Code — main.py", active_app="vscode")
        r = _resolver().resolve("summarize this", snap)
        self.assertIsNotNone(r)
        self.assertGreaterEqual(r["confidence"], 0.85)

    def test_summarize_this_risk_safe(self):
        snap = _snap(active_window_title="VS Code — main.py")
        r = _resolver().resolve("summarize this", snap)
        self.assertIsNotNone(r)
        self.assertEqual(r["risk"], "safe")

    def test_summarize_no_context_clarify(self):
        snap = _snap()  # no window, no project
        r = _resolver().resolve("summarize this", snap)
        self.assertIsNotNone(r)
        self.assertTrue(r["requires_clarification"])

    def test_recap_also_resolves(self):
        snap = _snap(active_window_title="Firefox — some page")
        r = _resolver().resolve("recap the screen", snap)
        self.assertIsNotNone(r)
        self.assertFalse(r["requires_clarification"])

    def test_summarize_with_project(self):
        snap = _snap(
            focused_project="prometheus",
            focused_project_path="/home/tatel/Desktop/Jarvis.v5.1",
        )
        r = _resolver().resolve("summarize this", snap)
        self.assertIsNotNone(r)
        # Falls back to project summarize, not screen (no window)
        self.assertIn("summar", r["intent"].lower())


# ── 3. "continue" with next_action → mission action ──────────────────────────

class Test03ContinueWithNextAction(unittest.TestCase):
    def test_continue_uses_next_action(self):
        snap = _snap(
            current_mission="Build Prometheus reliability patch",
            next_action="Write regression tests for response guard",
        )
        r = _resolver().resolve("continue", snap)
        self.assertIsNotNone(r)
        self.assertEqual(r["intent"], "execute_next_action")

    def test_continue_inferred_target_is_next_action(self):
        snap = _snap(next_action="Run the audit script")
        r = _resolver().resolve("continue", snap)
        self.assertIn("audit", r["inferred_target"].lower())

    def test_continue_confidence_high(self):
        snap = _snap(next_action="Run the audit script")
        r = _resolver().resolve("continue", snap)
        self.assertGreaterEqual(r["confidence"], 0.90)

    def test_continue_falls_to_subtask(self):
        snap = _snap(
            subtasks=[{"id": "task-1", "description": "Add tests for mission state"}],
        )
        r = _resolver().resolve("continue", snap)
        self.assertIsNotNone(r)
        self.assertIn("subtask", r["intent"].lower())

    def test_continue_no_mission_clarifies(self):
        snap = _snap()
        r = _resolver().resolve("continue", snap)
        self.assertIsNotNone(r)
        self.assertTrue(r["requires_clarification"])

    def test_proceed_also_matches(self):
        snap = _snap(next_action="Deploy to staging")
        r = _resolver().resolve("proceed", snap)
        self.assertIsNotNone(r)
        self.assertEqual(r["intent"], "execute_next_action")

    def test_keep_going_also_matches(self):
        snap = _snap(next_action="Fix the auth bug")
        r = _resolver().resolve("keep going", snap)
        self.assertIsNotNone(r)


# ── 4. "what's wrong" with blockers → blocker status ─────────────────────────

class Test04WhatsWrongWithBlockers(unittest.TestCase):
    def test_whats_wrong_with_blockers(self):
        snap = _snap(
            current_mission="Ship the new login flow",
            blockers=["OAuth redirect is broken in production"],
        )
        r = _resolver().resolve("what's wrong", snap)
        self.assertIsNotNone(r)
        self.assertEqual(r["intent"], "get_mission_status")

    def test_whats_wrong_high_confidence(self):
        snap = _snap(blockers=["Build is failing on CI"])
        r = _resolver().resolve("what's wrong", snap)
        self.assertGreaterEqual(r["confidence"], 0.90)

    def test_any_errors_resolves(self):
        snap = _snap(recent_errors=[{"kind": "tool_error", "description": "git push rejected"}])
        r = _resolver().resolve("any errors", snap)
        self.assertIsNotNone(r)
        self.assertEqual(r["intent"], "get_mission_status")

    def test_show_status_resolves(self):
        snap = _snap(current_mission="Build auth system")
        r = _resolver().resolve("show me the status", snap)
        self.assertIsNotNone(r)
        self.assertEqual(r["risk"], "safe")

    def test_what_are_we_doing_resolves(self):
        snap = _snap(current_mission="Ship feature X")
        r = _resolver().resolve("what are we doing", snap)
        self.assertIsNotNone(r)
        self.assertEqual(r["intent"], "get_mission_status")


# ── 5. "open it" with no clear target → clarification ────────────────────────

class Test05OpenItNoClearTarget(unittest.TestCase):
    def test_open_it_no_project_clarifies(self):
        snap = _snap()  # no project, no focused app
        r = _resolver().resolve("open it", snap)
        self.assertIsNotNone(r)
        self.assertTrue(r["requires_clarification"])

    def test_clarifying_question_is_set(self):
        snap = _snap()
        r = _resolver().resolve("open it", snap)
        self.assertIsNotNone(r["clarifying_question"])
        self.assertGreater(len(r["clarifying_question"]), 5)

    def test_open_it_with_project_resolves(self):
        snap = _snap(
            focused_project="prometheus",
            focused_project_path="/home/tatel/Desktop/Jarvis.v5.1",
        )
        r = _resolver().resolve("open it", snap)
        self.assertIsNotNone(r)
        self.assertFalse(r["requires_clarification"])

    def test_open_the_project_with_context(self):
        snap = _snap(
            focused_project="my-app",
            focused_project_path="/home/tatel/projects/my-app",
        )
        r = _resolver().resolve("open the project", snap)
        self.assertIsNotNone(r)
        self.assertEqual(r["intent"], "open_project")
        self.assertGreaterEqual(r["confidence"], 0.90)

    def test_pull_it_up_with_project(self):
        snap = _snap(
            focused_project="prometheus",
            focused_project_path="/home/tatel/Desktop/Jarvis.v5.1",
        )
        r = _resolver().resolve("pull it up", snap)
        self.assertIsNotNone(r)
        self.assertFalse(r["requires_clarification"])


# ── 6. "ship it" → confirmation required ─────────────────────────────────────

class Test06ShipItConfirmation(unittest.TestCase):
    def test_ship_it_with_project_needs_confirmation(self):
        snap = _snap(
            focused_project="my-app",
            focused_project_path="/home/tatel/projects/my-app",
            git_branch="main",
        )
        r = _resolver().resolve("ship it", snap)
        self.assertIsNotNone(r)
        self.assertTrue(r["requires_confirmation"] or r["requires_clarification"])

    def test_ship_it_risk_high(self):
        snap = _snap(
            focused_project="my-app",
            git_branch="main",
            focused_project_path="/home/tatel/projects/my-app",
        )
        r = _resolver().resolve("ship it", snap)
        self.assertIn(r["risk"], ("high", "dangerous"))

    def test_ship_it_no_project_needs_clarification(self):
        snap = _snap()
        r = _resolver().resolve("ship it", snap)
        self.assertIsNotNone(r)
        self.assertTrue(r["requires_clarification"] or r["requires_confirmation"])

    def test_deploy_also_triggers(self):
        snap = _snap(
            focused_project="my-app",
            focused_project_path="/home/tatel/projects/my-app",
            git_branch="main",
        )
        r = _resolver().resolve("deploy it", snap)
        self.assertIsNotNone(r)
        self.assertIn(r["risk"], ("high", "dangerous"))

    def test_not_auto_executed(self):
        snap = _snap(
            focused_project="my-app",
            focused_project_path="/home/tatel/projects/my-app",
            git_branch="main",
        )
        r = _resolver().resolve("ship it", snap)
        self.assertFalse(r.get("should_execute", False))


# ── 7. "delete it" → always blocked/confirmation ─────────────────────────────

class Test07DeleteItDangerous(unittest.TestCase):
    def test_delete_it_is_dangerous(self):
        snap = _snap()
        r = _resolver().resolve("delete it", snap)
        self.assertIsNotNone(r)
        self.assertEqual(r["risk"], "dangerous")

    def test_delete_it_not_auto_executed(self):
        snap = _snap()
        r = _resolver().resolve("delete it", snap)
        self.assertFalse(r.get("should_execute", False))

    def test_delete_it_requires_confirmation(self):
        snap = _snap()
        r = _resolver().resolve("delete it", snap)
        self.assertTrue(r["requires_confirmation"])

    def test_remove_it_also_dangerous(self):
        snap = _snap()
        r = _resolver().resolve("remove this", snap)
        self.assertIsNotNone(r)
        self.assertEqual(r["risk"], "dangerous")

    def test_erase_it_dangerous(self):
        snap = _snap()
        r = _resolver().resolve("erase it", snap)
        self.assertIsNotNone(r)
        self.assertFalse(r.get("should_execute", False))


# ── 8. "run it" with project → medium risk with confirmation ─────────────────

class Test08RunItWithProject(unittest.TestCase):
    def test_run_it_with_node_project(self):
        import tempfile, os, json
        with tempfile.TemporaryDirectory() as td:
            # Create a package.json to simulate Node project
            pkg = Path(td) / "package.json"
            pkg.write_text(json.dumps({"name": "test-app", "scripts": {"start": "node index.js"}}))
            snap = _snap(
                focused_project="test-app",
                focused_project_path=td,
            )
            r = _resolver().resolve("run it", snap)
            self.assertIsNotNone(r)
            self.assertIn(r["risk"], ("medium", "high"))

    def test_run_it_no_project_clarifies(self):
        snap = _snap()
        r = _resolver().resolve("run it", snap)
        self.assertIsNotNone(r)
        self.assertTrue(r["requires_clarification"])

    def test_run_with_next_action_run(self):
        snap = _snap(
            next_action="run pytest tests/",
            focused_project_path="/home/tatel/Desktop/Jarvis.v5.1",
        )
        r = _resolver().resolve("run it", snap)
        self.assertIsNotNone(r)
        self.assertNotEqual(r["intent"], "unknown")

    def test_execute_it_also_resolves(self):
        snap = _snap(next_action="run tests")
        r = _resolver().resolve("execute it", snap)
        self.assertIsNotNone(r)

    def test_start_it_resolves(self):
        snap = _snap(next_action="start the dev server")
        r = _resolver().resolve("start it", snap)
        self.assertIsNotNone(r)


# ── 9. "check if it worked" with recent activity → verify last action ─────────

class Test09CheckIfItWorked(unittest.TestCase):
    def test_check_if_it_worked_with_next_action(self):
        snap = _snap(
            next_action="Deploy to staging",
            focused_project_path="/home/tatel/projects/my-app",
        )
        r = _resolver().resolve("check if it worked", snap)
        self.assertIsNotNone(r)
        self.assertEqual(r["intent"], "verify_last_action")

    def test_check_if_it_worked_uses_next_action(self):
        snap = _snap(next_action="Run the database migration")
        r = _resolver().resolve("check if it worked", snap)
        self.assertIn("migration", r["inferred_target"].lower())

    def test_did_it_work_also_resolves(self):
        snap = _snap(next_action="Build the Docker image")
        r = _resolver().resolve("did it work", snap)
        self.assertIsNotNone(r)

    def test_check_results_resolves(self):
        snap = _snap(
            recent_activity=["[10:00:00] tool_action — git push origin main"],
        )
        r = _resolver().resolve("check results", snap)
        self.assertIsNotNone(r)

    def test_check_risk_safe(self):
        snap = _snap(next_action="Run tests")
        r = _resolver().resolve("check if it worked", snap)
        self.assertEqual(r["risk"], "safe")


# ── 10. "pull it up" with current project → open_project ──────────────────────

class Test10PullItUp(unittest.TestCase):
    def test_pull_it_up_with_project(self):
        snap = _snap(
            focused_project="prometheus",
            focused_project_path="/home/tatel/Desktop/Jarvis.v5.1",
        )
        r = _resolver().resolve("pull it up", snap)
        self.assertIsNotNone(r)
        self.assertFalse(r["requires_clarification"])

    def test_pull_it_up_intent_is_open(self):
        snap = _snap(
            focused_project="prometheus",
            focused_project_path="/home/tatel/Desktop/Jarvis.v5.1",
        )
        r = _resolver().resolve("pull it up", snap)
        self.assertIn("open", r["intent"].lower())

    def test_pull_it_up_confidence(self):
        snap = _snap(
            focused_project="prometheus",
            focused_project_path="/home/tatel/Desktop/Jarvis.v5.1",
        )
        r = _resolver().resolve("pull it up", snap)
        self.assertGreaterEqual(r["confidence"], 0.75)

    def test_pull_it_up_no_context_clarifies(self):
        snap = _snap()
        r = _resolver().resolve("pull it up", snap)
        self.assertIsNotNone(r)
        self.assertTrue(r["requires_clarification"])

    def test_bring_it_up_also_resolves(self):
        snap = _snap(
            focused_project="prometheus",
            focused_project_path="/home/tatel/Desktop/Jarvis.v5.1",
        )
        r = _resolver().resolve("bring it up", snap)
        self.assertIsNotNone(r)


# ── 11. Vague detection ───────────────────────────────────────────────────────

class Test11VagueDetection(unittest.TestCase):
    def test_clearly_vague_commands(self):
        vague = [
            "fix that", "fix this", "fix it",
            "open it", "open the project",
            "summarize this", "run it",
            "continue", "proceed",
            "what's wrong", "ship it",
            "delete it", "check if it worked",
            "pull it up", "handle it",
        ]
        for cmd in vague:
            with self.subTest(cmd=cmd):
                self.assertTrue(_is_vague(cmd), f"'{cmd}' should be detected as vague")

    def test_specific_commands_not_vague(self):
        specific = [
            "what time is it",
            "open firefox",
            "take a screenshot",
            "open prometheus in vs code",
            "search for python asyncio tutorial",
            "what is the capital of France",
        ]
        for cmd in specific:
            with self.subTest(cmd=cmd):
                self.assertFalse(_is_vague(cmd), f"'{cmd}' should NOT be detected as vague")

    def test_non_vague_returns_none(self):
        snap = _snap()
        r = _resolver().resolve("open firefox", snap)
        # Not vague, so resolver returns None (handled by direct override)
        self.assertIsNone(r)


# ── 12. Risk policy ───────────────────────────────────────────────────────────

class Test12RiskPolicy(unittest.TestCase):
    def test_safe_high_confidence_should_execute(self):
        snap = _snap(
            current_mission="Build Prometheus",
            blockers=["Tests failing"],
            recent_errors=[{"kind": "tool_error", "description": "test failed"}],
        )
        r = _resolver().resolve("what's wrong", snap)
        self.assertIsNotNone(r)
        self.assertEqual(r["risk"], "safe")
        # High confidence + safe → should_execute=True
        if r["confidence"] >= 0.90:
            self.assertTrue(r["should_execute"])

    def test_dangerous_never_auto_executes(self):
        snap = _snap()
        r = _resolver().resolve("delete it", snap)
        self.assertFalse(r.get("should_execute", False))
        self.assertFalse(r.get("should_execute", True))

    def test_medium_risk_no_auto_execute(self):
        snap = _snap(next_action="Deploy to staging")
        r = _resolver().resolve("ship it", snap)
        self.assertFalse(r.get("should_execute", False))


if __name__ == "__main__":
    unittest.main(verbosity=2)

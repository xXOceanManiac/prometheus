"""
test_workflow_selector.py — Tests for WorkflowResolution and resolve_workflow().
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from prometheus.planning.workflow_selector import resolve_workflow, WorkflowResolution
from prometheus.planning.workflow_registry import WORKFLOWS, WorkflowDefinition


# ── WorkflowDefinition structure ─────────────────────────────────────────────

class TestWorkflowRegistry:
    def test_workflows_is_dict(self):
        assert isinstance(WORKFLOWS, dict)

    def test_ten_workflows_defined(self):
        assert len(WORKFLOWS) >= 10

    def test_all_values_are_workflow_definitions(self):
        for name, wf in WORKFLOWS.items():
            assert isinstance(wf, WorkflowDefinition), f"{name} is not WorkflowDefinition"

    def test_all_have_trigger_examples(self):
        for name, wf in WORKFLOWS.items():
            assert len(wf.trigger_examples) >= 3, f"{name} has fewer than 3 triggers"

    def test_all_have_preferred_tools(self):
        for name, wf in WORKFLOWS.items():
            assert len(wf.preferred_tools) >= 1, f"{name} has no preferred_tools"

    def test_all_have_verification_steps(self):
        for name, wf in WORKFLOWS.items():
            assert len(wf.verification_steps) >= 1, f"{name} has no verification_steps"

    def test_risk_values_valid(self):
        valid = {"none", "low", "medium", "high"}
        for name, wf in WORKFLOWS.items():
            assert wf.risk in valid, f"{name}.risk='{wf.risk}' invalid"

    def test_required_workflows_present(self):
        required = {
            "debug_current_error", "resume_current_mission", "summarize_current_project",
            "inspect_recent_changes", "check_if_it_worked", "open_active_project",
            "continue_next_action", "ship_current_project", "diagnose_blocker",
            "prepare_current_workspace",
        }
        missing = required - set(WORKFLOWS.keys())
        assert not missing, f"Missing workflows: {missing}"

    def test_ship_is_high_risk(self):
        assert WORKFLOWS["ship_current_project"].risk == "high"

    def test_debug_is_low_risk(self):
        assert WORKFLOWS["debug_current_error"].risk == "low"

    def test_resume_is_no_risk(self):
        assert WORKFLOWS["resume_current_mission"].risk == "none"


# ── WorkflowResolution dataclass ──────────────────────────────────────────────

class TestWorkflowResolution:
    def test_resolution_has_required_fields(self):
        r = resolve_workflow("hello there")
        assert hasattr(r, "matched")
        assert hasattr(r, "workflow_name")
        assert hasattr(r, "confidence")
        assert hasattr(r, "reasoning")
        assert hasattr(r, "inferred_target")
        assert hasattr(r, "preferred_tools")
        assert hasattr(r, "requires_confirmation")
        assert hasattr(r, "requires_clarification")
        assert hasattr(r, "clarification_question")

    def test_unmatched_has_zero_confidence(self):
        r = resolve_workflow("hello there")
        assert r.matched is False
        assert r.confidence == 0.0
        assert r.workflow_name is None

    def test_preferred_tools_is_list(self):
        r = resolve_workflow("what time is it")
        assert isinstance(r.preferred_tools, list)


# ── Debug workflow ────────────────────────────────────────────────────────────

class TestDebugWorkflow:
    _snap_with_error = {
        "active_project": "prometheus",
        "active_project_path": "/home/tatel/Desktop/Jarvis.v5.1",
        "recent_errors": ["TypeError: cannot unpack non-sequence NoneType"],
    }

    def test_fix_that_with_error(self):
        r = resolve_workflow("fix that", self._snap_with_error)
        assert r.matched is True
        assert r.workflow_name == "debug_current_error"
        assert r.confidence >= 0.90

    def test_fix_the_error(self):
        r = resolve_workflow("fix the error", self._snap_with_error)
        assert r.matched is True
        assert r.workflow_name == "debug_current_error"

    def test_whats_wrong_with_project(self):
        r = resolve_workflow("what's wrong", {"active_project_path": "/home/tatel/Desktop/Jarvis.v5.1"})
        assert r.matched is True
        assert r.workflow_name == "debug_current_error"

    def test_debug_without_context_needs_clarification(self):
        r = resolve_workflow("fix that", {})
        assert r.matched is False
        assert r.requires_clarification is True
        assert r.clarification_question is not None

    def test_debug_inferred_target_from_error(self):
        r = resolve_workflow("fix it", self._snap_with_error)
        assert r.inferred_target is not None
        assert "TypeError" in r.inferred_target or "prometheus" in r.inferred_target

    def test_debug_preferred_tools_populated(self):
        r = resolve_workflow("debug this", self._snap_with_error)
        assert len(r.preferred_tools) >= 2
        assert "show_logs" in r.preferred_tools


# ── Resume mission workflow ───────────────────────────────────────────────────

class TestResumeMissionWorkflow:
    _snap_with_project = {
        "active_project": "prometheus",
        "active_project_path": "/home/tatel/Desktop/Jarvis.v5.1",
    }
    _mission = "Build the Tool Capability Registry and Workflow Selector."

    def test_where_were_we_with_mission(self):
        r = resolve_workflow("where were we", self._snap_with_project, self._mission)
        assert r.matched is True
        assert r.workflow_name == "resume_current_mission"
        assert r.confidence >= 0.90

    def test_what_are_we_working_on(self):
        r = resolve_workflow("what are we working on", self._snap_with_project, self._mission)
        assert r.matched is True
        assert r.workflow_name == "resume_current_mission"

    def test_pick_up_where_we_left_off(self):
        r = resolve_workflow("pick up where we left off", self._snap_with_project, self._mission)
        assert r.matched is True
        assert r.workflow_name == "resume_current_mission"

    def test_resume_without_mission_needs_clarification(self):
        r = resolve_workflow("where were we", {}, "")
        assert r.matched is False
        assert r.requires_clarification is True

    def test_resume_confidence_higher_with_mission(self):
        with_mission = resolve_workflow("resume", self._snap_with_project, self._mission)
        without_mission = resolve_workflow("resume", self._snap_with_project, "")
        assert with_mission.confidence >= without_mission.confidence


# ── Inspect changes workflow ──────────────────────────────────────────────────

class TestInspectChangesWorkflow:
    _snap = {
        "active_project": "prometheus",
        "active_project_path": "/home/tatel/Desktop/Jarvis.v5.1",
    }

    def test_what_changed_with_project(self):
        r = resolve_workflow("what changed", self._snap)
        assert r.matched is True
        assert r.workflow_name == "inspect_recent_changes"
        assert r.confidence >= 0.90

    def test_show_diff(self):
        r = resolve_workflow("show me the diff", self._snap)
        assert r.matched is True
        assert r.workflow_name == "inspect_recent_changes"

    def test_inspect_without_project_needs_clarification(self):
        r = resolve_workflow("what changed", {})
        assert r.matched is False
        assert r.requires_clarification is True

    def test_inspect_preferred_tools_include_git(self):
        r = resolve_workflow("what changed", self._snap)
        assert "git_status" in r.preferred_tools or "git_diff" in r.preferred_tools


# ── Ship workflow ─────────────────────────────────────────────────────────────

class TestShipWorkflow:
    _snap = {
        "active_project": "prometheus",
        "active_project_path": "/home/tatel/Desktop/Jarvis.v5.1",
    }

    def test_ship_it_requires_confirmation(self):
        r = resolve_workflow("ship it", self._snap)
        assert r.matched is True
        assert r.workflow_name == "ship_current_project"
        assert r.requires_confirmation is True

    def test_commit_and_push(self):
        r = resolve_workflow("commit and push", self._snap)
        assert r.matched is True
        assert r.workflow_name == "ship_current_project"
        assert r.requires_confirmation is True

    def test_ship_without_project_needs_clarification(self):
        r = resolve_workflow("ship it", {})
        assert r.matched is False
        assert r.requires_clarification is True

    def test_ship_confidence_high(self):
        r = resolve_workflow("time to ship", self._snap)
        assert r.confidence >= 0.90


# ── Verify workflow ───────────────────────────────────────────────────────────

class TestVerifyWorkflow:
    _snap = {"last_tool_action": "run_python"}

    def test_did_it_work(self):
        r = resolve_workflow("did it work", self._snap)
        assert r.matched is True
        assert r.workflow_name == "check_if_it_worked"
        assert r.confidence >= 0.90

    def test_is_it_working(self):
        r = resolve_workflow("is it working", self._snap)
        assert r.matched is True
        assert r.workflow_name == "check_if_it_worked"

    def test_verify_inferred_target_from_last_tool(self):
        r = resolve_workflow("did that work", self._snap)
        assert r.inferred_target == "run_python"

    def test_verify_preferred_tools_include_logs(self):
        r = resolve_workflow("did it work", self._snap)
        assert "show_logs" in r.preferred_tools


# ── Open project workflow ────────────────────────────────────────────────────

class TestOpenProjectWorkflow:
    _snap = {
        "active_project": "prometheus",
        "active_project_path": "/home/tatel/Desktop/Jarvis.v5.1",
    }

    def test_open_the_project(self):
        r = resolve_workflow("open the project", self._snap)
        assert r.matched is True
        assert r.workflow_name == "open_active_project"

    def test_lets_code(self):
        r = resolve_workflow("let's code", self._snap)
        assert r.matched is True
        assert r.workflow_name == "open_active_project"

    def test_open_project_without_context_needs_clarification(self):
        r = resolve_workflow("open the project", {})
        assert r.requires_clarification is True

    def test_open_app_command_not_matched(self):
        r = resolve_workflow("open firefox", self._snap)
        assert r.matched is False


# ── Continue next action workflow ─────────────────────────────────────────────

class TestContinueNextActionWorkflow:
    _snap = {"active_project": "prometheus"}
    _mission = "Implement the Workflow Selector."

    def test_keep_going_with_mission(self):
        r = resolve_workflow("keep going", self._snap, self._mission)
        assert r.matched is True
        assert r.workflow_name == "continue_next_action"

    def test_next_step(self):
        r = resolve_workflow("next step", self._snap, self._mission)
        assert r.matched is True
        assert r.workflow_name == "continue_next_action"

    def test_continue_without_mission_needs_clarification(self):
        r = resolve_workflow("keep going", self._snap, "")
        assert r.matched is False
        assert r.requires_clarification is True


# ── Diagnose blocker workflow ─────────────────────────────────────────────────

class TestDiagnoseBlockerWorkflow:
    _snap = {"recent_errors": ["Connection refused on port 8080"]}

    def test_were_stuck(self):
        r = resolve_workflow("we're stuck", self._snap)
        assert r.matched is True
        assert r.workflow_name == "diagnose_blocker"

    def test_run_diagnostics(self):
        r = resolve_workflow("run diagnostics", {})
        assert r.matched is True
        assert r.workflow_name == "diagnose_blocker"

    def test_figure_out_the_blocker(self):
        r = resolve_workflow("figure out the blocker", self._snap)
        assert r.matched is True
        assert r.workflow_name == "diagnose_blocker"

    def test_diagnose_preferred_tools(self):
        r = resolve_workflow("we're stuck", self._snap)
        assert len(r.preferred_tools) >= 2


# ── Prepare workspace workflow ────────────────────────────────────────────────

class TestPrepareWorkspaceWorkflow:
    def test_prepare_my_workspace(self):
        r = resolve_workflow("prepare my workspace", {})
        assert r.matched is True
        assert r.workflow_name == "prepare_current_workspace"

    def test_morning_setup(self):
        r = resolve_workflow("morning setup", {})
        assert r.matched is True
        assert r.workflow_name == "prepare_current_workspace"

    def test_start_the_session(self):
        r = resolve_workflow("start the session", {})
        assert r.matched is True
        assert r.workflow_name == "prepare_current_workspace"


# ── Summarize project workflow ────────────────────────────────────────────────

class TestSummarizeProjectWorkflow:
    _snap = {
        "active_project": "prometheus",
        "active_project_path": "/home/tatel/Desktop/Jarvis.v5.1",
    }

    def test_summarize_the_project(self):
        r = resolve_workflow("summarize the project", self._snap)
        assert r.matched is True
        assert r.workflow_name == "summarize_current_project"

    def test_project_overview(self):
        r = resolve_workflow("project overview", self._snap)
        assert r.matched is True
        assert r.workflow_name == "summarize_current_project"

    def test_summarize_without_project_needs_clarification(self):
        r = resolve_workflow("summarize the project", {})
        assert r.requires_clarification is True


# ── Empty command handling ────────────────────────────────────────────────────

class TestEdgeCases:
    def test_empty_command_needs_clarification(self):
        r = resolve_workflow("")
        assert r.matched is False
        assert r.requires_clarification is True
        assert r.clarification_question is not None

    def test_generic_phrase_not_matched(self):
        r = resolve_workflow("hello there", {})
        assert r.matched is False

    def test_unrelated_phrase_not_matched(self):
        r = resolve_workflow("play some music", {})
        assert r.matched is False

    def test_no_snapshot_doesnt_crash(self):
        r = resolve_workflow("fix that")
        assert isinstance(r, WorkflowResolution)

    def test_none_snapshot_handled(self):
        r = resolve_workflow("what changed", None)
        assert isinstance(r, WorkflowResolution)

    def test_confidence_always_zero_to_one(self):
        commands = [
            "fix that", "ship it", "resume", "keep going", "did it work",
            "what changed", "open the project", "hello", "",
        ]
        snap = {"active_project": "p", "active_project_path": "/tmp/p"}
        mission = "Test mission"
        for cmd in commands:
            r = resolve_workflow(cmd, snap, mission)
            assert 0.0 <= r.confidence <= 1.0, f"confidence={r.confidence} for '{cmd}'"

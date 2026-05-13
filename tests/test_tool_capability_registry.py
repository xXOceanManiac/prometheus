"""
test_tool_capability_registry.py — Tests for ToolCapability dataclass and TOOL_CAPABILITIES dict.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from prometheus.execution.tool_capability_registry import (
    TOOL_CAPABILITIES,
    ToolCapability,
    get_tool,
    tools_for_workflow,
    verifiable_tools,
    high_risk_tools,
)


# ── Registry structure ────────────────────────────────────────────────────────

class TestRegistryStructure:
    def test_registry_is_dict(self):
        assert isinstance(TOOL_CAPABILITIES, dict)

    def test_registry_has_entries(self):
        assert len(TOOL_CAPABILITIES) >= 50

    def test_all_values_are_tool_capability(self):
        for name, cap in TOOL_CAPABILITIES.items():
            assert isinstance(cap, ToolCapability), f"{name} is not ToolCapability"

    def test_keys_match_tool_names(self):
        for name, cap in TOOL_CAPABILITIES.items():
            assert cap.tool_name == name, f"key '{name}' != tool_name '{cap.tool_name}'"

    def test_all_action_enum_covered(self):
        required = {
            "open_app", "close_app", "open_url_key", "open_url_keys", "open_url_raw",
            "web_search", "open_code_folder", "open_terminal_here", "smart_action",
            "summarize_screen", "save_context", "resume_last_context", "run_routine",
            "save_routine", "backfill_memory", "run_dream_pass", "run_ha_script",
            "list_windows", "get_active_window", "desktop_state", "screen_context",
            "list_files", "read_file", "write_file", "mode_lock_in", "volume_change",
            "volume_set", "mute_toggle", "screenshot", "tell_time", "projector_on",
            "projector_off", "sleep", "restart", "shutdown", "confirm_pending",
            "cancel_pending", "background_task", "run_python", "run_shell", "show_logs",
            "search_codebase", "git_status", "git_diff", "git_commit", "session_wrapup",
            "system_status", "get_priorities", "start_coding_task", "get_coding_status",
            "start_build", "get_build_status", "query_vault", "run_diagnostics",
            "get_mission_status", "set_mission", "add_subtask", "complete_subtask",
        }
        missing = required - set(TOOL_CAPABILITIES.keys())
        assert not missing, f"Missing from registry: {missing}"

    def test_meta_tools_covered(self):
        meta = {"add_blocker", "clear_blocker", "set_next_action", "open_project"}
        missing = meta - set(TOOL_CAPABILITIES.keys())
        assert not missing, f"Missing meta-tools: {missing}"


# ── ToolCapability field validation ──────────────────────────────────────────

class TestToolCapabilityFields:
    def test_risk_values_valid(self):
        valid_risks = {"none", "low", "medium", "high"}
        for name, cap in TOOL_CAPABILITIES.items():
            assert cap.risk in valid_risks, f"{name}.risk='{cap.risk}' invalid"

    def test_all_have_description(self):
        for name, cap in TOOL_CAPABILITIES.items():
            assert cap.description, f"{name} has empty description"

    def test_all_have_examples(self):
        for name, cap in TOOL_CAPABILITIES.items():
            assert len(cap.examples) >= 1, f"{name} has no examples"

    def test_all_have_validates(self):
        for name, cap in TOOL_CAPABILITIES.items():
            assert cap.validates, f"{name} has empty validates"

    def test_supports_verification_is_bool(self):
        for name, cap in TOOL_CAPABILITIES.items():
            assert isinstance(cap.supports_verification, bool), f"{name}.supports_verification not bool"

    def test_workflow_tags_are_list(self):
        for name, cap in TOOL_CAPABILITIES.items():
            assert isinstance(cap.workflow_tags, list), f"{name}.workflow_tags not list"

    def test_required_slots_are_list(self):
        for name, cap in TOOL_CAPABILITIES.items():
            assert isinstance(cap.required_slots, list), f"{name}.required_slots not list"

    def test_optional_slots_are_list(self):
        for name, cap in TOOL_CAPABILITIES.items():
            assert isinstance(cap.optional_slots, list), f"{name}.optional_slots not list"

    def test_safe_when_are_list(self):
        for name, cap in TOOL_CAPABILITIES.items():
            assert isinstance(cap.safe_when, list), f"{name}.safe_when not list"

    def test_bad_for_are_list(self):
        for name, cap in TOOL_CAPABILITIES.items():
            assert isinstance(cap.bad_for, list), f"{name}.bad_for not list"


# ── High-risk tool checks ─────────────────────────────────────────────────────

class TestHighRiskTools:
    def test_shutdown_is_high_risk(self):
        assert TOOL_CAPABILITIES["shutdown"].risk == "high"

    def test_restart_is_high_risk(self):
        assert TOOL_CAPABILITIES["restart"].risk == "high"

    def test_sleep_is_high_risk(self):
        assert TOOL_CAPABILITIES["sleep"].risk == "high"

    def test_git_commit_is_high_risk(self):
        assert TOOL_CAPABILITIES["git_commit"].risk == "high"

    def test_write_file_is_medium_risk(self):
        assert TOOL_CAPABILITIES["write_file"].risk == "medium"

    def test_read_file_is_no_risk(self):
        assert TOOL_CAPABILITIES["read_file"].risk == "none"

    def test_tell_time_is_no_risk(self):
        assert TOOL_CAPABILITIES["tell_time"].risk == "none"

    def test_high_risk_tools_function(self):
        high = high_risk_tools()
        assert isinstance(high, list)
        assert "shutdown" in high
        assert "git_commit" in high
        assert "read_file" not in high


# ── Verification support ──────────────────────────────────────────────────────

class TestVerificationSupport:
    def test_write_file_supports_verification(self):
        assert TOOL_CAPABILITIES["write_file"].supports_verification is True

    def test_screenshot_supports_verification(self):
        assert TOOL_CAPABILITIES["screenshot"].supports_verification is True

    def test_git_status_supports_verification(self):
        assert TOOL_CAPABILITIES["git_status"].supports_verification is True

    def test_volume_change_no_verification(self):
        assert TOOL_CAPABILITIES["volume_change"].supports_verification is False

    def test_verifiable_tools_function(self):
        vt = verifiable_tools()
        assert isinstance(vt, list)
        assert "write_file" in vt
        assert "screenshot" in vt
        assert "volume_change" not in vt


# ── Workflow tags ────────────────────────────────────────────────────────────

class TestWorkflowTags:
    def test_open_code_folder_has_open_project_tag(self):
        assert "open_active_project" in TOOL_CAPABILITIES["open_code_folder"].workflow_tags

    def test_git_commit_has_ship_tag(self):
        assert "ship_current_project" in TOOL_CAPABILITIES["git_commit"].workflow_tags

    def test_show_logs_has_debug_tag(self):
        assert "debug_current_error" in TOOL_CAPABILITIES["show_logs"].workflow_tags

    def test_get_mission_status_has_resume_tag(self):
        assert "resume_mission" in TOOL_CAPABILITIES["get_mission_status"].workflow_tags

    def test_tools_for_workflow_function(self):
        debug_tools = tools_for_workflow("debug_current_error")
        assert isinstance(debug_tools, list)
        tool_names = {t.tool_name for t in debug_tools}
        assert "show_logs" in tool_names
        assert "search_codebase" in tool_names

    def test_tools_for_ship_workflow(self):
        ship_tools = tools_for_workflow("ship_current_project")
        tool_names = {t.tool_name for t in ship_tools}
        assert "git_commit" in tool_names
        assert "git_status" in tool_names


# ── Lookup helpers ────────────────────────────────────────────────────────────

class TestLookupHelpers:
    def test_get_tool_returns_capability(self):
        cap = get_tool("open_app")
        assert cap is not None
        assert isinstance(cap, ToolCapability)
        assert cap.tool_name == "open_app"

    def test_get_tool_returns_none_for_unknown(self):
        assert get_tool("does_not_exist") is None

    def test_get_tool_write_file(self):
        cap = get_tool("write_file")
        assert cap is not None
        assert "path" in cap.required_slots
        assert "content" in cap.required_slots


# ── Specific tool spot-checks ────────────────────────────────────────────────

class TestSpecificTools:
    def test_git_commit_requires_confirmation(self):
        cap = TOOL_CAPABILITIES["git_commit"]
        assert "confirmed" in cap.required_slots

    def test_write_file_safe_when_in_workspace(self):
        cap = TOOL_CAPABILITIES["write_file"]
        assert any("workspace" in s.lower() for s in cap.safe_when)

    def test_run_shell_bad_for_non_whitelisted(self):
        cap = TOOL_CAPABILITIES["run_shell"]
        bad = " ".join(cap.bad_for).lower()
        assert "whitelist" in bad or "rm" in bad

    def test_open_app_workflow_tags(self):
        cap = TOOL_CAPABILITIES["open_app"]
        assert len(cap.workflow_tags) >= 1

    def test_add_blocker_meta_tool(self):
        cap = TOOL_CAPABILITIES["add_blocker"]
        assert "description" in cap.required_slots
        assert "diagnose_blocker" in cap.workflow_tags

    def test_query_vault_always_safe(self):
        cap = TOOL_CAPABILITIES["query_vault"]
        assert cap.risk == "none"
        assert cap.supports_verification is True

    def test_run_diagnostics_safe(self):
        cap = TOOL_CAPABILITIES["run_diagnostics"]
        assert cap.risk == "none"

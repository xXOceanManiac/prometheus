"""
test_import_integrity.py — Verifies all major modules import cleanly after architecture reorganization.

Tests:
- All root-level modules load without ImportError
- All prometheus.* namespace paths load without ImportError
- No circular imports
- Key classes/functions initialize correctly
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


# ── Root-level module imports ─────────────────────────────────────────────────

class TestRootModuleImports:
    def test_config_imports(self):
        from config import CONFIG
        assert isinstance(CONFIG, dict)

    def test_utils_imports(self):
        from utils import log_event, command_exists, run_cmd
        assert callable(log_event)

    def test_memory_core_imports(self):
        from memory_core import read_json, write_json, norm_text
        assert callable(read_json)

    def test_working_memory_imports(self):
        from working_memory import WorkingMemory
        assert WorkingMemory is not None

    def test_mission_state_imports(self):
        from mission_state import MissionState
        ms = MissionState()
        assert ms is not None

    def test_tools_imports(self):
        from tools import ToolRegistry, ACTION_ENUM
        assert len(ACTION_ENUM) > 0

    def test_world_model_imports(self):
        from world_model import build_world_snapshot
        assert callable(build_world_snapshot)

    def test_contextual_intent_imports(self):
        from contextual_intent import ContextualIntentResolver
        assert ContextualIntentResolver is not None

    def test_event_bus_imports(self):
        from event_bus import get_bus, EventType, Event
        assert callable(get_bus)

    def test_llm_router_imports(self):
        from llm_router import chat_completion
        assert callable(chat_completion)

    def test_realtime_client_imports(self):
        from realtime_client import RealtimePrometheusClient
        assert RealtimePrometheusClient is not None

    def test_workspace_policy_imports(self):
        from workspace_policy import WORKSPACE_ROOT, resolve_workspace_path
        assert WORKSPACE_ROOT is not None
        assert callable(resolve_workspace_path)


# ── prometheus.core.* namespace ───────────────────────────────────────────────

class TestPrometheusCore:
    def test_intent_overrides_imports(self):
        from prometheus.core.intent_overrides import resolve_direct_intent, resolve_project_resume
        assert callable(resolve_direct_intent)
        assert callable(resolve_project_resume)

    def test_session_context_imports(self):
        from prometheus.core.session_context import build_instructions, build_live_state_block
        assert callable(build_instructions)
        assert callable(build_live_state_block)

    def test_tool_followups_imports(self):
        from prometheus.core.tool_followups import FOLLOWUP_ACTIONS
        assert isinstance(FOLLOWUP_ACTIONS, frozenset)
        assert "web_search" in FOLLOWUP_ACTIONS
        assert "get_mission_status" in FOLLOWUP_ACTIONS
        assert "show_logs" in FOLLOWUP_ACTIONS

    def test_core_realtime_client_wrapper(self):
        from prometheus.core.realtime_client import RealtimePrometheusClient  # noqa
        assert RealtimePrometheusClient is not None

    def test_core_prometheus_identity_wrapper(self):
        from prometheus.core.prometheus_identity import build_system_prompt  # noqa
        assert callable(build_system_prompt)

    def test_core_session_briefing_wrapper(self):
        from prometheus.core.session_briefing import SessionBriefing  # noqa
        assert SessionBriefing is not None


# ── prometheus.context.* namespace ───────────────────────────────────────────

class TestPrometheusContext:
    def test_world_model_wrapper(self):
        from prometheus.context.world_model import build_world_snapshot  # noqa
        assert callable(build_world_snapshot)

    def test_contextual_intent_wrapper(self):
        from prometheus.context.contextual_intent import ContextualIntentResolver  # noqa
        assert ContextualIntentResolver is not None

    def test_mission_state_wrapper(self):
        from prometheus.context.mission_state import MissionState  # noqa
        assert MissionState is not None


# ── prometheus.execution.* namespace ─────────────────────────────────────────

class TestPrometheusExecution:
    def test_tools_wrapper(self):
        from prometheus.execution.tools import ToolRegistry  # noqa
        assert ToolRegistry is not None

    def test_workspace_policy_wrapper(self):
        from prometheus.execution.workspace_policy import WORKSPACE_ROOT, resolve_workspace_path  # noqa
        assert callable(resolve_workspace_path)


# ── prometheus.memory.* namespace ────────────────────────────────────────────

class TestPrometheusMemory:
    def test_working_memory_wrapper(self):
        from prometheus.memory.working_memory import WorkingMemory  # noqa
        assert WorkingMemory is not None

    def test_memory_core_wrapper(self):
        from prometheus.memory.memory_core import read_json, write_json  # noqa
        assert callable(read_json)


# ── prometheus.infra.* namespace ─────────────────────────────────────────────

class TestPrometheusInfra:
    def test_utils_wrapper(self):
        from prometheus.infra.utils import log_event  # noqa
        assert callable(log_event)

    def test_config_wrapper(self):
        from prometheus.infra.config import CONFIG  # noqa
        assert isinstance(CONFIG, dict)

    def test_llm_router_wrapper(self):
        from prometheus.infra.llm_router import chat_completion  # noqa
        assert callable(chat_completion)


# ── prometheus.integrations.* namespace ──────────────────────────────────────

class TestPrometheusIntegrations:
    def test_google_calendar_imports(self):
        from prometheus.integrations.google_calendar import (
            GoogleCalendarConfig,
            GoogleCalendarResult,
            load_google_calendar_config,
            dry_run_calendar_operation,
        )
        assert callable(load_google_calendar_config)
        assert callable(dry_run_calendar_operation)

    def test_google_calendar_default_config_safe(self):
        from prometheus.integrations.google_calendar import GoogleCalendarConfig
        cfg = GoogleCalendarConfig()
        assert cfg.enabled is False
        assert cfg.dry_run is True


# ── prometheus.agents.* namespace ────────────────────────────────────────────

class TestPrometheusAgents:
    def test_lumen_ingestion_imports(self):
        from prometheus.agents.lumen_ingestion import (
            LumenIngestionResult,
            PendingCalendarProposal,
            validate_lumen_calendar_request,
            ingest_lumen_outbox_once,
            list_pending_lumen_calendar_proposals,
        )
        assert callable(validate_lumen_calendar_request)
        assert callable(ingest_lumen_outbox_once)
        assert callable(list_pending_lumen_calendar_proposals)

    def test_lumen_paths_in_infra(self):
        from prometheus.infra.paths import (
            PROMETHEUS_ECOSYSTEM_ROOT,
            LUMEN_ROOT,
            LUMEN_OUTBOX_DIR,
            LUMEN_ACCEPTED_DIR,
            LUMEN_REJECTED_DIR,
            PENDING_LUMEN_DIR,
        )
        assert LUMEN_ROOT.parent == PROMETHEUS_ECOSYSTEM_ROOT
        assert LUMEN_ROOT.name == "Lumen"


# ── prometheus.planning.* namespace ──────────────────────────────────────────

class TestPrometheusPlanning:
    def test_planner_wrapper(self):
        from prometheus.planning.planner import Planner  # noqa
        assert Planner is not None

    def test_executor_wrapper(self):
        from prometheus.planning.executor import Executor  # noqa
        assert Executor is not None


# ── Key class initialization ──────────────────────────────────────────────────

class TestClassInitialization:
    def test_tool_registry_initializes(self):
        from tools import ToolRegistry
        reg = ToolRegistry()
        assert reg is not None

    def test_mission_state_initializes(self):
        from mission_state import MissionState
        ms = MissionState()
        summary = ms.summary_text()
        assert isinstance(summary, str)

    def test_build_world_snapshot_runs(self):
        from world_model import build_world_snapshot
        snap = build_world_snapshot()
        assert isinstance(snap, dict)
        assert "timestamp" in snap

    def test_followup_actions_is_complete(self):
        from prometheus.core.tool_followups import FOLLOWUP_ACTIONS
        required = {"web_search", "git_status", "git_diff", "run_shell", "run_python",
                    "show_logs", "read_file", "list_files", "get_mission_status",
                    "get_build_status", "query_vault"}
        missing = required - FOLLOWUP_ACTIONS
        assert not missing, f"Missing from FOLLOWUP_ACTIONS: {missing}"

    def test_resolve_direct_intent_tell_time(self):
        from prometheus.core.intent_overrides import resolve_direct_intent
        result = resolve_direct_intent("what time is it")
        assert result is not None
        assert result["payload"]["action"] == "tell_time"

    def test_resolve_direct_intent_screenshot(self):
        from prometheus.core.intent_overrides import resolve_direct_intent
        result = resolve_direct_intent("take a screenshot")
        assert result is not None
        assert result["payload"]["action"] == "screenshot"

    def test_resolve_direct_intent_vault_recall(self):
        from prometheus.core.intent_overrides import resolve_direct_intent
        result = resolve_direct_intent("what do you remember about microschool")
        assert result is not None
        assert result["type"] == "vault_recall"

    def test_resolve_direct_intent_none_for_generic(self):
        from prometheus.core.intent_overrides import resolve_direct_intent
        result = resolve_direct_intent("hello there")
        assert result is None

    def test_build_instructions_returns_string(self):
        from prometheus.core.session_context import build_instructions
        result = build_instructions("You are Prometheus.", "", "")
        assert isinstance(result, str)
        assert "Prometheus" in result

    def test_build_live_state_block_returns_string(self):
        from prometheus.core.session_context import build_live_state_block
        result = build_live_state_block()
        assert isinstance(result, str)

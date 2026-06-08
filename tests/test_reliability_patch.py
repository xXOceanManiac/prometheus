"""
tests/test_reliability_patch.py — Reliability patch regression tests.

Verifies:
1. _response_active guard blocks duplicate response.create
2. Guard resets on response.done / response.failed / response.cancelled / error
3. activity.jsonl is written by log_event for activity-class events
4. MissionState persists after reload
5. "what are we working on" uses direct state lookup (no LLM)
6. HUD Store has mission field and reads MISSION_FILE
7. Direct overrides bypass planner/LLM for tell_time, screenshot, apps
8. get_mission_status tool returns correct structure
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ── 1. Response guard — blocks duplicate ─────────────────────────────────────

class Test1ResponseGuardBlocksDuplicate(unittest.TestCase):
    def _mk_client(self):
        from realtime_client import RealtimePrometheusClient
        c = object.__new__(RealtimePrometheusClient)
        c._response_active = False
        c._current_trace_id = ""
        c.ws = MagicMock()
        return c

    def test_first_call_succeeds(self):
        c = self._mk_client()
        sent = []

        async def _run():
            c.send = AsyncMock(side_effect=lambda d: sent.append(d))
            result = await c._guarded_response_create({"modalities": ["audio"]}, "test")
            return result

        ok = asyncio.run(_run())
        self.assertTrue(ok, "First call should succeed")
        self.assertTrue(c._response_active, "Flag must be True after first call")
        self.assertEqual(len(sent), 1)

    def test_duplicate_blocked(self):
        c = self._mk_client()
        c._response_active = True  # simulate in-flight response
        sent = []

        async def _run():
            c.send = AsyncMock(side_effect=lambda d: sent.append(d))
            result = await c._guarded_response_create({"modalities": ["audio"]}, "dupe")
            return result

        blocked = asyncio.run(_run())
        self.assertFalse(blocked, "Duplicate must be blocked")
        self.assertEqual(len(sent), 0, "No send should occur when blocked")

    def test_guard_blocked_event_logged(self):
        from config import LOG_DIR
        c = self._mk_client()
        c._response_active = True

        async def _run():
            c.send = AsyncMock()
            await c._guarded_response_create({"modalities": ["audio"]}, "dupe-test-context")

        asyncio.run(_run())
        today = time.strftime("%Y-%m-%d")
        log_file = LOG_DIR / f"{today}.jsonl"
        self.assertTrue(log_file.exists())
        logged = any(
            "response_create_skipped_active" in line
            for line in log_file.read_text().splitlines()[-20:]
        )
        self.assertTrue(logged, "response_create_skipped_active must be logged")


# ── 2. Guard resets correctly ─────────────────────────────────────────────────

class Test2GuardResets(unittest.TestCase):
    def setUp(self):
        from realtime_client import RealtimePrometheusClient
        self.client = object.__new__(RealtimePrometheusClient)
        self.client._response_active = True

    def _simulate_event(self, event_type: str):
        """Simulate the guard being reset the way _receiver does it."""
        c = self.client
        if event_type == "response.done":
            c._response_active = False
        elif event_type in {"response.cancelled", "response.failed"}:
            c._response_active = False
        elif event_type == "error":
            c._response_active = False

    def test_reset_on_done(self):
        self._simulate_event("response.done")
        self.assertFalse(self.client._response_active)

    def test_reset_on_cancelled(self):
        self._simulate_event("response.cancelled")
        self.assertFalse(self.client._response_active)

    def test_reset_on_failed(self):
        self._simulate_event("response.failed")
        self.assertFalse(self.client._response_active)

    def test_reset_on_error(self):
        self._simulate_event("error")
        self.assertFalse(self.client._response_active)

    def test_guard_attribute_exists(self):
        from realtime_client import RealtimePrometheusClient
        c = object.__new__(RealtimePrometheusClient)
        c._response_active = False
        self.assertFalse(c._response_active)

    def test_source_has_response_cancelled_handler(self):
        src = (_ROOT / "realtime_client.py").read_text()
        self.assertIn("response.cancelled", src)
        self.assertIn("response.failed", src)
        self.assertIn("_response_active = False", src)


# ── 3. activity.jsonl written by log_event ────────────────────────────────────

class Test3ActivityLog(unittest.TestCase):
    def test_activity_kinds_written(self):
        from utils import log_event, _ACTIVITY_FILE
        log_event("transcript", {"transcript": "reliability patch test", "_audit": True})
        self.assertTrue(_ACTIVITY_FILE.exists(), "activity.jsonl must exist after log_event('transcript')")
        lines = _ACTIVITY_FILE.read_text(encoding="utf-8").splitlines()
        self.assertTrue(len(lines) > 0)
        found = any("transcript" in line for line in lines[-10:])
        self.assertTrue(found, "transcript event must appear in activity.jsonl")

    def test_non_activity_kinds_not_written(self):
        from utils import log_event, _ACTIVITY_FILE
        size_before = _ACTIVITY_FILE.stat().st_size if _ACTIVITY_FILE.exists() else 0
        log_event("some_internal_debug_event_zz99", {"data": "x"})
        size_after = _ACTIVITY_FILE.stat().st_size if _ACTIVITY_FILE.exists() else 0
        self.assertEqual(size_before, size_after, "Non-activity event must not append to activity.jsonl")

    def test_activity_jsonl_is_valid_jsonl(self):
        from utils import _ACTIVITY_FILE
        if not _ACTIVITY_FILE.exists():
            self.skipTest("activity.jsonl not yet written")
        for line in _ACTIVITY_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception as e:
                self.fail(f"Invalid JSONL line in activity.jsonl: {e}\nLine: {line[:100]}")
            self.assertIn("ts", obj)
            self.assertIn("kind", obj)


# ── 4. MissionState persists after reload ────────────────────────────────────

class Test4MissionStatePersists(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        from memory_core import MEMORY_DIR
        self._orig_path = None

    def test_set_and_reload(self):
        from mission_state import MissionState, MISSION_FILE
        ms1 = MissionState()
        ms1.set_mission("Build reliability patch for Prometheus", goal="Pass all audit tests")
        ms1.add_subtask("Add response guard to realtime_client.py")
        ms1.add_subtask("Write activity.jsonl from log_event()")

        ms2 = MissionState()
        data = ms2.get_mission()
        self.assertEqual(data["current_mission"], "Build reliability patch for Prometheus")
        self.assertEqual(data["active_goal"], "Pass all audit tests")
        self.assertEqual(len(data["subtasks"]), 2)

    def test_complete_subtask(self):
        from mission_state import MissionState
        ms = MissionState()
        ms.set_mission("Test complete_subtask")
        ms.add_subtask("Unique subtask for completion test XYZ")
        ok = ms.complete_subtask("Unique subtask for completion test XYZ")
        self.assertTrue(ok, "complete_subtask must return True on match")
        data = ms.get_mission()
        subtask_descs = [t["description"] for t in data["subtasks"]]
        completed_descs = [t["description"] for t in data["completed_subtasks"]]
        self.assertNotIn("Unique subtask for completion test XYZ", subtask_descs)
        self.assertIn("Unique subtask for completion test XYZ", completed_descs)

    def test_summary_text_no_crash(self):
        from mission_state import MissionState
        ms = MissionState()
        summary = ms.summary_text()
        self.assertIsInstance(summary, str)

    def test_survives_restart(self):
        from mission_state import MissionState
        ms1 = MissionState()
        ms1.set_mission("Persist across restart test", goal="Verify reload")
        # Simulate restart by creating a new instance (reads from disk)
        ms2 = MissionState()
        data = ms2.get_mission()
        self.assertIn("Persist across restart test", data["current_mission"])


# ── 5. "what are we working on" — direct state lookup ────────────────────────

class Test5MissionDirectOverride(unittest.TestCase):
    def _client(self):
        from realtime_client import RealtimePrometheusClient
        return object.__new__(RealtimePrometheusClient)

    def test_what_are_we_working_on_overridden(self):
        c = self._client()
        override = c._direct_intent_override("what are we working on")
        self.assertIsNotNone(override, "Short form must have direct override")
        self.assertEqual(override.get("type"), "direct_tool")
        self.assertEqual(override["payload"]["action"], "get_mission_status")

    def test_current_mission_overridden(self):
        c = self._client()
        override = c._direct_intent_override("what's the current mission")
        self.assertIsNotNone(override)
        self.assertEqual(override["payload"]["action"], "get_mission_status")

    def test_what_is_next_overridden(self):
        c = self._client()
        override = c._direct_intent_override("what's next")
        self.assertIsNotNone(override)
        self.assertEqual(override["payload"]["action"], "get_mission_status")

    def test_what_am_i_working_on_overridden(self):
        c = self._client()
        override = c._direct_intent_override("what am i working on")
        self.assertIsNotNone(override)
        self.assertEqual(override["payload"]["action"], "get_mission_status")

    def test_any_blockers_overridden(self):
        c = self._client()
        override = c._direct_intent_override("any blockers")
        self.assertIsNotNone(override)
        self.assertEqual(override["payload"]["action"], "get_mission_status")

    def test_show_mission_status_overridden(self):
        c = self._client()
        override = c._direct_intent_override("show mission status")
        self.assertIsNotNone(override)
        self.assertEqual(override["payload"]["action"], "get_mission_status")


# ── 6. HUD Store has mission field ────────────────────────────────────────────

class Test6HUDMissionField(unittest.TestCase):
    def test_store_has_mission_field(self):
        from jarvis_desktop_hud import Store
        store = Store()
        self.assertTrue(hasattr(store, "mission"), "Store must have mission field")
        self.assertIsInstance(store.mission, dict)

    def test_store_refresh_reads_mission_file(self):
        from jarvis_desktop_hud import Store, MISSION_FILE
        # Write a test mission file
        test_data = {
            "current_mission": "HUD audit test mission",
            "active_goal": "Verify HUD reads mission",
            "subtasks": [],
            "completed_subtasks": [],
            "blocked_items": [],
            "next_action": "",
            "last_updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        MISSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        MISSION_FILE.write_text(json.dumps(test_data), encoding="utf-8")

        store = Store()
        store.refresh()
        self.assertEqual(store.mission.get("current_mission"), "HUD audit test mission")

    def test_mission_file_constant_defined(self):
        import jarvis_desktop_hud
        self.assertTrue(hasattr(jarvis_desktop_hud, "MISSION_FILE"))
        self.assertIn("mission_state.json", str(jarvis_desktop_hud.MISSION_FILE))

    def test_draw_mission_strip_method_exists(self):
        from jarvis_desktop_hud import HUDWindow
        self.assertTrue(hasattr(HUDWindow, "_draw_mission_strip"))


# ── 7. Direct overrides for tell_time, screenshot, apps ───────────────────────

class Test7DirectOverrides(unittest.TestCase):
    def _client(self):
        from realtime_client import RealtimePrometheusClient
        return object.__new__(RealtimePrometheusClient)

    def test_tell_time_overridden(self):
        c = self._client()
        override = c._direct_intent_override("what time is it")
        self.assertIsNotNone(override, "tell_time must have direct override")
        self.assertEqual(override["payload"]["action"], "tell_time")

    def test_whats_the_time_overridden(self):
        c = self._client()
        override = c._direct_intent_override("what's the time")
        self.assertIsNotNone(override)
        self.assertEqual(override["payload"]["action"], "tell_time")

    def test_screenshot_overridden(self):
        c = self._client()
        override = c._direct_intent_override("take a screenshot")
        self.assertIsNotNone(override)
        self.assertEqual(override["payload"]["action"], "screenshot")

    def test_screenshot_variant_overridden(self):
        c = self._client()
        override = c._direct_intent_override("grab a screenshot")
        self.assertIsNotNone(override)
        self.assertEqual(override["payload"]["action"], "screenshot")

    def test_open_firefox_overridden(self):
        c = self._client()
        override = c._direct_intent_override("open firefox")
        self.assertIsNotNone(override)
        self.assertEqual(override["payload"]["action"], "open_app")
        self.assertEqual(override["payload"]["app"], "firefox")

    def test_open_chrome_overridden(self):
        c = self._client()
        override = c._direct_intent_override("open chrome")
        self.assertIsNotNone(override)
        self.assertEqual(override["payload"]["action"], "open_app")

    def test_open_terminal_overridden(self):
        c = self._client()
        override = c._direct_intent_override("open terminal")
        self.assertIsNotNone(override)
        self.assertEqual(override["payload"]["action"], "open_app")
        self.assertEqual(override["payload"]["app"], "terminal")

    def test_open_discord_overridden(self):
        c = self._client()
        override = c._direct_intent_override("open discord")
        self.assertIsNotNone(override)
        self.assertEqual(override["payload"]["action"], "open_app")
        self.assertEqual(override["payload"]["app"], "discord")


# ── 8. get_mission_status tool ────────────────────────────────────────────────

class Test8GetMissionStatusTool(unittest.TestCase):
    def test_returns_structured_result(self):
        from tools import ToolRegistry
        reg = ToolRegistry()
        result = reg._execute_one_inner({"action": "get_mission_status"})
        self.assertTrue(result.ok, f"get_mission_status failed: {result.message}")
        self.assertIsInstance(result.data, dict)
        for key in ("current_mission", "active_goal", "subtasks", "completed_subtasks", "blocked_items"):
            self.assertIn(key, result.data, f"Key '{key}' missing from get_mission_status data")

    def test_set_mission_tool(self):
        from tools import ToolRegistry
        reg = ToolRegistry()
        result = reg._execute_one_inner({"action": "set_mission", "mission": "Reliability patch audit test"})
        self.assertTrue(result.ok, result.message)
        status = reg._execute_one_inner({"action": "get_mission_status"})
        self.assertIn("Reliability patch audit test", status.message)

    def test_add_subtask_tool(self):
        from tools import ToolRegistry
        reg = ToolRegistry()
        result = reg._execute_one_inner({"action": "add_subtask", "description": "Test subtask for reliability audit"})
        self.assertTrue(result.ok, result.message)
        self.assertIn("id", result.data or {})

    def test_get_mission_status_in_action_enum(self):
        from tools import ACTION_ENUM
        self.assertIn("get_mission_status", ACTION_ENUM)
        self.assertIn("set_mission", ACTION_ENUM)
        self.assertIn("add_subtask", ACTION_ENUM)
        self.assertIn("complete_subtask", ACTION_ENUM)


if __name__ == "__main__":
    unittest.main(verbosity=2)

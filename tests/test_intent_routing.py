"""
tests/test_intent_routing.py — Deterministic direct-intent routing coverage.

Protects the zero-latency phrase → tool routing layer and the voice error
callback contract in the tool registry.
"""
from __future__ import annotations

import unittest


class TestDirectIntentOverride(unittest.TestCase):
    """Known phrases must short-circuit to local tools without an LLM call."""

    def _get_override(self, transcript: str):
        from realtime_client import RealtimePrometheusClient
        # Minimal instance without __init__ to avoid audio/WebSocket side effects
        client = object.__new__(RealtimePrometheusClient)
        return client._direct_intent_override(transcript)

    def test_debug_this_routes_to_coding_task(self):
        result = self._get_override("debug this for me")
        self.assertIsNotNone(result)
        self.assertEqual(result.get("type"), "direct_tool")
        self.assertEqual(result["payload"]["action"], "start_coding_task")

    def test_health_check_routes_to_diagnostics(self):
        result = self._get_override("run diagnostics")
        self.assertIsNotNone(result)
        self.assertEqual(result.get("type"), "direct_tool")
        self.assertEqual(result["payload"]["action"], "run_diagnostics")

    def test_check_my_notes_routes_to_vault_recall(self):
        result = self._get_override("check my notes about the project")
        self.assertIsNotNone(result)
        self.assertEqual(result.get("type"), "vault_recall")

    def test_pick_up_where_we_left_off_routes_to_smart_action(self):
        result = self._get_override("pick up where we left off")
        self.assertIsNotNone(result)
        self.assertEqual(result.get("type"), "direct_tool")
        self.assertEqual(result["payload"]["action"], "smart_action")

    def test_background_tasks_routes_to_system_status(self):
        result = self._get_override("what are you working on right now")
        self.assertIsNotNone(result)
        self.assertEqual(result.get("type"), "direct_tool")
        self.assertEqual(result["payload"]["action"], "system_status")

    def test_put_together_a_site_routes_to_coding_task(self):
        result = self._get_override("put together a simple site for me")
        self.assertIsNotNone(result)
        self.assertEqual(result.get("type"), "direct_tool")
        self.assertEqual(result["payload"]["action"], "start_coding_task")

    def test_run_diagnostics_in_action_enum(self):
        from tools import ACTION_ENUM
        self.assertIn("run_diagnostics", ACTION_ENUM)


class TestVoiceErrorCallback(unittest.TestCase):
    """set_voice_error_callback / notify_voice_error contract."""

    def test_set_and_notify(self):
        import tools

        received: list = []
        tools.set_voice_error_callback(lambda a, e: received.append((a, e)))
        try:
            tools.notify_voice_error("test_action", "something went wrong")
        finally:
            tools.set_voice_error_callback(None)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][0], "test_action")
        self.assertIn("something went wrong", received[0][1])

    def test_notify_with_no_callback_is_silent(self):
        import tools
        tools.set_voice_error_callback(None)
        tools.notify_voice_error("action", "error")  # must not raise


if __name__ == "__main__":
    unittest.main()

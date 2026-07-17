"""
tests/test_world_model_integration.py — Integration tests for world_model + sensor layer.

Verifies that:
  - build_world_snapshot() returns all required fields (including new sensor fields)
  - Snapshot completes in under 100ms
  - Snapshot never throws even if all sensors are offline
  - Sensor caches are correctly reflected in snapshot
"""
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from prometheus.context.world_model import build_world_snapshot


class TestWorldSnapshotFields(unittest.TestCase):
    """Snapshot must contain all defined fields with safe defaults."""

    def setUp(self) -> None:
        self.snap = build_world_snapshot()

    # ── Core fields ──────────────────────────────────────────────────────────

    def test_has_timestamp(self) -> None:
        self.assertIn("timestamp", self.snap)
        self.assertIsInstance(self.snap["timestamp"], str)
        self.assertTrue(len(self.snap["timestamp"]) > 0)

    def test_has_mission_fields(self) -> None:
        for field in ("current_mission", "active_goal", "next_action"):
            self.assertIn(field, self.snap)
            self.assertIsInstance(self.snap[field], str)

    def test_has_list_fields(self) -> None:
        for field in ("subtasks", "blockers", "recent_activity", "recent_errors"):
            self.assertIn(field, self.snap)
            self.assertIsInstance(self.snap[field], list)

    def test_has_workspace_fields(self) -> None:
        for field in ("active_window_title", "active_app", "focused_project", "focused_project_path"):
            self.assertIn(field, self.snap)
            self.assertIsInstance(self.snap[field], str)

    def test_has_git_fields(self) -> None:
        self.assertIn("git_branch", self.snap)
        self.assertIn("git_status_short", self.snap)
        self.assertIn("git_has_changes", self.snap)
        self.assertIsInstance(self.snap["git_has_changes"], bool)

    # ── New sensor fields ─────────────────────────────────────────────────────

    def test_has_selected_text_field(self) -> None:
        self.assertIn("selected_text", self.snap)
        self.assertIsInstance(self.snap["selected_text"], str)

    def test_has_recent_file_changes_field(self) -> None:
        self.assertIn("recent_file_changes", self.snap)
        self.assertIsInstance(self.snap["recent_file_changes"], list)

    def test_has_running_dev_processes_field(self) -> None:
        self.assertIn("running_dev_processes", self.snap)
        self.assertIsInstance(self.snap["running_dev_processes"], list)

    def test_has_running_dev_servers_legacy_field(self) -> None:
        # Backward compat — legacy field still present
        self.assertIn("running_dev_servers", self.snap)
        self.assertIsInstance(self.snap["running_dev_servers"], list)


class TestWorldSnapshotPerformance(unittest.TestCase):
    """Snapshot must be fast — under 100ms even with sensors running."""

    def test_snapshot_completes_under_100ms(self) -> None:
        start = time.monotonic()
        build_world_snapshot()
        elapsed_ms = (time.monotonic() - start) * 1000
        self.assertLess(elapsed_ms, 100, f"Snapshot took {elapsed_ms:.1f}ms (limit: 100ms)")

    def test_snapshot_fast_with_all_sensors_mocked(self) -> None:
        def slow_cache():
            time.sleep(0)  # Should not actually block
            return {}

        with patch("prometheus.sensors.window_sensor.get_cache", side_effect=slow_cache):
            start = time.monotonic()
            build_world_snapshot()
            elapsed_ms = (time.monotonic() - start) * 1000
        self.assertLess(elapsed_ms, 100)


class TestWorldSnapshotRobustness(unittest.TestCase):
    """Snapshot must never throw, even if every sensor explodes."""

    def test_never_raises_if_sensors_unavailable(self) -> None:
        with patch("prometheus.sensors.window_sensor.get_cache", side_effect=ImportError):
            with patch("prometheus.sensors.clipboard_sensor.get_cache", side_effect=RuntimeError):
                with patch("prometheus.sensors.filesystem_sensor.get_cache", side_effect=OSError):
                    with patch("prometheus.sensors.error_sensor.get_cache", side_effect=Exception):
                        with patch("prometheus.sensors.process_sensor.get_cache", side_effect=Exception):
                            snap = build_world_snapshot()
        # Should return a valid dict with safe defaults
        self.assertIsInstance(snap, dict)
        self.assertEqual(snap["selected_text"], "")
        self.assertEqual(snap["recent_file_changes"], [])
        self.assertEqual(snap["running_dev_processes"], [])

    def test_never_raises_if_sensor_modules_missing(self) -> None:
        with patch.dict("sys.modules", {"sensors.window_sensor": None}):
            snap = build_world_snapshot()
        self.assertIsInstance(snap, dict)

    def test_returns_valid_snapshot_when_no_files_on_disk(self) -> None:
        with patch("pathlib.Path.exists", return_value=False):
            snap = build_world_snapshot()
        self.assertIsInstance(snap, dict)
        self.assertIn("timestamp", snap)


class TestWorldSnapshotSensorIntegration(unittest.TestCase):
    """Sensor caches are correctly reflected in the snapshot."""

    def test_window_sensor_title_appears_in_snapshot(self) -> None:
        fake_cache = {
            "window_title": "VS Code — contextual_intent.py",
            "app_name": "vscode",
            "window_class": "code",
            "updated_at": "2026-05-12T10:00:00",
        }
        with patch("prometheus.sensors.window_sensor.get_cache", return_value=fake_cache):
            snap = build_world_snapshot()
        self.assertEqual(snap["active_window_title"], "VS Code — contextual_intent.py")
        self.assertEqual(snap["active_app"], "vscode")

    def test_clipboard_text_appears_in_snapshot(self) -> None:
        fake_cache = {
            "selected_text": "def fix_that(): pass",
            "char_count": 20,
            "updated_at": "2026-05-12T10:00:00",
        }
        with patch("prometheus.sensors.clipboard_sensor.get_cache", return_value=fake_cache):
            snap = build_world_snapshot()
        self.assertEqual(snap["selected_text"], "def fix_that(): pass")

    def test_file_changes_appear_in_snapshot(self) -> None:
        fake_changes = [
            {"filename": "main.py", "change_type": "MODIFY", "project": "prometheus", "timestamp": "2026-05-12T10:00:00"},
            {"filename": "tools.py", "change_type": "MODIFY", "project": "prometheus", "timestamp": "2026-05-12T10:00:01"},
        ]
        with patch("prometheus.sensors.filesystem_sensor.get_cache", return_value=fake_changes):
            snap = build_world_snapshot()
        self.assertEqual(len(snap["recent_file_changes"]), 2)
        self.assertEqual(snap["recent_file_changes"][0]["filename"], "main.py")

    def test_live_errors_appear_in_snapshot(self) -> None:
        fake_errors = [
            {
                "source": "journalctl",
                "raw_line": "ERROR: database connection refused",
                "error_pattern": "ERROR",
                "severity": "error",
                "timestamp": "2026-05-12T10:00:00",
            }
        ]
        with patch("prometheus.sensors.error_sensor.get_cache", return_value=fake_errors):
            snap = build_world_snapshot()
        self.assertEqual(len(snap["recent_errors"]), 1)
        self.assertIn("database connection refused", snap["recent_errors"][0]["description"])

    def test_dev_processes_appear_in_snapshot(self) -> None:
        fake_procs = [
            {"pid": 1234, "name": "uvicorn", "cmdline_summary": "uvicorn main:app --reload"},
        ]
        with patch("prometheus.sensors.process_sensor.get_cache", return_value=fake_procs):
            snap = build_world_snapshot()
        self.assertEqual(len(snap["running_dev_processes"]), 1)
        self.assertEqual(snap["running_dev_processes"][0]["name"], "uvicorn")

    def test_live_errors_take_precedence_over_activity_log(self) -> None:
        fake_live = [
            {
                "source": "journalctl",
                "raw_line": "ERROR: live sensor error",
                "error_pattern": "ERROR",
                "severity": "error",
                "timestamp": "2026-05-12T10:00:00",
            }
        ]
        with patch("prometheus.sensors.error_sensor.get_cache", return_value=fake_live):
            snap = build_world_snapshot()
        # Live sensor errors should populate recent_errors
        self.assertTrue(any("live sensor error" in e["description"] for e in snap["recent_errors"]))

    def test_file_changes_capped_at_5(self) -> None:
        fake_changes = [
            {"filename": f"file{i}.py", "change_type": "MODIFY", "project": "p", "timestamp": "2026-05-12T10:00:00"}
            for i in range(10)
        ]
        with patch("prometheus.sensors.filesystem_sensor.get_cache", return_value=fake_changes):
            snap = build_world_snapshot()
        self.assertLessEqual(len(snap["recent_file_changes"]), 5)


if __name__ == "__main__":
    unittest.main()

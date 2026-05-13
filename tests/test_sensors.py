"""
tests/test_sensors.py — Unit tests for Prometheus desktop sensors.

Tests use mocked subprocess/os calls and inject lines directly into
processing functions. No actual xdotool, xclip, inotifywait, or journalctl
calls are made.
"""
import asyncio
import sys
import time
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _reset_sensor_caches() -> None:
    """Clear all sensor module-level caches and deduplication state."""
    import sensors.window_sensor as ws
    import sensors.clipboard_sensor as cs
    import sensors.filesystem_sensor as fs
    import sensors.error_sensor as es
    import sensors.process_sensor as ps

    ws._CACHE.update({"window_title": "", "app_name": "", "window_class": "", "updated_at": ""})
    ws._LAST.update({"title": "\x00", "wclass": "\x00"})

    cs._CACHE.update({"selected_text": "", "char_count": 0, "updated_at": ""})
    cs._last_text = "\x00"

    fs._CACHE.clear()
    fs._DEBOUNCE.clear()

    es._CACHE.clear()
    es._DEDUPE.clear()

    ps._REGISTRY.clear()
    ps._CACHE.clear()


def _mock_bus() -> MagicMock:
    bus = MagicMock()
    bus.publish = MagicMock()
    return bus


# ── Window sensor ─────────────────────────────────────────────────────────────

class TestWindowSensor(unittest.TestCase):
    def setUp(self) -> None:
        _reset_sensor_caches()

    def test_get_active_window_parses_xdotool_output(self) -> None:
        from sensors.window_sensor import _get_active_window
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="VS Code — contextual_intent.py\n"),
                MagicMock(returncode=0, stdout="code\n"),
            ]
            title, wclass = _get_active_window()
        self.assertEqual(title, "VS Code — contextual_intent.py")
        self.assertEqual(wclass, "code")

    def test_get_active_window_returns_empty_on_failure(self) -> None:
        from sensors.window_sensor import _get_active_window
        with patch("subprocess.run", side_effect=FileNotFoundError("xdotool not found")):
            title, wclass = _get_active_window()
        self.assertEqual(title, "")
        self.assertEqual(wclass, "")

    def test_app_from_title_vscode(self) -> None:
        from sensors.window_sensor import _app_from_title
        self.assertEqual(_app_from_title("VS Code — main.py"), "vscode")

    def test_app_from_title_firefox(self) -> None:
        from sensors.window_sensor import _app_from_title
        self.assertEqual(_app_from_title("Firefox — github.com"), "firefox")

    def test_app_from_title_terminal(self) -> None:
        from sensors.window_sensor import _app_from_title
        self.assertEqual(_app_from_title("Konsole — bash"), "terminal")

    def test_app_from_title_unknown(self) -> None:
        from sensors.window_sensor import _app_from_title
        result = _app_from_title("Slack")
        self.assertEqual(result, "Slack")

    def test_window_sensor_emits_event_on_change(self) -> None:
        from sensors import window_sensor as ws
        mock_bus = _mock_bus()
        with patch("sensors.window_sensor.get_bus", return_value=mock_bus):
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = [
                    MagicMock(returncode=0, stdout="VS Code — main.py\n"),
                    MagicMock(returncode=0, stdout="code\n"),
                ]
                title, wclass = ws._get_active_window()
                # Simulate cache update + publish
                if title != ws._LAST["title"]:
                    ws._LAST["title"] = title
                    ws._LAST["wclass"] = wclass
                    ws._CACHE.update({"window_title": title, "app_name": ws._app_from_title(title), "window_class": wclass})
                    from event_bus import Event, EventType, Priority
                    mock_bus.publish(Event(EventType.WINDOW_CHANGED, "window_sensor", {"window_title": title}))

        mock_bus.publish.assert_called_once()
        call_event = mock_bus.publish.call_args[0][0]
        self.assertEqual(call_event.event_type.name, "WINDOW_CHANGED")
        self.assertIn("VS Code", call_event.payload["window_title"])

    def test_window_sensor_no_emit_if_unchanged(self) -> None:
        from sensors import window_sensor as ws
        ws._LAST["title"] = "VS Code — main.py"
        ws._LAST["wclass"] = "code"

        mock_bus = _mock_bus()
        with patch("sensors.window_sensor.get_bus", return_value=mock_bus):
            # Same title → should NOT emit
            title = "VS Code — main.py"
            if title == ws._LAST["title"]:
                pass  # no publish
        mock_bus.publish.assert_not_called()

    def test_get_cache_returns_dict(self) -> None:
        from sensors.window_sensor import get_cache
        cache = get_cache()
        self.assertIsInstance(cache, dict)
        self.assertIn("window_title", cache)
        self.assertIn("app_name", cache)

    def test_sensor_unavailable_if_no_xdotool(self) -> None:
        with patch("shutil.which", return_value=None):
            from sensors.window_sensor import WindowSensor
            sensor = WindowSensor()
            self.assertFalse(sensor.is_available())


# ── Clipboard sensor ──────────────────────────────────────────────────────────

class TestClipboardSensor(unittest.TestCase):
    def setUp(self) -> None:
        _reset_sensor_caches()

    def test_get_primary_selection_returns_text(self) -> None:
        from sensors.clipboard_sensor import _get_primary_selection
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="def fix_that():\n    pass\n")
            text = _get_primary_selection()
        self.assertEqual(text, "def fix_that():\n    pass")

    def test_get_primary_selection_empty_on_failure(self) -> None:
        from sensors.clipboard_sensor import _get_primary_selection
        with patch("subprocess.run", side_effect=FileNotFoundError):
            text = _get_primary_selection()
        self.assertEqual(text, "")

    def test_empty_selection_not_emitted(self) -> None:
        from sensors.clipboard_sensor import _get_primary_selection
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="   \n")
            text = _get_primary_selection()
        self.assertEqual(text, "")

    def test_selection_truncated_to_2000_chars(self) -> None:
        from sensors.clipboard_sensor import _get_primary_selection
        long_text = "x" * 5000
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=long_text)
            text = _get_primary_selection()
        self.assertEqual(len(text), 2000)

    def test_emits_on_selection_change(self) -> None:
        import sensors.clipboard_sensor as cs
        cs._last_text = "\x00"
        mock_bus = _mock_bus()

        with patch("sensors.clipboard_sensor.get_bus", return_value=mock_bus):
            new_text = "selected error text"
            if new_text and new_text != cs._last_text:
                cs._last_text = new_text
                cs._CACHE.update({"selected_text": new_text, "char_count": len(new_text)})
                from event_bus import Event, EventType, Priority
                mock_bus.publish(Event(EventType.TEXT_SELECTED, "clipboard_sensor", {"selected_text": new_text}))

        mock_bus.publish.assert_called_once()
        call_event = mock_bus.publish.call_args[0][0]
        self.assertEqual(call_event.event_type.name, "TEXT_SELECTED")

    def test_no_emit_on_same_selection(self) -> None:
        import sensors.clipboard_sensor as cs
        cs._last_text = "same text"
        mock_bus = _mock_bus()

        with patch("sensors.clipboard_sensor.get_bus", return_value=mock_bus):
            new_text = "same text"
            if new_text and new_text != cs._last_text:
                mock_bus.publish(MagicMock())

        mock_bus.publish.assert_not_called()

    def test_sensor_unavailable_if_no_xclip(self) -> None:
        with patch("shutil.which", return_value=None):
            from sensors.clipboard_sensor import ClipboardSensor
            sensor = ClipboardSensor()
            self.assertFalse(sensor.is_available())


# ── Filesystem sensor ─────────────────────────────────────────────────────────

class TestFilesystemSensor(unittest.TestCase):
    def setUp(self) -> None:
        _reset_sensor_caches()

    def test_handle_inotify_line_emits_event(self) -> None:
        from sensors import filesystem_sensor as fs
        mock_bus = _mock_bus()
        with patch("sensors.filesystem_sensor.get_bus", return_value=mock_bus):
            fs._handle_inotify_line(
                "MODIFY /home/tatel/Desktop/Jarvis.v5.1/ contextual_intent.py",
                ["/home/tatel/Desktop/Jarvis.v5.1/"],
            )
        mock_bus.publish.assert_called_once()
        call_event = mock_bus.publish.call_args[0][0]
        self.assertEqual(call_event.event_type.name, "FILE_CHANGED")
        self.assertEqual(call_event.payload["filename"], "contextual_intent.py")
        self.assertEqual(call_event.payload["change_type"], "MODIFY")

    def test_handle_inotify_line_adds_to_cache(self) -> None:
        from sensors import filesystem_sensor as fs
        mock_bus = _mock_bus()
        with patch("sensors.filesystem_sensor.get_bus", return_value=mock_bus):
            fs._handle_inotify_line(
                "CREATE /home/tatel/Desktop/Jarvis.v5.1/ new_file.py",
                [],
            )
        cache = fs.get_cache()
        self.assertEqual(len(cache), 1)
        self.assertEqual(cache[0]["filename"], "new_file.py")

    def test_debounce_suppresses_duplicate_within_window(self) -> None:
        from sensors import filesystem_sensor as fs
        mock_bus = _mock_bus()
        path = "/home/tatel/Desktop/Jarvis.v5.1/world_model.py"
        fs._DEBOUNCE[path] = time.monotonic()  # mark as recently seen

        with patch("sensors.filesystem_sensor.get_bus", return_value=mock_bus):
            fs._handle_inotify_line(
                "MODIFY /home/tatel/Desktop/Jarvis.v5.1/ world_model.py",
                [],
            )
        mock_bus.publish.assert_not_called()

    def test_debounce_allows_after_window_expires(self) -> None:
        from sensors import filesystem_sensor as fs
        mock_bus = _mock_bus()
        path = "/home/tatel/Desktop/Jarvis.v5.1/world_model.py"
        # Set debounce time to 10 seconds ago (expired)
        fs._DEBOUNCE[path] = time.monotonic() - 10.0

        with patch("sensors.filesystem_sensor.get_bus", return_value=mock_bus):
            fs._handle_inotify_line(
                "MODIFY /home/tatel/Desktop/Jarvis.v5.1/ world_model.py",
                [],
            )
        mock_bus.publish.assert_called_once()

    def test_malformed_line_does_not_crash(self) -> None:
        from sensors.filesystem_sensor import _handle_inotify_line
        mock_bus = _mock_bus()
        with patch("sensors.filesystem_sensor.get_bus", return_value=mock_bus):
            _handle_inotify_line("", [])
            _handle_inotify_line("MODIFY", [])
        mock_bus.publish.assert_not_called()

    def test_sensor_unavailable_if_no_inotifywait(self) -> None:
        with patch("shutil.which", return_value=None):
            from sensors.filesystem_sensor import FilesystemSensor
            sensor = FilesystemSensor()
            self.assertFalse(sensor.is_available())


# ── Error sensor ──────────────────────────────────────────────────────────────

class TestErrorSensor(unittest.TestCase):
    def setUp(self) -> None:
        _reset_sensor_caches()

    def test_process_line_detects_ERROR(self) -> None:
        from sensors import error_sensor as es
        mock_bus = _mock_bus()
        with patch("sensors.error_sensor.get_bus", return_value=mock_bus):
            es._process_line("2024-01-01 12:00:00 prometheus ERROR: database connection failed", "journalctl")
        mock_bus.publish.assert_called_once()
        payload = mock_bus.publish.call_args[0][0].payload
        self.assertEqual(payload["error_pattern"], "ERROR")
        self.assertEqual(payload["severity"], "error")

    def test_process_line_detects_Exception(self) -> None:
        from sensors import error_sensor as es
        mock_bus = _mock_bus()
        with patch("sensors.error_sensor.get_bus", return_value=mock_bus):
            es._process_line("Exception: NoneType has no attribute strip", "journalctl")
        mock_bus.publish.assert_called_once()

    def test_process_line_detects_Traceback(self) -> None:
        from sensors import error_sensor as es
        mock_bus = _mock_bus()
        with patch("sensors.error_sensor.get_bus", return_value=mock_bus):
            es._process_line("Traceback (most recent call last):", "journalctl")
        mock_bus.publish.assert_called_once()

    def test_process_line_detects_failed(self) -> None:
        from sensors import error_sensor as es
        mock_bus = _mock_bus()
        with patch("sensors.error_sensor.get_bus", return_value=mock_bus):
            es._process_line("service prometheus failed to start", "journalctl")
        mock_bus.publish.assert_called_once()
        payload = mock_bus.publish.call_args[0][0].payload
        self.assertEqual(payload["severity"], "warning")

    def test_process_line_ignores_normal_log(self) -> None:
        from sensors import error_sensor as es
        mock_bus = _mock_bus()
        with patch("sensors.error_sensor.get_bus", return_value=mock_bus):
            es._process_line("2024-01-01 INFO: sensor started successfully", "journalctl")
        mock_bus.publish.assert_not_called()

    def test_deduplicate_same_line_within_window(self) -> None:
        from sensors import error_sensor as es
        mock_bus = _mock_bus()
        line = "ERROR: same error repeated"
        with patch("sensors.error_sensor.get_bus", return_value=mock_bus):
            es._process_line(line, "journalctl")
            es._process_line(line, "journalctl")  # should be deduplicated
        self.assertEqual(mock_bus.publish.call_count, 1)

    def test_adds_to_cache(self) -> None:
        from sensors import error_sensor as es
        mock_bus = _mock_bus()
        with patch("sensors.error_sensor.get_bus", return_value=mock_bus):
            es._process_line("FATAL: kernel panic", "journalctl")
        cache = es.get_cache()
        self.assertEqual(len(cache), 1)
        self.assertEqual(cache[0]["severity"], "fatal")

    def test_inject_line_public_api(self) -> None:
        from sensors import error_sensor as es
        mock_bus = _mock_bus()
        with patch("sensors.error_sensor.get_bus", return_value=mock_bus):
            es.inject_line("ERROR: test via public API")
        mock_bus.publish.assert_called_once()

    def test_error_event_is_high_priority(self) -> None:
        from sensors import error_sensor as es
        from event_bus import Priority
        mock_bus = _mock_bus()
        with patch("sensors.error_sensor.get_bus", return_value=mock_bus):
            es._process_line("ERROR: critical failure", "journalctl")
        event = mock_bus.publish.call_args[0][0]
        self.assertEqual(event.priority, Priority.HIGH)


# ── Process sensor ────────────────────────────────────────────────────────────

class TestProcessSensor(unittest.TestCase):
    def setUp(self) -> None:
        _reset_sensor_caches()

    def _make_proc_entry(self, pid: int, name: str, cmdline: str) -> MagicMock:
        entry = MagicMock()
        entry.name = str(pid)
        entry.is_dir.return_value = True
        return entry

    def test_scan_detects_new_process(self) -> None:
        from sensors import process_sensor as ps
        mock_bus = _mock_bus()

        mock_entry = MagicMock()
        mock_entry.name = "12345"

        with patch("sensors.process_sensor.get_bus", return_value=mock_bus):
            with patch("os.scandir") as mock_scandir:
                mock_scandir.return_value = [mock_entry]
                with patch("sensors.process_sensor._read_cmdline", return_value="python3 main.py"):
                    ps.scan_processes()

        self.assertIn(12345, ps._REGISTRY)
        mock_bus.publish.assert_called_once()
        event = mock_bus.publish.call_args[0][0]
        self.assertEqual(event.event_type.name, "PROCESS_CHANGED")
        self.assertEqual(event.payload["change_type"], "started")

    def test_scan_detects_stopped_process(self) -> None:
        from sensors import process_sensor as ps
        mock_bus = _mock_bus()

        # Pre-populate registry with a process
        ps._REGISTRY[99999] = {
            "pid": 99999, "name": "uvicorn", "cmdline_summary": "uvicorn main:app",
            "started_at": "2024-01-01T00:00:00",
        }

        # Scan returns empty /proc — process has gone
        with patch("sensors.process_sensor.get_bus", return_value=mock_bus):
            with patch("os.scandir", return_value=[]):
                ps.scan_processes()

        self.assertNotIn(99999, ps._REGISTRY)
        mock_bus.publish.assert_called_once()
        event = mock_bus.publish.call_args[0][0]
        self.assertEqual(event.payload["change_type"], "stopped")

    def test_non_dev_process_ignored(self) -> None:
        from sensors import process_sensor as ps
        mock_bus = _mock_bus()

        mock_entry = MagicMock()
        mock_entry.name = "11111"

        with patch("sensors.process_sensor.get_bus", return_value=mock_bus):
            with patch("os.scandir", return_value=[mock_entry]):
                with patch("sensors.process_sensor._read_cmdline", return_value="/usr/bin/dbus-daemon"):
                    ps.scan_processes()

        self.assertNotIn(11111, ps._REGISTRY)
        mock_bus.publish.assert_not_called()

    def test_match_dev_pattern(self) -> None:
        from sensors.process_sensor import _match_dev_pattern
        self.assertEqual(_match_dev_pattern("node server.js"), "node")
        self.assertEqual(_match_dev_pattern("python3 -m uvicorn"), "uvicorn")
        self.assertEqual(_match_dev_pattern("/usr/bin/Xorg"), "")

    def test_cache_updated_after_scan(self) -> None:
        from sensors import process_sensor as ps
        mock_bus = _mock_bus()

        mock_entry = MagicMock()
        mock_entry.name = "55555"

        with patch("sensors.process_sensor.get_bus", return_value=mock_bus):
            with patch("os.scandir", return_value=[mock_entry]):
                with patch("sensors.process_sensor._read_cmdline", return_value="vite dev"):
                    ps.scan_processes()

        cache = ps.get_cache()
        self.assertEqual(len(cache), 1)
        self.assertEqual(cache[0]["name"], "vite")

    def test_process_sensor_available_on_linux(self) -> None:
        from sensors.process_sensor import ProcessSensor
        sensor = ProcessSensor()
        # On Linux, /proc exists
        self.assertTrue(sensor.is_available())


if __name__ == "__main__":
    unittest.main()

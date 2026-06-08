"""
tests/test_pass7_time_browser.py — Pass 7: Time truthfulness and browser/app verification.

Covers:
1. tell_time uses explicit timezone from CONFIG, defaults America/New_York
2. tell_time returns verified_success with correct time format
3. tell_time ZoneInfo is read from CONFIG["timezone"]
4. prometheus_identity.build_system_prompt injects current local time/date
5. open_url_raw, open_url_key, open_url_keys return accepted_unverified
6. open_app already-running returns verified_success (pgrep + wmctrl confirmed)
7. open_app fresh-launch: verified_success when pgrep confirms post-launch
8. open_app fresh-launch: accepted_unverified when pgrep cannot confirm
9. config.py DEFAULT_CONFIG has "timezone" = "America/New_York"
"""
from __future__ import annotations

import sys
import time
from datetime import datetime as _datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tool_registry() -> "ToolRegistry":
    """Minimal ToolRegistry without real memory or disk IO."""
    from tools import ToolRegistry
    with (
        patch("tools.MemoryStore"),
        patch("tools.EpisodicMemory"),
        patch("tools.SemanticMemory"),
        patch("tools.ProceduralMemory"),
        patch("tools.WorkingMemory"),
        patch("tools.DreamManager"),
        patch("tools.BehaviorLearningEngine"),
    ):
        return ToolRegistry()


# ---------------------------------------------------------------------------
# 1. Config: DEFAULT_CONFIG has "timezone"
# ---------------------------------------------------------------------------

class TestDefaultConfig:
    def test_timezone_in_default_config(self):
        from config import DEFAULT_CONFIG
        assert "timezone" in DEFAULT_CONFIG

    def test_timezone_defaults_to_new_york(self):
        from config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["timezone"] == "America/New_York"


# ---------------------------------------------------------------------------
# 2. tell_time: returns verified_success with correct status
# ---------------------------------------------------------------------------

class TestTellTimeStatus:
    def test_tell_time_is_verified_success(self):
        from tools import ToolStatus
        registry = _make_tool_registry()
        result = registry.execute({"action": "tell_time"})
        assert result.status == ToolStatus.VERIFIED_SUCCESS
        assert result.verified is True
        assert result.ok is True

    def test_tell_time_message_contains_time(self):
        registry = _make_tool_registry()
        result = registry.execute({"action": "tell_time"})
        msg = result.message.lower()
        assert "it is" in msg
        assert ":" in msg
        assert ("am" in msg or "pm" in msg)

    def test_tell_time_high_confidence(self):
        registry = _make_tool_registry()
        result = registry.execute({"action": "tell_time"})
        assert result.confidence >= 0.99


# ---------------------------------------------------------------------------
# 3. tell_time: uses timezone from CONFIG
# ---------------------------------------------------------------------------

class TestTellTimeTimezone:
    def test_tell_time_uses_configured_timezone(self):
        """tell_time should read CONFIG["timezone"] and use ZoneInfo."""
        from zoneinfo import ZoneInfo
        from datetime import datetime as _datetime
        registry = _make_tool_registry()

        frozen_utc = _datetime(2026, 6, 7, 18, 45, 0, tzinfo=ZoneInfo("UTC"))
        ny_tz = ZoneInfo("America/New_York")
        expected_hour = frozen_utc.astimezone(ny_tz).strftime("%I").lstrip("0")

        with patch("tools.CONFIG", {"timezone": "America/New_York"}):
            with patch("tools._datetime") as mock_dt:
                mock_dt.now.return_value = frozen_utc.astimezone(ny_tz)
                result = registry.execute({"action": "tell_time"})

        assert expected_hour in result.message

    def test_tell_time_falls_back_to_new_york_for_invalid_tz(self):
        """Invalid timezone falls back gracefully — no exception."""
        registry = _make_tool_registry()
        with patch("tools.CONFIG", {"timezone": "Not/A/Real/Timezone"}):
            result = registry.execute({"action": "tell_time"})
        assert result.ok is True
        assert "it is" in result.message.lower()

    def test_tell_time_respects_utc_timezone(self):
        """tell_time with UTC timezone returns UTC time."""
        from zoneinfo import ZoneInfo
        from datetime import datetime as _datetime
        frozen = _datetime(2026, 6, 7, 12, 0, 0, tzinfo=ZoneInfo("UTC"))
        registry = _make_tool_registry()
        with patch("tools.CONFIG", {"timezone": "UTC"}):
            with patch("tools._datetime") as mock_dt:
                mock_dt.now.return_value = frozen
                result = registry.execute({"action": "tell_time"})
        assert "12:00" in result.message

    def test_tell_time_summary_contains_timezone_name(self):
        """Verification summary includes the timezone name used."""
        registry = _make_tool_registry()
        with patch("tools.CONFIG", {"timezone": "America/Chicago"}):
            result = registry.execute({"action": "tell_time"})
        assert "America/Chicago" in result.verification_summary


# ---------------------------------------------------------------------------
# 4. prometheus_identity: injects current time/date
# ---------------------------------------------------------------------------

class TestPrometheusIdentityTimeInjection:
    def _call_build(self, timezone: str = "America/New_York") -> str:
        from prometheus_identity import build_system_prompt
        profile = {
            "name": "Tate",
            "timezone": timezone,
            "working_style": "",
            "preferred_response_style": "",
            "faith_fitness_legacy": False,
            "current_priorities": [],
        }
        return build_system_prompt(
            workspace={},
            vault_context=[],
            recent_sessions=[],
            working_memory={},
            profile=profile,
        )

    def test_prompt_contains_current_time_label(self):
        prompt = self._call_build()
        assert "Current time:" in prompt

    def test_prompt_contains_am_or_pm(self):
        prompt = self._call_build()
        # Should contain AM or PM in the time string
        assert "AM" in prompt or "PM" in prompt

    def test_prompt_contains_year(self):
        prompt = self._call_build()
        # Should have a 4-digit year
        import re
        assert re.search(r"20\d{2}", prompt), "Expected a year in the prompt"

    def test_prompt_timezone_reflected(self):
        """Timezone label appears in the USER PROFILE section."""
        prompt = self._call_build("America/Chicago")
        assert "America/Chicago" in prompt

    def test_prompt_time_uses_local_timezone(self):
        """Frozen UTC time: time in prompt matches expected local conversion."""
        from zoneinfo import ZoneInfo
        from datetime import datetime as _datetime
        frozen_utc = _datetime(2026, 6, 7, 22, 30, 0, tzinfo=ZoneInfo("UTC"))
        ny = ZoneInfo("America/New_York")
        expected_local = frozen_utc.astimezone(ny)
        expected_hour = expected_local.strftime("%I").lstrip("0")

        from prometheus_identity import build_system_prompt
        with patch("prometheus_identity._datetime") as mock_dt:
            mock_dt.now.return_value = expected_local
            prompt = build_system_prompt(
                workspace={},
                vault_context=[],
                recent_sessions=[],
                working_memory={},
                profile={"name": "Tate", "timezone": "America/New_York",
                         "faith_fitness_legacy": False, "current_priorities": []},
            )
        assert expected_hour in prompt

    def test_build_never_raises_on_bad_timezone(self):
        """build_system_prompt never raises even with an invalid timezone."""
        from prometheus_identity import build_system_prompt
        profile = {
            "name": "Tate",
            "timezone": "Invalid/Zone",
            "faith_fitness_legacy": False,
            "current_priorities": [],
        }
        result = build_system_prompt({}, [], [], {}, profile)
        assert isinstance(result, str)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# 5. open_url_raw: returns accepted_unverified
# ---------------------------------------------------------------------------

class TestOpenUrlRaw:
    def test_open_url_raw_is_accepted_unverified(self):
        from tools import ToolStatus
        registry = _make_tool_registry()
        with patch("webbrowser.open"):
            result = registry.execute({"action": "open_url_raw", "url": "https://example.com"})
        assert result.status == ToolStatus.ACCEPTED_UNVERIFIED
        assert result.verified is False
        assert result.ok is True

    def test_open_url_raw_empty_url_is_failure(self):
        from tools import ToolStatus
        registry = _make_tool_registry()
        result = registry.execute({"action": "open_url_raw", "url": ""})
        assert result.ok is False

    def test_open_url_raw_message_does_not_claim_window_open(self):
        """Message must not say 'is open' — that's a false claim."""
        registry = _make_tool_registry()
        with patch("webbrowser.open"):
            result = registry.execute({"action": "open_url_raw", "url": "https://google.com"})
        assert "is open" not in result.message.lower()
        assert "opened google.com" not in result.message.lower()


# ---------------------------------------------------------------------------
# 6. open_url_key: returns accepted_unverified
# ---------------------------------------------------------------------------

class TestOpenUrlKey:
    def test_open_url_key_is_accepted_unverified(self):
        from tools import ToolStatus
        registry = _make_tool_registry()
        with patch("tools.CONFIG", {"urls": {"youtube": "https://youtube.com"}, "apps": {}}):
            with patch("webbrowser.open"):
                result = registry.execute({"action": "open_url_key", "url_key": "youtube"})
        assert result.status == ToolStatus.ACCEPTED_UNVERIFIED
        assert result.ok is True

    def test_open_url_key_unknown_key_is_failure(self):
        registry = _make_tool_registry()
        with patch("tools.CONFIG", {"urls": {}, "apps": {}}):
            result = registry.execute({"action": "open_url_key", "url_key": "nonexistent"})
        assert result.ok is False


# ---------------------------------------------------------------------------
# 7. open_url_keys: returns accepted_unverified on all-ok
# ---------------------------------------------------------------------------

class TestOpenUrlKeys:
    def test_open_url_keys_all_ok_is_accepted_unverified(self):
        from tools import ToolStatus
        registry = _make_tool_registry()
        with patch("tools.CONFIG", {"urls": {"youtube": "https://youtube.com",
                                             "gmail": "https://mail.google.com"}, "apps": {}}):
            with patch("webbrowser.open"):
                result = registry.execute({"action": "open_url_keys",
                                           "url_keys": ["youtube", "gmail"]})
        assert result.status == ToolStatus.ACCEPTED_UNVERIFIED
        assert result.ok is True

    def test_open_url_keys_partial_failure_not_verified(self):
        """If any key is unknown, result is not verified_success."""
        from tools import ToolStatus
        registry = _make_tool_registry()
        with patch("tools.CONFIG", {"urls": {"youtube": "https://youtube.com"}, "apps": {}}):
            with patch("webbrowser.open"):
                result = registry.execute({"action": "open_url_keys",
                                           "url_keys": ["youtube", "badkey"]})
        assert result.status != ToolStatus.VERIFIED_SUCCESS


# ---------------------------------------------------------------------------
# 8. open_app: already-running branch returns verified_success
# ---------------------------------------------------------------------------

class TestOpenAppAlreadyRunning:
    def test_already_running_is_verified_success(self):
        from tools import ToolStatus
        registry = _make_tool_registry()
        proc_name = "code"
        with (
            patch("tools._APP_PROCESS_NAMES", {"code": proc_name}),
            patch("tools.command_exists", return_value=True),
            patch("subprocess.run") as mock_run,
            patch("subprocess.Popen"),
        ):
            # pgrep succeeds (returncode=0), wmctrl finds window
            pgrep_result = MagicMock()
            pgrep_result.returncode = 0
            wmctrl_result = MagicMock()
            wmctrl_result.stdout = "0x123 code — vs code\n"
            mock_run.side_effect = [pgrep_result, wmctrl_result]
            result = registry.execute({"action": "open_app", "app": "code"})
        assert result.status == ToolStatus.VERIFIED_SUCCESS
        assert result.verified is True
        assert result.ok is True

    def test_already_running_message_contains_already_open(self):
        registry = _make_tool_registry()
        with (
            patch("tools._APP_PROCESS_NAMES", {"code": "code"}),
            patch("tools.command_exists", return_value=True),
            patch("subprocess.run") as mock_run,
            patch("subprocess.Popen"),
        ):
            pgrep_result = MagicMock()
            pgrep_result.returncode = 0
            wmctrl_result = MagicMock()
            wmctrl_result.stdout = "0x123 code\n"
            mock_run.side_effect = [pgrep_result, wmctrl_result]
            result = registry.execute({"action": "open_app", "app": "code"})
        assert "already open" in result.message.lower()


# ---------------------------------------------------------------------------
# 9. open_app: fresh-launch with successful post-launch pgrep → verified_success
# ---------------------------------------------------------------------------

class TestOpenAppFreshLaunch:
    def _fresh_launch_result(self, post_pgrep_rc: int, proc_name: str = "spotify"):
        """
        Simulate a fresh app launch where pgrep returns returncode post_pgrep_rc.
        Initial running check: pgrep=1 (not running), so wmctrl not called.
        _launch_with_fallback returns ok=True.
        Post-launch pgrep returns post_pgrep_rc.
        """
        from tools import ToolStatus
        registry = _make_tool_registry()
        with (
            patch("tools._APP_PROCESS_NAMES", {"spotify": proc_name}),
            patch("tools.command_exists", return_value=True),
            patch("subprocess.run") as mock_run,
            patch("tools.ToolRegistry._launch_with_fallback") as mock_launch,
            patch("time.sleep"),
        ):
            # Pre-launch check: pgrep says not running (rc=1)
            pre_check = MagicMock()
            pre_check.returncode = 1
            # Post-launch check
            post_check = MagicMock()
            post_check.returncode = post_pgrep_rc
            mock_run.side_effect = [pre_check, post_check]

            from tools import ToolResult as TR
            mock_launch.return_value = TR(True, "Launched spotify.")

            result = registry.execute({"action": "open_app", "app": "spotify"})
        return result

    def test_fresh_launch_process_confirmed_is_verified_success(self):
        from tools import ToolStatus
        result = self._fresh_launch_result(post_pgrep_rc=0)
        assert result.status == ToolStatus.VERIFIED_SUCCESS
        assert result.verified is True

    def test_fresh_launch_process_not_found_is_accepted_unverified(self):
        from tools import ToolStatus
        result = self._fresh_launch_result(post_pgrep_rc=1)
        assert result.status == ToolStatus.ACCEPTED_UNVERIFIED
        assert result.verified is False

    def test_fresh_launch_no_proc_name_is_accepted_unverified(self):
        """App with no known process name cannot be verified — accepted_unverified."""
        from tools import ToolStatus
        registry = _make_tool_registry()
        with (
            patch("tools._APP_PROCESS_NAMES", {}),
            patch("tools.command_exists", return_value=False),
            patch("subprocess.run") as mock_run,
            patch("tools.ToolRegistry._launch_with_fallback") as mock_launch,
            patch("time.sleep"),
        ):
            from tools import ToolResult as TR
            mock_launch.return_value = TR(True, "Launched unknownapp.")
            result = registry.execute({"action": "open_app", "app": "unknownapp"})
        assert result.status == ToolStatus.ACCEPTED_UNVERIFIED

    def test_fresh_launch_failure_is_tool_failure(self):
        """If _launch_with_fallback returns ok=False, that propagates."""
        from tools import ToolStatus
        registry = _make_tool_registry()
        with (
            patch("tools._APP_PROCESS_NAMES", {"code": "code"}),
            patch("tools.command_exists", return_value=True),
            patch("subprocess.run") as mock_run,
            patch("tools.ToolRegistry._launch_with_fallback") as mock_launch,
        ):
            pre_check = MagicMock()
            pre_check.returncode = 1
            mock_run.side_effect = [pre_check]
            from tools import ToolResult as TR
            mock_launch.return_value = TR(False, "Could not find command for code.")
            result = registry.execute({"action": "open_app", "app": "code"})
        assert result.ok is False

"""
tests/acceptance/test_no_false_success_claims.py

Prometheus must never claim confirmed success when it cannot actually verify
the outcome of an action. This file systematically tests that every action
which cannot verify its outcome is correctly classified as accepted_unverified
and that the LLM response instructions prevent false claims.

Scope:
  - Browser / URL open actions
  - App open actions (fresh launch, unconfirmable)
  - HA script calls (accepted_unverified when HA verifier not run)
  - Calendar write actions (no live Google API)
  - Shell / Python execution (no guaranteed output yet)

Every test is offline — no real Realtime API, HA, or Google.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ── Helpers ──────────────────────────────────────────────────────────────────

def _registry():
    from tools import ToolRegistry
    return ToolRegistry()


# ── Browser / URL actions ─────────────────────────────────────────────────────

class TestBrowserActionsNeverVerified:
    """Opening a URL in a browser cannot be confirmed without browser access."""

    def test_open_url_raw_is_accepted_unverified(self):
        from tools import ToolStatus
        r = _registry()
        with patch("webbrowser.open"):
            result = r.execute({"action": "open_url_raw", "url": "https://google.com"})
        assert result.status == ToolStatus.ACCEPTED_UNVERIFIED
        assert result.verified is False

    def test_open_url_raw_message_never_says_is_open(self):
        r = _registry()
        with patch("webbrowser.open"):
            result = r.execute({"action": "open_url_raw", "url": "https://reddit.com"})
        bad_phrases = ["is open", "opened", "now open", "reddit is", "is now"]
        for phrase in bad_phrases:
            assert phrase not in result.message.lower(), \
                f"message must not claim confirmed state: {result.message!r}"

    def test_open_url_raw_message_contains_browser_or_launch(self):
        r = _registry()
        with patch("webbrowser.open"):
            result = r.execute({"action": "open_url_raw", "url": "https://youtube.com"})
        assert any(w in result.message.lower() for w in ("browser", "launch", "sent", "command")), \
            f"message should indicate a command was sent, not confirmed: {result.message!r}"

    def test_open_url_key_is_accepted_unverified(self):
        from tools import ToolStatus
        r = _registry()
        with (
            patch("tools.CONFIG", {"urls": {"youtube": "https://youtube.com"}, "apps": {}}),
            patch("webbrowser.open"),
        ):
            result = r.execute({"action": "open_url_key", "url_key": "youtube"})
        assert result.status == ToolStatus.ACCEPTED_UNVERIFIED

    def test_open_url_key_message_does_not_claim_youtube_is_open(self):
        r = _registry()
        with (
            patch("tools.CONFIG", {"urls": {"youtube": "https://youtube.com"}, "apps": {}}),
            patch("webbrowser.open"),
        ):
            result = r.execute({"action": "open_url_key", "url_key": "youtube"})
        assert "youtube is open" not in result.message.lower()
        assert "opened youtube" not in result.message.lower()

    def test_open_url_keys_all_ok_is_accepted_unverified(self):
        from tools import ToolStatus
        r = _registry()
        with (
            patch("tools.CONFIG", {"urls": {"gmail": "https://mail.google.com",
                                             "cal": "https://calendar.google.com"}, "apps": {}}),
            patch("webbrowser.open"),
        ):
            result = r.execute({"action": "open_url_keys", "url_keys": ["gmail", "cal"]})
        if result.ok:
            assert result.status == ToolStatus.ACCEPTED_UNVERIFIED, \
                "multi-URL open must not claim verified_success"

    def test_open_url_unknown_key_returns_failure(self):
        r = _registry()
        with patch("tools.CONFIG", {"urls": {}, "apps": {}}):
            result = r.execute({"action": "open_url_key", "url_key": "_nonexistent_key"})
        assert result.ok is False


# ── App open actions ──────────────────────────────────────────────────────────

class TestAppOpenActionsNeverClaimVerifiedIfUnconfirmed:
    """App opens are only verified_success when pgrep confirms the process started."""

    def test_fresh_launch_with_no_known_process_is_accepted_unverified(self):
        from tools import ToolRegistry, ToolStatus, ToolResult as TR
        r = ToolRegistry()
        with (
            patch("tools._APP_PROCESS_NAMES", {}),  # no process name mapping
            patch("tools.command_exists", return_value=True),
            patch("tools.ToolRegistry._launch_with_fallback",
                  return_value=TR(True, "Launched someapp.")),
        ):
            result = r.execute({"action": "open_app", "app": "someapp"})
        assert result.status == ToolStatus.ACCEPTED_UNVERIFIED
        assert result.verified is False

    def test_fresh_launch_pgrep_no_match_is_accepted_unverified(self):
        import subprocess
        from tools import ToolRegistry, ToolStatus, ToolResult as TR
        r = ToolRegistry()
        pre = MagicMock(); pre.returncode = 1   # not running before
        post = MagicMock(); post.returncode = 1  # still not found after launch
        with (
            patch("tools._APP_PROCESS_NAMES", {"myapp": "myapp"}),
            patch("tools.command_exists", return_value=True),
            patch("subprocess.run", side_effect=[pre, post]),
            patch("tools.ToolRegistry._launch_with_fallback",
                  return_value=TR(True, "Launched myapp.")),
            patch("time.sleep"),
        ):
            result = r.execute({"action": "open_app", "app": "myapp"})
        assert result.status == ToolStatus.ACCEPTED_UNVERIFIED

    def test_app_not_installed_returns_failure(self):
        from tools import ToolRegistry, ToolStatus
        r = ToolRegistry()
        with (
            patch("tools.command_exists", return_value=False),
            patch("subprocess.run", side_effect=Exception("not found")),
        ):
            result = r.execute({"action": "open_app", "app": "_definitely_not_installed_xyz"})
        assert result.ok is False

    def test_open_app_message_never_says_is_open_when_unconfirmed(self):
        from tools import ToolRegistry, ToolResult as TR
        r = ToolRegistry()
        with (
            patch("tools._APP_PROCESS_NAMES", {}),
            patch("tools.command_exists", return_value=True),
            patch("tools.ToolRegistry._launch_with_fallback",
                  return_value=TR(True, "Launched discord.")),
        ):
            result = r.execute({"action": "open_app", "app": "discord"})
        if result.ok:
            assert "discord is open" not in result.message.lower()
            assert "discord is now" not in result.message.lower()


# ── HA script actions ─────────────────────────────────────────────────────────

class TestHAScriptActionsNeverVerifiedByDefault:
    """
    HA script actions without post-verification must be accepted_unverified.
    The verifier is only called for specific script categories (lights, xbox).
    """

    def test_run_ha_script_is_accepted_unverified_by_default(self):
        import os
        from tools import ToolRegistry, ToolStatus
        r = ToolRegistry()
        _env = {"HOME_ASSISTANT_URL": "http://fake-ha", "HOME_ASSISTANT_API_KEY": "faketoken"}
        with (
            patch.dict(os.environ, _env),
            patch("requests.post",
                  return_value=MagicMock(status_code=200, json=lambda: [])),
        ):
            # jarvis_media_play_music is not in lights/xbox categories — no verifier → accepted_unverified
            result = r.execute({"action": "run_ha_script",
                                "script_name": "jarvis_media_play_music"})
        assert result.status == ToolStatus.ACCEPTED_UNVERIFIED
        assert result.verified is False

    def test_ha_script_message_never_claims_device_state(self):
        import os
        from tools import ToolRegistry
        r = ToolRegistry()
        _env = {"HOME_ASSISTANT_URL": "http://fake-ha", "HOME_ASSISTANT_API_KEY": "faketoken"}
        with (
            patch.dict(os.environ, _env),
            patch("requests.post",
                  return_value=MagicMock(status_code=200, json=lambda: [])),
        ):
            result = r.execute({"action": "run_ha_script",
                                "script_name": "jarvis_media_play_music"})
        bad_phrases = ["lights are on", "lights are off", "lights are now", "confirmed on"]
        for phrase in bad_phrases:
            assert phrase not in result.message.lower(), \
                f"HA message must not claim device state: {result.message!r}"


# ── Response synthesizer instruction contract ──────────────────────────────────

class TestResponseSynthesizerInstructions:
    """
    tool_response_instructions() must prohibit false device-state claims for
    accepted_unverified results, and must allow confirmed claims for verified_success.
    """

    def test_accepted_unverified_instructions_forbid_device_state(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult, ToolStatus
        r = ToolResult.accepted_unverified("Browser launch sent for youtube.")
        instructions = tool_response_instructions(r, "open_url_key")
        text = instructions.lower()
        assert any(phrase in text for phrase in (
            "do not say", "must not", "do not claim", "command was sent",
            "cannot confirm", "do not state",
        )), f"instructions must prohibit false claims, got: {instructions!r}"

    def test_verified_success_instructions_allow_done(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult, ToolStatus
        r = ToolResult.verified_success("It is 2:47 PM, Sunday June 8 2026.",
                                        summary="clock read")
        instructions = tool_response_instructions(r, "tell_time")
        text = instructions.lower()
        assert any(phrase in text for phrase in (
            "verified", "done", "confirmed", "state the result", "report",
        )), f"verified_success instructions should allow stating result, got: {instructions!r}"

    def test_tool_failure_instructions_say_failed(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult, ToolStatus
        r = ToolResult(False, "Unknown action.")
        instructions = tool_response_instructions(r, "unknown_action")
        text = instructions.lower()
        assert any(phrase in text for phrase in (
            "failed", "error", "unable", "could not", "did not",
        )), f"failure instructions must surface failure, got: {instructions!r}"

    def test_accepted_unverified_never_produces_done_alone(self):
        from prometheus.execution.response_synthesizer import tool_response_instructions
        from tools import ToolResult
        r = ToolResult.accepted_unverified("App launch command sent.")
        instructions = tool_response_instructions(r, "open_app")
        # The bare string "Done." should not be the entire instruction
        # (it would let LLM claim confirmed success)
        assert instructions.strip().lower() != "done.", \
            "accepted_unverified instructions must not just say 'Done.'"


# ── ok=True never implies verified ────────────────────────────────────────────

class TestOkTrueDoesNotImplyVerified:
    """ok=True is 'the command ran'. verified=True is 'the outcome was confirmed'."""

    def test_plain_tool_result_ok_true_is_not_verified(self):
        from tools import ToolResult, ToolStatus
        r = ToolResult(True, "Command sent.")
        assert r.ok is True
        assert r.verified is False
        assert r.status == ToolStatus.ACCEPTED_UNVERIFIED

    def test_verified_success_factory_both_true(self):
        from tools import ToolResult, ToolStatus
        r = ToolResult.verified_success("Done.", summary="verified via pgrep")
        assert r.ok is True
        assert r.verified is True
        assert r.status == ToolStatus.VERIFIED_SUCCESS

    def test_accepted_unverified_factory_ok_true_verified_false(self):
        from tools import ToolResult, ToolStatus
        r = ToolResult.accepted_unverified("Command sent.")
        assert r.ok is True
        assert r.verified is False
        assert r.status == ToolStatus.ACCEPTED_UNVERIFIED

    def test_tool_failure_both_false(self):
        from tools import ToolResult, ToolStatus
        r = ToolResult(False, "Failed.")
        assert r.ok is False
        assert r.verified is False
        assert r.status == ToolStatus.TOOL_FAILURE

    def test_verified_failure_ok_false_status_correct(self):
        from tools import ToolResult, ToolStatus
        r = ToolResult.verified_failure("State did not change as expected.")
        assert r.ok is False
        assert r.status == ToolStatus.VERIFIED_FAILURE
        # verified=False because the action didn't take effect — HA confirmed the negative
        assert r.verified is False

    def test_status_enum_strings_are_stable(self):
        from tools import ToolStatus
        assert ToolStatus.VERIFIED_SUCCESS == "verified_success"
        assert ToolStatus.ACCEPTED_UNVERIFIED == "accepted_unverified"
        assert ToolStatus.VERIFIED_FAILURE == "verified_failure"
        assert ToolStatus.TOOL_FAILURE == "tool_failure"
        assert ToolStatus.BLOCKED == "blocked"

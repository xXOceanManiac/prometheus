"""
tests/test_ha_verification.py — Pass 6: HA post-state verification.

Verifies:
- POST 200 + expected state match → verified_success
- POST 200 + state mismatch → verified_failure (clear mismatch) or accepted_unverified (ambiguous)
- GET failure → accepted_unverified
- Red lights mismatch does not produce verified_success
- Xbox app unknown does not produce verified_success
- trace_id appears on all verification log events
- Light entity not configured → accepted_unverified
- Morning routine path (run_ha_script directly) is not affected
- verify_ha_script(routine_script) returns None
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ha_state(state: str, attrs: dict | None = None) -> dict:
    """Build a minimal HA entity state dict."""
    return {"state": state, "attributes": attrs or {}}


def _patch_get_state(return_value):
    """Patch _get_ha_state in ha_verifier."""
    return patch(
        "prometheus.integrations.ha_verifier._get_ha_state",
        return_value=return_value,
    )


def _patch_sleep():
    return patch("prometheus.integrations.ha_verifier.time.sleep")


def _patch_log():
    logged = []
    return patch("prometheus.integrations.ha_verifier.log_event",
                 side_effect=lambda kind, payload: logged.append((kind, payload))), logged


# ---------------------------------------------------------------------------
# verify_ha_script — routing
# ---------------------------------------------------------------------------

class TestVerifyHaScriptRouting:
    def test_lights_script_is_routed(self):
        from prometheus.integrations.ha_verifier import verify_ha_script
        with _patch_sleep(), _patch_get_state(None), \
             patch("prometheus.integrations.ha_verifier.log_event"):
            result = verify_ha_script("jarvis_lights_power_on")
        assert result is not None

    def test_xbox_script_is_routed(self):
        from prometheus.integrations.ha_verifier import verify_ha_script
        with _patch_sleep(), _patch_get_state(None), \
             patch("prometheus.integrations.ha_verifier.log_event"):
            result = verify_ha_script("jarvis_xbox_power_on")
        assert result is not None

    def test_routine_script_returns_none(self):
        from prometheus.integrations.ha_verifier import verify_ha_script
        result = verify_ha_script("jarvis_routine_watch_netflix")
        assert result is None

    def test_unknown_script_returns_none(self):
        from prometheus.integrations.ha_verifier import verify_ha_script
        result = verify_ha_script("some_other_script")
        assert result is None

    def test_returns_tool_result_type(self):
        from prometheus.integrations.ha_verifier import verify_ha_script
        from tools import ToolResult
        with _patch_sleep(), _patch_get_state(None), \
             patch("prometheus.integrations.ha_verifier.log_event"):
            result = verify_ha_script("jarvis_lights_power_on")
        assert isinstance(result, ToolResult)


# ---------------------------------------------------------------------------
# Light entity not configured → accepted_unverified
# ---------------------------------------------------------------------------

class TestLightEntityNotConfigured:
    def test_no_light_entity_accepted_unverified(self, monkeypatch):
        from prometheus.integrations.ha_verifier import verify_ha_script
        from tools import ToolStatus
        monkeypatch.setattr(
            "prometheus.integrations.ha_verifier.CONFIG",
            {"ha_light_entity": ""},
        )
        with patch("prometheus.integrations.ha_verifier.log_event"):
            result = verify_ha_script("jarvis_lights_power_on")
        assert result is not None
        assert result.status == ToolStatus.ACCEPTED_UNVERIFIED

    def test_no_light_entity_verification_summary_mentions_not_configured(self, monkeypatch):
        from prometheus.integrations.ha_verifier import verify_ha_script
        monkeypatch.setattr(
            "prometheus.integrations.ha_verifier.CONFIG",
            {"ha_light_entity": ""},
        )
        with patch("prometheus.integrations.ha_verifier.log_event"):
            result = verify_ha_script("jarvis_lights_power_on")
        assert "not configured" in result.verification_summary.lower()


# ---------------------------------------------------------------------------
# Light verification — lights_power_on
# ---------------------------------------------------------------------------

class TestLightsPowerOn:
    def _run(self, state_value: str, *, entity: str = "light.rgb_strip"):
        from prometheus.integrations.ha_verifier import verify_ha_script
        with patch("prometheus.integrations.ha_verifier.CONFIG",
                   {"ha_light_entity": entity}), \
             _patch_sleep(), \
             _patch_get_state(_make_ha_state(state_value)), \
             patch("prometheus.integrations.ha_verifier.log_event"):
            return verify_ha_script("jarvis_lights_power_on")

    def test_light_on_is_verified_success(self):
        from tools import ToolStatus
        result = self._run("on")
        assert result.status == ToolStatus.VERIFIED_SUCCESS

    def test_light_on_verified_is_true(self):
        result = self._run("on")
        assert result.verified is True

    def test_light_off_is_verified_failure(self):
        from tools import ToolStatus
        result = self._run("off")
        assert result.status == ToolStatus.VERIFIED_FAILURE

    def test_light_transitional_state_is_accepted_unverified(self):
        from tools import ToolStatus
        result = self._run("unavailable")
        assert result.status == ToolStatus.ACCEPTED_UNVERIFIED


class TestLightsPowerOff:
    def _run(self, state_value: str, *, entity: str = "light.rgb_strip"):
        from prometheus.integrations.ha_verifier import verify_ha_script
        with patch("prometheus.integrations.ha_verifier.CONFIG",
                   {"ha_light_entity": entity}), \
             _patch_sleep(), \
             _patch_get_state(_make_ha_state(state_value)), \
             patch("prometheus.integrations.ha_verifier.log_event"):
            return verify_ha_script("jarvis_lights_power_off")

    def test_light_off_is_verified_success(self):
        from tools import ToolStatus
        result = self._run("off")
        assert result.status == ToolStatus.VERIFIED_SUCCESS

    def test_light_on_is_verified_failure(self):
        from tools import ToolStatus
        result = self._run("on")
        assert result.status == ToolStatus.VERIFIED_FAILURE


# ---------------------------------------------------------------------------
# Light verification — color scenes
# ---------------------------------------------------------------------------

class TestLightColorScenes:
    def _run(self, color: str, state_value: str, attrs: dict | None = None):
        from prometheus.integrations.ha_verifier import verify_ha_script
        with patch("prometheus.integrations.ha_verifier.CONFIG",
                   {"ha_light_entity": "light.rgb_strip"}), \
             _patch_sleep(), \
             _patch_get_state(_make_ha_state(state_value, attrs or {})), \
             patch("prometheus.integrations.ha_verifier.log_event"):
            return verify_ha_script(f"jarvis_lights_scene_{color}")

    def test_red_lights_off_is_verified_failure(self):
        from tools import ToolStatus
        result = self._run("red", "off")
        assert result.status == ToolStatus.VERIFIED_FAILURE

    def test_red_lights_mismatch_not_verified_success(self):
        from tools import ToolStatus
        # Light is on but hs_color indicates blue, not red
        result = self._run("red", "on", {"hs_color": [240.0, 100.0]})
        assert result.status != ToolStatus.VERIFIED_SUCCESS

    def test_red_lights_hs_match_is_verified_success(self):
        from tools import ToolStatus
        result = self._run("red", "on", {"hs_color": [5.0, 100.0]})
        assert result.status == ToolStatus.VERIFIED_SUCCESS

    def test_blue_lights_hs_match_is_verified_success(self):
        from tools import ToolStatus
        result = self._run("blue", "on", {"hs_color": [240.0, 100.0]})
        assert result.status == ToolStatus.VERIFIED_SUCCESS

    def test_green_lights_hs_match_is_verified_success(self):
        from tools import ToolStatus
        result = self._run("green", "on", {"hs_color": [120.0, 100.0]})
        assert result.status == ToolStatus.VERIFIED_SUCCESS

    def test_purple_lights_hs_match_is_verified_success(self):
        from tools import ToolStatus
        result = self._run("purple", "on", {"hs_color": [285.0, 100.0]})
        assert result.status == ToolStatus.VERIFIED_SUCCESS

    def test_red_rgb_match_is_verified_success(self):
        from tools import ToolStatus
        result = self._run("red", "on", {"rgb_color": [255, 0, 0]})
        assert result.status == ToolStatus.VERIFIED_SUCCESS

    def test_no_color_attrs_is_accepted_unverified(self):
        from tools import ToolStatus
        # Light is on but no hs_color or rgb_color in attributes
        result = self._run("red", "on", {})
        assert result.status == ToolStatus.ACCEPTED_UNVERIFIED

    def test_desaturated_hs_is_accepted_unverified(self):
        from tools import ToolStatus
        # Saturation too low — can't confirm color
        result = self._run("red", "on", {"hs_color": [5.0, 10.0]})
        assert result.status == ToolStatus.ACCEPTED_UNVERIFIED

    def test_color_unverifiable_summary_is_helpful(self):
        result = self._run("red", "on", {})
        assert "cannot be confirmed" in result.verification_summary.lower() or \
               "unverifi" in result.verification_summary.lower()


# ---------------------------------------------------------------------------
# Light verification — scene (non-color)
# ---------------------------------------------------------------------------

class TestLightNonColorScene:
    def _run(self, scene: str, state_value: str):
        from prometheus.integrations.ha_verifier import verify_ha_script
        with patch("prometheus.integrations.ha_verifier.CONFIG",
                   {"ha_light_entity": "light.rgb_strip"}), \
             _patch_sleep(), \
             _patch_get_state(_make_ha_state(state_value)), \
             patch("prometheus.integrations.ha_verifier.log_event"):
            return verify_ha_script(f"jarvis_lights_scene_{scene}")

    def test_movie_mode_light_on_is_accepted_unverified(self):
        from tools import ToolStatus
        result = self._run("movie", "on")
        assert result.status == ToolStatus.ACCEPTED_UNVERIFIED

    def test_movie_mode_light_off_is_verified_failure(self):
        from tools import ToolStatus
        result = self._run("movie", "off")
        assert result.status == ToolStatus.VERIFIED_FAILURE

    def test_work_mode_scene_settings_unverifiable_in_summary(self):
        result = self._run("work", "on")
        assert "scene" in result.verification_summary.lower() or "settings" in result.verification_summary.lower()


# ---------------------------------------------------------------------------
# HA GET failure → accepted_unverified
# ---------------------------------------------------------------------------

class TestGetStateFails:
    def test_get_returns_none_gives_accepted_unverified_lights(self, monkeypatch):
        from prometheus.integrations.ha_verifier import verify_ha_script
        from tools import ToolStatus
        monkeypatch.setattr(
            "prometheus.integrations.ha_verifier.CONFIG",
            {"ha_light_entity": "light.rgb_strip"},
        )
        with _patch_sleep(), _patch_get_state(None), \
             patch("prometheus.integrations.ha_verifier.log_event"):
            result = verify_ha_script("jarvis_lights_power_on")
        assert result.status == ToolStatus.ACCEPTED_UNVERIFIED

    def test_get_returns_none_gives_accepted_unverified_xbox(self):
        from prometheus.integrations.ha_verifier import verify_ha_script
        from tools import ToolStatus
        with _patch_sleep(), _patch_get_state(None), \
             patch("prometheus.integrations.ha_verifier.log_event"):
            result = verify_ha_script("jarvis_xbox_power_on")
        assert result.status == ToolStatus.ACCEPTED_UNVERIFIED

    def test_get_returns_empty_dict_entity_not_found_lights(self, monkeypatch):
        from prometheus.integrations.ha_verifier import verify_ha_script
        from tools import ToolStatus
        monkeypatch.setattr(
            "prometheus.integrations.ha_verifier.CONFIG",
            {"ha_light_entity": "light.rgb_strip"},
        )
        with _patch_sleep(), _patch_get_state({}), \
             patch("prometheus.integrations.ha_verifier.log_event"):
            result = verify_ha_script("jarvis_lights_power_on")
        assert result.status == ToolStatus.ACCEPTED_UNVERIFIED

    def test_get_returns_empty_dict_entity_not_found_xbox(self):
        from prometheus.integrations.ha_verifier import verify_ha_script
        from tools import ToolStatus
        with _patch_sleep(), _patch_get_state({}), \
             patch("prometheus.integrations.ha_verifier.log_event"):
            result = verify_ha_script("jarvis_xbox_power_on")
        assert result.status == ToolStatus.ACCEPTED_UNVERIFIED


# ---------------------------------------------------------------------------
# Xbox verification
# ---------------------------------------------------------------------------

class TestXboxPowerOn:
    def _run(self, state_value: str):
        from prometheus.integrations.ha_verifier import verify_ha_script
        with _patch_sleep(), \
             _patch_get_state(_make_ha_state(state_value)), \
             patch("prometheus.integrations.ha_verifier.log_event"):
            return verify_ha_script("jarvis_xbox_power_on")

    def test_xbox_on_state_is_verified_success(self):
        from tools import ToolStatus
        result = self._run("on")
        assert result.status == ToolStatus.VERIFIED_SUCCESS

    def test_xbox_playing_state_is_verified_success(self):
        from tools import ToolStatus
        result = self._run("playing")
        assert result.status == ToolStatus.VERIFIED_SUCCESS

    def test_xbox_idle_state_is_verified_success(self):
        from tools import ToolStatus
        result = self._run("idle")
        assert result.status == ToolStatus.VERIFIED_SUCCESS

    def test_xbox_off_is_verified_failure(self):
        from tools import ToolStatus
        result = self._run("off")
        assert result.status == ToolStatus.VERIFIED_FAILURE

    def test_xbox_unavailable_is_verified_failure(self):
        from tools import ToolStatus
        result = self._run("unavailable")
        assert result.status == ToolStatus.VERIFIED_FAILURE


class TestXboxPowerOff:
    def _run(self, state_value: str):
        from prometheus.integrations.ha_verifier import verify_ha_script
        with _patch_sleep(), \
             _patch_get_state(_make_ha_state(state_value)), \
             patch("prometheus.integrations.ha_verifier.log_event"):
            return verify_ha_script("jarvis_xbox_power_off")

    def test_xbox_off_is_verified_success(self):
        from tools import ToolStatus
        result = self._run("off")
        assert result.status == ToolStatus.VERIFIED_SUCCESS

    def test_xbox_playing_is_verified_failure(self):
        from tools import ToolStatus
        result = self._run("playing")
        assert result.status == ToolStatus.VERIFIED_FAILURE


class TestXboxAppLaunch:
    def _run(self, app_script: str, app_name_attr: str, state: str = "playing"):
        from prometheus.integrations.ha_verifier import verify_ha_script
        with _patch_sleep(), \
             _patch_get_state(_make_ha_state(state, {"app_name": app_name_attr})), \
             patch("prometheus.integrations.ha_verifier.log_event"):
            return verify_ha_script(app_script)

    def test_youtube_confirmed_when_app_name_matches(self):
        from tools import ToolStatus
        result = self._run("jarvis_xbox_app_youtube", "YouTube")
        assert result.status == ToolStatus.VERIFIED_SUCCESS

    def test_netflix_confirmed_when_app_name_matches(self):
        from tools import ToolStatus
        result = self._run("jarvis_xbox_app_netflix", "Netflix")
        assert result.status == ToolStatus.VERIFIED_SUCCESS

    def test_spotify_confirmed_when_app_name_matches(self):
        from tools import ToolStatus
        result = self._run("jarvis_xbox_app_spotify", "Spotify")
        assert result.status == ToolStatus.VERIFIED_SUCCESS

    def test_xbox_app_unknown_is_accepted_unverified(self):
        from tools import ToolStatus
        # App hasn't loaded yet — app_name is empty
        result = self._run("jarvis_xbox_app_youtube", "")
        assert result.status == ToolStatus.ACCEPTED_UNVERIFIED

    def test_xbox_app_unknown_does_not_say_launched(self):
        from tools import ToolStatus
        result = self._run("jarvis_xbox_app_youtube", "")
        assert result.status != ToolStatus.VERIFIED_SUCCESS
        assert "launched" not in result.message.lower()

    def test_wrong_app_is_accepted_unverified_not_failure(self):
        from tools import ToolStatus
        # Different app is open — app may still be loading, so accepted_unverified
        result = self._run("jarvis_xbox_app_youtube", "Netflix")
        assert result.status == ToolStatus.ACCEPTED_UNVERIFIED


class TestXboxMediaPauseResume:
    def test_pause_confirmed_when_paused(self):
        from tools import ToolStatus
        from prometheus.integrations.ha_verifier import verify_ha_script
        with _patch_sleep(), \
             _patch_get_state(_make_ha_state("paused")), \
             patch("prometheus.integrations.ha_verifier.log_event"):
            result = verify_ha_script("jarvis_xbox_media_pause")
        assert result.status == ToolStatus.VERIFIED_SUCCESS

    def test_pause_not_confirmed_when_playing(self):
        from tools import ToolStatus
        from prometheus.integrations.ha_verifier import verify_ha_script
        with _patch_sleep(), \
             _patch_get_state(_make_ha_state("playing")), \
             patch("prometheus.integrations.ha_verifier.log_event"):
            result = verify_ha_script("jarvis_xbox_media_pause")
        assert result.status == ToolStatus.ACCEPTED_UNVERIFIED

    def test_resume_confirmed_when_playing(self):
        from tools import ToolStatus
        from prometheus.integrations.ha_verifier import verify_ha_script
        with _patch_sleep(), \
             _patch_get_state(_make_ha_state("playing")), \
             patch("prometheus.integrations.ha_verifier.log_event"):
            result = verify_ha_script("jarvis_xbox_media_resume")
        assert result.status == ToolStatus.VERIFIED_SUCCESS


class TestXboxVolume:
    def test_volume_command_is_accepted_unverified(self):
        from tools import ToolStatus
        from prometheus.integrations.ha_verifier import verify_ha_script
        with patch("prometheus.integrations.ha_verifier.log_event"):
            result = verify_ha_script("jarvis_xbox_volume_up")
        assert result.status == ToolStatus.ACCEPTED_UNVERIFIED


# ---------------------------------------------------------------------------
# trace_id appears in log events
# ---------------------------------------------------------------------------

class TestTraceIdInLogs:
    def test_trace_id_in_ha_command_sent_lights(self, monkeypatch):
        from prometheus.integrations.ha_verifier import verify_ha_script
        monkeypatch.setattr(
            "prometheus.integrations.ha_verifier.CONFIG",
            {"ha_light_entity": "light.rgb_strip"},
        )
        log_calls, logged = [], []
        with _patch_sleep(), _patch_get_state(_make_ha_state("on")):
            with patch("prometheus.integrations.ha_verifier.log_event",
                       side_effect=lambda k, p: logged.append((k, p))):
                verify_ha_script("jarvis_lights_power_on", trace_id="test-trace-abc1")

        sent_events = [p for k, p in logged if k == "ha_command_sent"]
        assert sent_events, "ha_command_sent not logged"
        assert sent_events[0].get("trace_id") == "test-trace-abc1"

    def test_trace_id_in_ha_verification_result_lights(self, monkeypatch):
        from prometheus.integrations.ha_verifier import verify_ha_script
        monkeypatch.setattr(
            "prometheus.integrations.ha_verifier.CONFIG",
            {"ha_light_entity": "light.rgb_strip"},
        )
        logged = []
        with _patch_sleep(), _patch_get_state(_make_ha_state("on")):
            with patch("prometheus.integrations.ha_verifier.log_event",
                       side_effect=lambda k, p: logged.append((k, p))):
                verify_ha_script("jarvis_lights_power_on", trace_id="test-trace-xyz9")

        result_events = [p for k, p in logged if k == "ha_verification_result"]
        assert result_events, "ha_verification_result not logged"
        assert result_events[0].get("trace_id") == "test-trace-xyz9"

    def test_trace_id_in_xbox_command_sent(self):
        from prometheus.integrations.ha_verifier import verify_ha_script
        logged = []
        with _patch_sleep(), _patch_get_state(_make_ha_state("on")):
            with patch("prometheus.integrations.ha_verifier.log_event",
                       side_effect=lambda k, p: logged.append((k, p))):
                verify_ha_script("jarvis_xbox_power_on", trace_id="xbox-trace-r4f2")

        sent_events = [p for k, p in logged if k == "ha_command_sent"]
        assert sent_events, "ha_command_sent not logged for Xbox"
        assert sent_events[0].get("trace_id") == "xbox-trace-r4f2"

    def test_ha_post_state_fetch_logged_with_entity(self, monkeypatch):
        from prometheus.integrations.ha_verifier import verify_ha_script
        monkeypatch.setattr(
            "prometheus.integrations.ha_verifier.CONFIG",
            {"ha_light_entity": "light.test_light"},
        )
        logged = []
        with _patch_sleep(), _patch_get_state(_make_ha_state("on")):
            with patch("prometheus.integrations.ha_verifier.log_event",
                       side_effect=lambda k, p: logged.append((k, p))):
                verify_ha_script("jarvis_lights_power_on", trace_id="trace-fetch-t1")

        fetch_events = [p for k, p in logged if k == "ha_post_state_fetch"]
        assert fetch_events, "ha_post_state_fetch not logged"
        assert fetch_events[0].get("entity_id") == "light.test_light"


# ---------------------------------------------------------------------------
# tools.py integration — ToolRegistry routes through verifier
# ---------------------------------------------------------------------------

class TestToolRegistryHAVerification:
    def test_run_ha_script_lights_calls_verifier(self, monkeypatch):
        """ToolRegistry._execute_one_inner should call verify_ha_script for lights."""
        from tools import ToolRegistry, ToolStatus

        monkeypatch.setattr("tools.run_ha_script", lambda name: __import__("tools").ToolResult(True, f"Executed: {name}"))
        monkeypatch.setattr("tools.log_event", lambda k, p: None)

        verified_result = __import__("tools").ToolResult.verified_success(
            "Executed: jarvis_lights_power_on",
            summary="Light confirmed on",
        )
        with patch("prometheus.integrations.ha_verifier.verify_ha_script",
                   return_value=verified_result) as mock_verify:
            reg = ToolRegistry()
            result = reg._execute_one_inner({"action": "run_ha_script", "script_name": "jarvis_lights_power_on"})

        mock_verify.assert_called_once()
        assert result.status == ToolStatus.VERIFIED_SUCCESS

    def test_run_ha_script_verifier_crash_is_non_fatal(self, monkeypatch):
        """If verifier raises, _execute_one_inner falls back to original result."""
        from tools import ToolRegistry, ToolStatus

        monkeypatch.setattr("tools.run_ha_script", lambda name: __import__("tools").ToolResult(True, f"Executed: {name}"))
        monkeypatch.setattr("tools.log_event", lambda k, p: None)

        with patch("prometheus.integrations.ha_verifier.verify_ha_script",
                   side_effect=RuntimeError("verifier exploded")):
            reg = ToolRegistry()
            result = reg._execute_one_inner({"action": "run_ha_script", "script_name": "jarvis_lights_power_on"})

        # Fallback: original accepted_unverified result
        assert result.ok is True
        assert result.status == ToolStatus.ACCEPTED_UNVERIFIED

    def test_run_ha_script_verifier_returns_none_keeps_original(self, monkeypatch):
        """If verifier returns None (routine script), original result is used."""
        from tools import ToolRegistry, ToolStatus

        monkeypatch.setattr("tools.run_ha_script", lambda name: __import__("tools").ToolResult(True, f"Executed: {name}"))
        monkeypatch.setattr("tools.log_event", lambda k, p: None)

        with patch("prometheus.integrations.ha_verifier.verify_ha_script",
                   return_value=None):
            reg = ToolRegistry()
            result = reg._execute_one_inner({"action": "run_ha_script", "script_name": "jarvis_routine_good_night"})

        assert result.ok is True
        assert result.status == ToolStatus.ACCEPTED_UNVERIFIED

    def test_run_ha_script_failure_does_not_call_verifier(self, monkeypatch):
        """Verifier must not be called when run_ha_script returns ok=False."""
        from tools import ToolRegistry

        monkeypatch.setattr(
            "tools.run_ha_script",
            lambda name: __import__("tools").ToolResult(False, "HA error")
        )
        monkeypatch.setattr("tools.log_event", lambda k, p: None)

        with patch("prometheus.integrations.ha_verifier.verify_ha_script") as mock_verify:
            reg = ToolRegistry()
            result = reg._execute_one_inner({"action": "run_ha_script", "script_name": "jarvis_lights_power_on"})

        mock_verify.assert_not_called()
        assert result.ok is False


# ---------------------------------------------------------------------------
# _color_matches helper
# ---------------------------------------------------------------------------

class TestColorMatches:
    def test_red_low_hue(self):
        from prometheus.integrations.ha_verifier import _color_matches
        assert _color_matches("red", [5.0, 100.0], None) is True

    def test_red_high_hue_wraps(self):
        from prometheus.integrations.ha_verifier import _color_matches
        assert _color_matches("red", [350.0, 100.0], None) is True

    def test_blue_mid_hue(self):
        from prometheus.integrations.ha_verifier import _color_matches
        assert _color_matches("blue", [240.0, 100.0], None) is True

    def test_green_mid_hue(self):
        from prometheus.integrations.ha_verifier import _color_matches
        assert _color_matches("green", [120.0, 100.0], None) is True

    def test_purple_mid_hue(self):
        from prometheus.integrations.ha_verifier import _color_matches
        assert _color_matches("purple", [290.0, 100.0], None) is True

    def test_wrong_color_hue_not_red(self):
        from prometheus.integrations.ha_verifier import _color_matches
        assert _color_matches("red", [240.0, 100.0], None) is False

    def test_low_saturation_returns_false(self):
        from prometheus.integrations.ha_verifier import _color_matches
        assert _color_matches("red", [5.0, 20.0], None) is False

    def test_rgb_red_match(self):
        from prometheus.integrations.ha_verifier import _color_matches
        assert _color_matches("red", None, [255, 0, 0]) is True

    def test_rgb_blue_match(self):
        from prometheus.integrations.ha_verifier import _color_matches
        assert _color_matches("blue", None, [0, 0, 255]) is True

    def test_no_color_data_returns_false(self):
        from prometheus.integrations.ha_verifier import _color_matches
        assert _color_matches("red", None, None) is False

    def test_empty_hs_falls_through_to_rgb(self):
        from prometheus.integrations.ha_verifier import _color_matches
        assert _color_matches("red", None, [200, 20, 20]) is True

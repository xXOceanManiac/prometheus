"""
tests/test_proactive_speech_policy.py

Unit tests for prometheus.policies.proactive_speech_policy.

All presence signals are mocked — no subprocess calls, no loginctl, no dbus-send.
Tests verify the policy logic, not the detection plumbing.
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ── Helpers ───────────────────────────────────────────────────────────────────

@contextmanager
def _presence(
    locked: bool | None,
    idle_min: float | None,
    source: str = "mock",
):
    """Patch detect_presence() to return a controlled PresenceState."""
    from prometheus.policies.proactive_speech_policy import PresenceState

    state = PresenceState(
        screen_locked=locked,
        idle_minutes=idle_min,
        presence_source=source,
    )
    with patch(
        "prometheus.policies.proactive_speech_policy.detect_presence",
        return_value=state,
    ):
        yield


@contextmanager
def _env(**kwargs: str):
    """Temporarily override env vars and clear them after."""
    with patch.dict(os.environ, kwargs):
        yield


# ── Tests: always-allow reasons ───────────────────────────────────────────────

class TestAlwaysAllowReasons:

    def test_morning_routine_bypasses_locked_idle(self):
        from prometheus.policies.proactive_speech_policy import should_allow_proactive_speech

        with _presence(locked=True, idle_min=60.0):
            assert should_allow_proactive_speech("morning_routine") is True

    def test_user_ptt_bypasses_locked_idle(self):
        from prometheus.policies.proactive_speech_policy import should_allow_proactive_speech

        with _presence(locked=True, idle_min=60.0):
            assert should_allow_proactive_speech("user_ptt") is True

    def test_explicit_reminder_bypasses_locked_idle(self):
        from prometheus.policies.proactive_speech_policy import should_allow_proactive_speech

        with _presence(locked=True, idle_min=60.0):
            assert should_allow_proactive_speech("explicit_reminder") is True

    def test_always_allow_reasons_do_not_call_detect_presence(self):
        """Always-allow reasons must short-circuit before any subprocess call."""
        from prometheus.policies.proactive_speech_policy import should_allow_proactive_speech

        with patch(
            "prometheus.policies.proactive_speech_policy.detect_presence"
        ) as mock_detect:
            should_allow_proactive_speech("morning_routine")
            mock_detect.assert_not_called()


# ── Tests: active session → allow ────────────────────────────────────────────

class TestActiveSessionAllows:

    def test_active_unlocked_session_allows_wrapup(self):
        """Unlocked + low idle → allow wrap-up reminder regardless of time of day."""
        from prometheus.policies.proactive_speech_policy import should_allow_proactive_speech

        with _presence(locked=False, idle_min=5.0):
            assert should_allow_proactive_speech("wrap_up") is True

    def test_active_late_night_allows_bedtime_reminder(self):
        """No clock-based quiet hours — active late-night session must allow bedtime reminder."""
        from prometheus.policies.proactive_speech_policy import should_allow_proactive_speech

        with _presence(locked=False, idle_min=2.0):
            # 2 minutes idle — clearly active
            result = should_allow_proactive_speech("bedtime")
        assert result is True, (
            "Active late-night session should allow bedtime/productivity check-ins. "
            "Suppression must never be based on clock time."
        )

    def test_active_unlocked_allows_proactive_general(self):
        from prometheus.policies.proactive_speech_policy import should_allow_proactive_speech

        with _presence(locked=False, idle_min=8.0):
            assert should_allow_proactive_speech("proactive_general") is True


# ── Tests: locked + idle → suppress ─────────────────────────────────────────

class TestLockedIdleSuppresses:

    def test_locked_and_high_idle_suppresses_wrapup(self):
        """Locked + idle ≥ threshold (default 20 min) → suppress."""
        from prometheus.policies.proactive_speech_policy import should_allow_proactive_speech

        with _presence(locked=True, idle_min=25.0):
            assert should_allow_proactive_speech("proactive_evening_wrapup") is False

    def test_locked_no_recent_activity_suppresses_bedtime(self):
        """Locked + idle ≥ recent threshold (default 15 min) → suppress."""
        from prometheus.policies.proactive_speech_policy import should_allow_proactive_speech

        with _presence(locked=True, idle_min=16.0):
            assert should_allow_proactive_speech("bedtime") is False

    def test_locked_unknown_idle_suppresses(self):
        """Locked + idle_minutes=None (can't read idle) → suppress (no activity evidence)."""
        from prometheus.policies.proactive_speech_policy import should_allow_proactive_speech

        with _presence(locked=True, idle_min=None):
            assert should_allow_proactive_speech("proactive_general") is False

    def test_locked_below_recent_threshold_allows(self):
        """Locked + idle < recent threshold → recent activity still plausible → allow."""
        from prometheus.policies.proactive_speech_policy import should_allow_proactive_speech

        with _presence(locked=True, idle_min=5.0):
            # 5 min idle < 15 min recent threshold — user may have just stepped away
            assert should_allow_proactive_speech("proactive_general") is True


# ── Tests: high idle without lock signal → suppress ──────────────────────────

class TestHighIdleWithoutLockSuppresses:

    def test_high_idle_unknown_lock_suppresses(self):
        """idle ≥ threshold with lock=None → suppress on idle alone."""
        from prometheus.policies.proactive_speech_policy import should_allow_proactive_speech

        with _presence(locked=None, idle_min=25.0):
            assert should_allow_proactive_speech("proactive_background_task") is False

    def test_moderate_idle_unknown_lock_allows(self):
        """idle < threshold with lock=None → allow (possibly just stepped away briefly)."""
        from prometheus.policies.proactive_speech_policy import should_allow_proactive_speech

        with _presence(locked=None, idle_min=10.0):
            assert should_allow_proactive_speech("proactive_general") is True


# ── Tests: all detectors fail ─────────────────────────────────────────────────

class TestMissingDetectors:

    def test_all_detectors_fail_does_not_crash(self):
        """If all subprocess calls fail, should_allow_proactive_speech must not raise."""
        from prometheus.policies.proactive_speech_policy import should_allow_proactive_speech

        with _presence(locked=None, idle_min=None, source="unknown"):
            result = should_allow_proactive_speech("proactive_general")
        # Unknown presence → allow (do not suppress due to missing data)
        assert result is True

    def test_missing_detectors_do_not_suppress_morning_routine(self):
        """morning_routine always allowed — even without any detectors."""
        from prometheus.policies.proactive_speech_policy import should_allow_proactive_speech

        with _presence(locked=None, idle_min=None):
            assert should_allow_proactive_speech("morning_routine") is True


# ── Tests: suppression killswitch ────────────────────────────────────────────

class TestSuppressionConfig:

    def test_suppression_disabled_by_env_var(self):
        """PROMETHEUS_SUPPRESS_PROACTIVE_WHEN_LOCKED_AND_IDLE=false disables all suppression."""
        from prometheus.policies.proactive_speech_policy import should_allow_proactive_speech

        with _presence(locked=True, idle_min=120.0), \
             _env(PROMETHEUS_SUPPRESS_PROACTIVE_WHEN_LOCKED_AND_IDLE="false"):
            assert should_allow_proactive_speech("proactive_general") is True

    def test_custom_idle_threshold_respected(self):
        """PROMETHEUS_PROACTIVE_IDLE_THRESHOLD_MINUTES=60 raises the bar."""
        from prometheus.policies.proactive_speech_policy import should_allow_proactive_speech

        with _presence(locked=False, idle_min=30.0), \
             _env(PROMETHEUS_PROACTIVE_IDLE_THRESHOLD_MINUTES="60"):
            # 30 min < 60 min threshold → allow
            assert should_allow_proactive_speech("proactive_general") is True

    def test_suppress_when_locked_false_ignores_lock(self):
        """PROMETHEUS_PROACTIVE_SUPPRESS_WHEN_LOCKED=false — lock alone never suppresses."""
        from prometheus.policies.proactive_speech_policy import should_allow_proactive_speech

        with _presence(locked=True, idle_min=None), \
             _env(PROMETHEUS_PROACTIVE_SUPPRESS_WHEN_LOCKED="false"):
            # No idle data, no suppress_when_locked flag — idle also unknown → allow
            assert should_allow_proactive_speech("proactive_general") is True


# ── Tests: detect_presence never raises ──────────────────────────────────────

class TestDetectPresenceNeverRaises:

    def test_all_subprocess_calls_fail(self):
        """detect_presence() must return a PresenceState even when every subprocess fails."""
        from prometheus.policies.proactive_speech_policy import detect_presence

        with patch(
            "prometheus.policies.proactive_speech_policy._run",
            return_value=(127, ""),
        ):
            state = detect_presence()

        # Should not raise; all fields unknown
        assert state.screen_locked is None
        assert state.idle_minutes is None
        assert state.presence_source == "unknown"

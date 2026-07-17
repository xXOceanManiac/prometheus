"""
prometheus/policies/proactive_speech_policy.py

Centralized gate for proactive speech.

should_allow_proactive_speech(reason, context=None) -> bool

Returns True when Prometheus is permitted to speak proactively.
Returns False (and logs proactive_speech_suppressed) when the screen is locked
and/or the user has been idle long enough to imply they are away or asleep.

Policy invariants:
- No hardcoded quiet hours or clock-time checks.
- morning_routine, user_ptt, and explicit_reminder always bypass suppression.
- Proactive/check-in/wrap-up speech is suppressed only by presence signals.
- If all detectors fail, presence is unknown and speech is allowed.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any

from prometheus.infra.utils import log_event

# ── Reason taxonomy ───────────────────────────────────────────────────────────

# Any reason that starts with or contains one of these prefixes bypasses all checks.
_ALWAYS_ALLOW_SUBSTRINGS = ("morning_routine", "user_ptt", "explicit_reminder")


# ── Env config ────────────────────────────────────────────────────────────────

def _cfg_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes")


def _cfg_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip())
    except (ValueError, TypeError):
        return default


# ── Presence detection ────────────────────────────────────────────────────────

@dataclass
class PresenceState:
    screen_locked: bool | None = None
    idle_minutes: float | None = None
    presence_source: str = "unknown"
    detection_errors: list[str] = field(default_factory=list)


def _run(args: list[str], timeout: float = 2.0) -> tuple[int, str]:
    """Run subprocess; return (returncode, stdout). Never raises."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip()
    except FileNotFoundError:
        return 127, ""
    except Exception:
        return 1, ""


def _detect_locked_loginctl() -> bool | None:
    """Check LockedHint via loginctl (reliable on KDE + systemd-logind)."""
    rc, out = _run(["loginctl", "list-sessions", "--no-legend"])
    if rc != 0 or not out:
        return None
    parts = out.split()
    if not parts:
        return None
    session_id = parts[0]
    rc2, props = _run(["loginctl", "show-session", session_id, "--property=LockedHint"])
    if rc2 != 0:
        return None
    m = re.search(r"LockedHint=(\w+)", props)
    if m:
        return m.group(1).lower() == "yes"
    return None


def _detect_locked_dbus() -> bool | None:
    """Check org.freedesktop.ScreenSaver.GetActive via dbus-send (fallback)."""
    rc, out = _run([
        "dbus-send", "--print-reply",
        "--dest=org.freedesktop.ScreenSaver",
        "/ScreenSaver",
        "org.freedesktop.ScreenSaver.GetActive",
    ])
    if rc != 0:
        return None
    m = re.search(r"boolean\s+(true|false)", out)
    if m:
        return m.group(1) == "true"
    return None


def _detect_idle_minutes_dbus() -> float | None:
    """Return idle time in minutes via GetSessionIdleTime (returns ms)."""
    rc, out = _run([
        "dbus-send", "--print-reply",
        "--dest=org.freedesktop.ScreenSaver",
        "/ScreenSaver",
        "org.freedesktop.ScreenSaver.GetSessionIdleTime",
    ])
    if rc != 0:
        return None
    m = re.search(r"uint32\s+(\d+)", out)
    if m:
        ms = int(m.group(1))
        return ms / 60_000.0
    return None


def _detect_idle_hint_loginctl() -> bool | None:
    """Return True if loginctl IdleHint=yes (coarse — system-managed threshold)."""
    rc, out = _run(["loginctl", "list-sessions", "--no-legend"])
    if rc != 0 or not out:
        return None
    parts = out.split()
    if not parts:
        return None
    session_id = parts[0]
    rc2, props = _run(["loginctl", "show-session", session_id, "--property=IdleHint"])
    if rc2 != 0:
        return None
    m = re.search(r"IdleHint=(\w+)", props)
    if m:
        return m.group(1).lower() == "yes"
    return None


def detect_presence() -> PresenceState:
    """
    Detect user presence using available local signals.

    Detection order:
    1. loginctl LockedHint  — KDE/systemd screen lock (most reliable here)
    2. DBus GetActive       — ScreenSaver lock status (fallback)
    3. DBus GetSessionIdleTime — exact idle time in milliseconds
    4. loginctl IdleHint    — coarse idle boolean (fallback when 3 fails)

    Never raises. Returns PresenceState with None fields if all detectors fail.
    """
    state = PresenceState()

    # ── Lock status ───────────────────────────────────────────────────────────
    locked = _detect_locked_loginctl()
    if locked is not None:
        state.screen_locked = locked
        state.presence_source = "loginctl"
    else:
        locked = _detect_locked_dbus()
        if locked is not None:
            state.screen_locked = locked
            state.presence_source = "dbus_screensaver"

    # ── Idle time ─────────────────────────────────────────────────────────────
    idle_min = _detect_idle_minutes_dbus()
    if idle_min is not None:
        state.idle_minutes = idle_min
        state.presence_source = (
            state.presence_source + "+dbus_idle"
            if state.presence_source != "unknown"
            else "dbus_idle"
        )
    else:
        idle_hint = _detect_idle_hint_loginctl()
        if idle_hint is not None:
            # Treat IdleHint=yes as ~5 minutes idle (system threshold), no as <1 min
            state.idle_minutes = 5.0 if idle_hint else 0.0
            state.presence_source = (
                state.presence_source + "+loginctl_idle_hint"
                if state.presence_source != "unknown"
                else "loginctl_idle_hint"
            )

    return state


# ── Policy ────────────────────────────────────────────────────────────────────

def should_allow_proactive_speech(
    reason: str,
    context: dict[str, Any] | None = None,
) -> bool:
    """
    Returns True if proactive speech is permitted right now.

    Always True for: morning_routine, user_ptt, explicit_reminder.
    For all other reasons, evaluates screen lock + idle time.
    Never suppresses based on time of day or clock.
    """
    reason_lower = reason.lower().replace("-", "_")

    # Always-allow — bypasses all presence checks
    for substr in _ALWAYS_ALLOW_SUBSTRINGS:
        if substr in reason_lower:
            log_event("proactive_speech_allowed", {
                "reason": reason,
                "policy": "always_allow",
                "screen_locked": None,
                "idle_minutes": None,
                "recent_activity": None,
                "presence_source": "policy",
            })
            return True

    # Read config
    enabled = _cfg_bool("PROMETHEUS_SUPPRESS_PROACTIVE_WHEN_LOCKED_AND_IDLE", True)
    idle_threshold = _cfg_int("PROMETHEUS_PROACTIVE_IDLE_THRESHOLD_MINUTES", 20)
    recent_threshold = _cfg_int("PROMETHEUS_PROACTIVE_RECENT_ACTIVITY_MINUTES", 15)
    suppress_when_locked = _cfg_bool("PROMETHEUS_PROACTIVE_SUPPRESS_WHEN_LOCKED", True)

    if not enabled:
        log_event("proactive_speech_allowed", {
            "reason": reason,
            "policy": "suppression_disabled",
            "screen_locked": None,
            "idle_minutes": None,
            "recent_activity": None,
            "presence_source": "config",
        })
        return True

    presence = detect_presence()
    screen_locked = presence.screen_locked
    idle_min = presence.idle_minutes

    # recent_activity = True only when we positively know idle is below the recent threshold
    recent_activity: bool | None = (
        idle_min < recent_threshold if idle_min is not None else None
    )

    suppress = False
    policy_applied = "none"

    if suppress_when_locked and screen_locked:
        # Condition 1: locked + idle exceeds threshold
        if idle_min is not None and idle_min >= idle_threshold:
            suppress = True
            policy_applied = "locked_and_high_idle"
        # Condition 2: locked + no evidence of recent activity
        elif recent_activity is not True:
            suppress = True
            policy_applied = "locked_and_no_recent_activity"

    # Condition 3: high idle regardless of lock status
    if not suppress and idle_min is not None and idle_min >= idle_threshold:
        suppress = True
        policy_applied = "high_idle_regardless_of_lock"

    payload: dict[str, Any] = {
        "reason": reason,
        "screen_locked": screen_locked,
        "idle_minutes": round(idle_min, 2) if idle_min is not None else None,
        "recent_activity": recent_activity,
        "presence_source": presence.presence_source,
        "policy": policy_applied if suppress else "allowed",
    }

    if suppress:
        log_event("proactive_speech_suppressed", payload)
        return False

    log_event("proactive_speech_allowed", payload)
    return True

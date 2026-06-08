"""
prometheus/integrations/ha_verifier.py — Post-state verification for HA script calls.

Called from tools.py after run_ha_script() returns ok=True.
Reads HA entity state via GET /api/states/{entity_id} and compares to expected outcome.

Return contract:
  verified_success      — state matched expected outcome
  accepted_unverified   — entity not configured, GET failed, or outcome ambiguous
  verified_failure      — state explicitly contradicts expected outcome
  None                  — script type not verifiable (caller keeps accepted_unverified default)
"""
from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

from config import CONFIG
from utils import log_event

if TYPE_CHECKING:
    from tools import ToolResult

# Xbox media player entity (same constant used by workspace_manager.py)
_XBOX_ENTITY = "media_player.blackhawkred"

# Wait times after script call before reading state
_DELAY_LIGHTS_S = 1.2
_DELAY_XBOX_POWER_S = 2.5
_DELAY_XBOX_APP_S = 3.0
_DELAY_XBOX_MEDIA_S = 1.2

# Expected hue ranges for color verification [lo, hi] in degrees (0-360)
# Red wraps around 0, handled specially
_EXPECTED_HUE: dict[str, tuple[float, float]] = {
    "green":  (100.0, 150.0),
    "blue":   (220.0, 260.0),
    "purple": (270.0, 310.0),
}

# App name fragments in HA app_name attribute for each Xbox app script
_XBOX_APP_FRAGMENTS: dict[str, str] = {
    "youtube": "youtube",
    "netflix": "netflix",
    "spotify": "spotify",
}


# ---------------------------------------------------------------------------
# Low-level HA state fetch (no side-effects except optional logging)
# ---------------------------------------------------------------------------

def _get_ha_state(entity_id: str) -> dict | None:
    """
    GET /api/states/{entity_id}.

    Returns:
      dict   — parsed JSON state object (entity found)
      {}     — entity returned 404 (not configured in HA)
      None   — request failed or credentials missing
    """
    base_url = os.getenv("HOME_ASSISTANT_URL", "").strip().rstrip("/")
    token = os.getenv("HOME_ASSISTANT_API_KEY", "").strip()
    if not base_url or not token:
        return None
    url = f"{base_url}/api/states/{entity_id}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        import requests
        resp = requests.get(url, headers=headers, timeout=4)
        if resp.status_code == 404:
            return {}
        if resp.status_code >= 400:
            return None
        return resp.json()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# ToolResult factory helpers (local import avoids circular at module load)
# ---------------------------------------------------------------------------

def _accepted(message: str, summary: str = "") -> "ToolResult":
    from tools import ToolResult
    r = ToolResult.accepted_unverified(message)
    if summary:
        r.verification_summary = summary
    return r


def _success(message: str, summary: str = "", actual: dict | None = None) -> "ToolResult":
    from tools import ToolResult
    return ToolResult.verified_success(message, summary=summary, actual_state=actual or {})


def _failure(message: str, summary: str = "", actual: dict | None = None) -> "ToolResult":
    from tools import ToolResult
    return ToolResult.verified_failure(message, summary=summary, actual_state=actual or {})


# ---------------------------------------------------------------------------
# Color verification helper
# ---------------------------------------------------------------------------

def _color_matches(color_name: str, hs_color: object, rgb_color: object) -> bool:
    """
    Return True if hs_color or rgb_color approximately matches color_name.
    Conservative: returns False rather than guessing when data is ambiguous.
    """
    if isinstance(hs_color, (list, tuple)) and len(hs_color) >= 2:
        hue = float(hs_color[0])
        sat = float(hs_color[1])
        if sat < 30.0:
            return False  # Too desaturated to confirm a specific color
        if color_name == "red":
            return hue <= 15.0 or hue >= 345.0
        lo, hi = _EXPECTED_HUE.get(color_name, (0.0, -1.0))
        return lo <= hue <= hi

    if isinstance(rgb_color, (list, tuple)) and len(rgb_color) >= 3:
        r, g, b = float(rgb_color[0]), float(rgb_color[1]), float(rgb_color[2])
        if color_name == "red":
            return r > 150.0 and g < 80.0 and b < 80.0
        if color_name == "green":
            return g > 150.0 and r < 80.0 and b < 80.0
        if color_name == "blue":
            return b > 150.0 and r < 80.0 and g < 80.0
        if color_name == "purple":
            return r > 100.0 and b > 100.0 and g < 80.0

    return False


# ---------------------------------------------------------------------------
# Lights verifier
# ---------------------------------------------------------------------------

def _verify_lights(script_name: str, trace_id: str) -> "ToolResult":
    """Verify light entity state after a jarvis_lights_* script call."""
    light_entity = CONFIG.get("ha_light_entity", "").strip()
    msg_base = f"Executed Home Assistant script: {script_name}"

    log_event("ha_command_sent", {
        "trace_id": trace_id,
        "script": script_name,
        "verify_entity": light_entity or "none",
    })

    if not light_entity:
        log_event("ha_verification_result", {
            "trace_id": trace_id, "script": script_name,
            "result": "accepted_unverified", "reason": "ha_light_entity_not_configured",
        })
        return _accepted(msg_base, "Light entity not configured — outcome unverifiable")

    time.sleep(_DELAY_LIGHTS_S)

    state_data = _get_ha_state(light_entity)

    log_event("ha_post_state_fetch", {
        "trace_id": trace_id,
        "entity_id": light_entity,
        "got_data": state_data is not None,
        "state": str(state_data.get("state", "")) if state_data else "",
    })

    if state_data is None:
        log_event("ha_verification_result", {
            "trace_id": trace_id, "script": script_name,
            "result": "accepted_unverified", "reason": "state_fetch_failed",
        })
        return _accepted(msg_base, "Light state fetch failed — outcome unverifiable")

    if not state_data:
        log_event("ha_verification_result", {
            "trace_id": trace_id, "script": script_name,
            "result": "accepted_unverified", "reason": "entity_not_found",
        })
        return _accepted(msg_base, f"Light entity '{light_entity}' not found in HA")

    current_state = str(state_data.get("state", "")).lower()
    attrs = state_data.get("attributes") or {}
    actual = {
        "state": current_state,
        "hs_color": attrs.get("hs_color"),
        "rgb_color": attrs.get("rgb_color"),
        "brightness": attrs.get("brightness"),
    }

    # power_on — expect state == "on"
    if script_name == "jarvis_lights_power_on":
        if current_state == "on":
            log_event("ha_verification_result", {
                "trace_id": trace_id, "script": script_name,
                "result": "verified_success", "state": current_state,
            })
            return _success(msg_base, f"Light confirmed on", actual)
        if current_state == "off":
            log_event("ha_verification_result", {
                "trace_id": trace_id, "script": script_name,
                "result": "verified_failure", "state": current_state,
            })
            return _failure(msg_base, "Light still off after power_on command", actual)
        return _accepted(msg_base, f"Light state '{current_state}' — transitional, outcome unclear")

    # power_off — expect state == "off"
    if script_name == "jarvis_lights_power_off":
        if current_state == "off":
            log_event("ha_verification_result", {
                "trace_id": trace_id, "script": script_name,
                "result": "verified_success", "state": current_state,
            })
            return _success(msg_base, "Light confirmed off", actual)
        if current_state == "on":
            log_event("ha_verification_result", {
                "trace_id": trace_id, "script": script_name,
                "result": "verified_failure", "state": current_state,
            })
            return _failure(msg_base, "Light still on after power_off command", actual)
        return _accepted(msg_base, f"Light state '{current_state}' — power_off outcome unclear")

    # color scenes — verify on + color
    for color in ("red", "blue", "green", "purple"):
        if script_name == f"jarvis_lights_scene_{color}":
            if current_state == "off":
                log_event("ha_verification_result", {
                    "trace_id": trace_id, "script": script_name,
                    "result": "verified_failure", "state": current_state,
                })
                return _failure(msg_base, f"Light is off after {color} scene command", actual)
            if current_state != "on":
                return _accepted(msg_base, f"Light state '{current_state}' — color unverifiable")
            # Light is on — attempt color check
            if _color_matches(color, attrs.get("hs_color"), attrs.get("rgb_color")):
                log_event("ha_verification_result", {
                    "trace_id": trace_id, "script": script_name,
                    "result": "verified_success", "color": color,
                    "hs_color": str(attrs.get("hs_color")),
                })
                return _success(msg_base, f"Light confirmed on with {color} color", actual)
            log_event("ha_verification_result", {
                "trace_id": trace_id, "script": script_name,
                "result": "accepted_unverified", "reason": "color_unverifiable",
                "hs_color": str(attrs.get("hs_color")),
                "rgb_color": str(attrs.get("rgb_color")),
            })
            return _accepted(
                msg_base,
                f"Light is on but {color} color cannot be confirmed from entity attributes",
            )

    # Other scene commands (movie/work/night/disco) — verify light is on, can't verify scene settings
    if "scene" in script_name:
        if current_state == "on":
            log_event("ha_verification_result", {
                "trace_id": trace_id, "script": script_name,
                "result": "accepted_unverified", "reason": "scene_settings_unverifiable",
            })
            return _accepted(msg_base, "Light is on — scene applied but specific settings cannot be verified")
        if current_state == "off":
            return _failure(msg_base, f"Light is off after scene command {script_name}", actual)

    # Brightness and other light commands — light must be on for these to do anything meaningful
    if current_state == "on":
        log_event("ha_verification_result", {
            "trace_id": trace_id, "script": script_name,
            "result": "accepted_unverified", "reason": "command_type_unverifiable",
        })
        return _accepted(msg_base, "Light is on — exact command outcome cannot be verified")

    log_event("ha_verification_result", {
        "trace_id": trace_id, "script": script_name,
        "result": "accepted_unverified", "reason": "ambiguous_state",
        "state": current_state,
    })
    return _accepted(msg_base, f"Light state '{current_state}' — script outcome unverifiable")


# ---------------------------------------------------------------------------
# Xbox / media verifier
# ---------------------------------------------------------------------------

def _verify_xbox(script_name: str, trace_id: str) -> "ToolResult":
    """Verify Xbox/media entity state after a jarvis_xbox_* script call."""
    entity_id = _XBOX_ENTITY
    msg_base = f"Executed Home Assistant script: {script_name}"

    log_event("ha_command_sent", {
        "trace_id": trace_id,
        "script": script_name,
        "verify_entity": entity_id,
    })

    # Classify script and set wait time
    if "power_on" in script_name:
        expected = "power_on"
        wait = _DELAY_XBOX_POWER_S
    elif "power_off" in script_name:
        expected = "power_off"
        wait = _DELAY_XBOX_POWER_S
    elif "_app_" in script_name:
        expected = "app"
        wait = _DELAY_XBOX_APP_S
    elif "media_pause" in script_name:
        expected = "pause"
        wait = _DELAY_XBOX_MEDIA_S
    elif "media_resume" in script_name:
        expected = "resume"
        wait = _DELAY_XBOX_MEDIA_S
    elif "volume" in script_name:
        log_event("ha_verification_result", {
            "trace_id": trace_id, "script": script_name,
            "result": "accepted_unverified", "reason": "volume_not_verifiable",
        })
        return _accepted(msg_base, "Volume commands cannot be verified from entity state")
    else:
        log_event("ha_verification_result", {
            "trace_id": trace_id, "script": script_name,
            "result": "accepted_unverified", "reason": "unknown_xbox_command_type",
        })
        return _accepted(msg_base, "Unknown Xbox command type — outcome unverifiable")

    time.sleep(wait)

    state_data = _get_ha_state(entity_id)

    log_event("ha_post_state_fetch", {
        "trace_id": trace_id,
        "entity_id": entity_id,
        "got_data": state_data is not None,
        "state": str(state_data.get("state", "")) if state_data else "",
    })

    if state_data is None:
        log_event("ha_verification_result", {
            "trace_id": trace_id, "script": script_name,
            "result": "accepted_unverified", "reason": "state_fetch_failed",
        })
        return _accepted(msg_base, "Xbox state fetch failed — outcome unverifiable")

    if not state_data:
        log_event("ha_verification_result", {
            "trace_id": trace_id, "script": script_name,
            "result": "accepted_unverified", "reason": "entity_not_found",
        })
        return _accepted(msg_base, f"Xbox entity '{entity_id}' not found in HA")

    current_state = str(state_data.get("state", "")).lower()
    attrs = state_data.get("attributes") or {}
    app_name = str(attrs.get("app_name") or attrs.get("source") or "").lower()
    actual = {
        "state": current_state,
        "app_name": app_name,
        "media_title": str(attrs.get("media_title") or ""),
    }

    if expected == "power_on":
        if current_state not in ("off", "unavailable", "unknown"):
            log_event("ha_verification_result", {
                "trace_id": trace_id, "script": script_name,
                "result": "verified_success", "state": current_state,
            })
            return _success(msg_base, f"Xbox confirmed on (state={current_state})", actual)
        log_event("ha_verification_result", {
            "trace_id": trace_id, "script": script_name,
            "result": "verified_failure", "state": current_state,
        })
        return _failure(
            msg_base, f"Xbox still off or unavailable after power_on (state={current_state})", actual
        )

    if expected == "power_off":
        if current_state in ("off", "unavailable"):
            log_event("ha_verification_result", {
                "trace_id": trace_id, "script": script_name,
                "result": "verified_success", "state": current_state,
            })
            return _success(msg_base, "Xbox confirmed off", actual)
        log_event("ha_verification_result", {
            "trace_id": trace_id, "script": script_name,
            "result": "verified_failure", "state": current_state,
        })
        return _failure(
            msg_base, f"Xbox still active after power_off (state={current_state})", actual
        )

    if expected == "app":
        expected_app_fragment = next(
            (v for k, v in _XBOX_APP_FRAGMENTS.items() if k in script_name), None
        )
        if expected_app_fragment and expected_app_fragment in app_name:
            log_event("ha_verification_result", {
                "trace_id": trace_id, "script": script_name,
                "result": "verified_success", "app_name": app_name,
            })
            return _success(
                msg_base,
                f"Xbox confirmed on {expected_app_fragment} (app_name='{app_name}')",
                actual,
            )
        # App may still be loading — not a confirmed failure
        log_event("ha_verification_result", {
            "trace_id": trace_id, "script": script_name,
            "result": "accepted_unverified", "reason": "app_not_yet_visible",
            "app_name": app_name, "expected": expected_app_fragment or "unknown",
        })
        return _accepted(
            msg_base,
            f"Xbox state={current_state} app='{app_name or 'unknown'}' — "
            f"{expected_app_fragment or 'app'} may still be loading",
        )

    if expected == "pause":
        if current_state == "paused":
            log_event("ha_verification_result", {
                "trace_id": trace_id, "script": script_name,
                "result": "verified_success", "state": current_state,
            })
            return _success(msg_base, "Xbox playback confirmed paused", actual)
        log_event("ha_verification_result", {
            "trace_id": trace_id, "script": script_name,
            "result": "accepted_unverified", "state": current_state,
        })
        return _accepted(msg_base, f"Xbox state '{current_state}' after pause — unconfirmed")

    if expected == "resume":
        if current_state == "playing":
            log_event("ha_verification_result", {
                "trace_id": trace_id, "script": script_name,
                "result": "verified_success", "state": current_state,
            })
            return _success(msg_base, "Xbox playback confirmed playing", actual)
        log_event("ha_verification_result", {
            "trace_id": trace_id, "script": script_name,
            "result": "accepted_unverified", "state": current_state,
        })
        return _accepted(msg_base, f"Xbox state '{current_state}' after resume — unconfirmed")

    return _accepted(msg_base, "Xbox script outcome unverifiable")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def verify_ha_script(script_name: str, trace_id: str = "") -> "ToolResult | None":
    """
    Route a just-fired HA script to the appropriate post-state verifier.

    Returns a ToolResult if the script type is verifiable, None otherwise.
    None means the caller should keep the default accepted_unverified result
    from run_ha_script().
    """
    if script_name.startswith("jarvis_lights_"):
        return _verify_lights(script_name, trace_id)
    if script_name.startswith("jarvis_xbox_"):
        return _verify_xbox(script_name, trace_id)
    # Routine scripts (jarvis_routine_*) and any unknown scripts — not verifiable
    return None

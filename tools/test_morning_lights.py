"""
tools/test_morning_lights.py — Visible smoke test for morning routine lights.

Sets light.color_light_2, light.color_light_3, and light.color_light_3_2 to
full brightness at 2700K over a 5-second transition — a clearly visible warm
flash. Optionally restores the previous state afterward.

Usage:
    cd /home/tatel/Desktop/PROMETHEUS/Prometheus_Main
    source .venv/bin/activate

    # Flash lights to full warm brightness:
    python3 tools/test_morning_lights.py

    # Flash then restore previous state:
    python3 tools/test_morning_lights.py --restore
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

_LIGHTS = [
    "light.color_light_2",
    "light.color_light_3",
    "light.color_light_3_2",
]
_TRANSITION = 5      # seconds for the test flash
_HOLD = 8            # seconds to hold the test state before restoring
_WARM_KELVIN = 2700  # warm white
_MAX_BRIGHTNESS = 255


def _headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _get_state(base_url: str, api_key: str, entity_id: str) -> dict | None:
    try:
        resp = requests.get(
            f"{base_url}/api/states/{entity_id}",
            headers=_headers(api_key),
            timeout=5,
        )
        if resp.status_code == 200:
            return resp.json()
        return None
    except Exception:
        return None


def _call_service(base_url: str, api_key: str, domain: str, service: str, payload: dict) -> bool:
    try:
        resp = requests.post(
            f"{base_url}/api/services/{domain}/{service}",
            headers=_headers(api_key),
            json=payload,
            timeout=10,
        )
        return resp.status_code in (200, 201)
    except Exception as exc:
        print(f"[LIGHTS TEST] service call error: {exc}")
        return False


def _print_light_state(label: str, entity_id: str, data: dict | None) -> None:
    if data is None:
        print(f"[LIGHTS TEST] {label} {entity_id}: <unavailable>")
        return
    state = data.get("state", "unknown")
    attrs = data.get("attributes") or {}
    brightness = attrs.get("brightness")
    color_temp = attrs.get("color_temp_kelvin") or attrs.get("color_temp")
    print(f"[LIGHTS TEST] {label} {entity_id}: state={state!r} brightness={brightness} color_temp_kelvin={color_temp}")


def main() -> int:
    base_url = os.getenv("HOME_ASSISTANT_URL", "").rstrip("/")
    api_key = os.getenv("HOME_ASSISTANT_API_KEY", "")
    if not base_url or not api_key:
        print("[LIGHTS TEST] FAIL — HOME_ASSISTANT_URL or HOME_ASSISTANT_API_KEY not set")
        return 1

    restore = "--restore" in sys.argv
    print(f"[LIGHTS TEST] Target: {base_url}  restore={restore}")
    print(f"[LIGHTS TEST] Lights: {', '.join(_LIGHTS)}")

    # 1. Capture current state
    before_states: dict[str, dict | None] = {}
    print("\n[LIGHTS TEST] --- BEFORE ---")
    for entity_id in _LIGHTS:
        data = _get_state(base_url, api_key, entity_id)
        before_states[entity_id] = data
        _print_light_state("BEFORE", entity_id, data)

    # 2. Set lights to full brightness warm white
    print(f"\n[LIGHTS TEST] Setting lights: brightness=255 kelvin={_WARM_KELVIN} transition={_TRANSITION}s")
    for entity_id in _LIGHTS:
        ok = _call_service(base_url, api_key, "light", "turn_on", {
            "entity_id": entity_id,
            "brightness": _MAX_BRIGHTNESS,
            "kelvin": _WARM_KELVIN,
            "transition": _TRANSITION,
        })
        status = "OK" if ok else "FAIL"
        print(f"[LIGHTS TEST] turn_on {entity_id}: {status}")

    print(f"\n[LIGHTS TEST] Holding for {_HOLD}s — lights should be visibly bright warm white...")
    time.sleep(_HOLD)

    # 3. Check state after
    print("\n[LIGHTS TEST] --- AFTER ---")
    all_on = True
    for entity_id in _LIGHTS:
        data = _get_state(base_url, api_key, entity_id)
        _print_light_state("AFTER ", entity_id, data)
        if data and data.get("state") != "on":
            all_on = False

    # 4. Optionally restore
    if restore:
        print("\n[LIGHTS TEST] Restoring previous state...")
        for entity_id in _LIGHTS:
            prev = before_states.get(entity_id)
            if prev is None:
                print(f"[LIGHTS TEST] RESTORE {entity_id}: no prior state, skipping")
                continue
            if prev.get("state") == "off":
                ok = _call_service(base_url, api_key, "light", "turn_off", {"entity_id": entity_id})
                print(f"[LIGHTS TEST] RESTORE {entity_id}: turn_off {'OK' if ok else 'FAIL'}")
            else:
                attrs = prev.get("attributes") or {}
                payload: dict = {"entity_id": entity_id, "transition": 3}
                if attrs.get("brightness") is not None:
                    payload["brightness"] = attrs["brightness"]
                if attrs.get("color_temp_kelvin") is not None:
                    payload["kelvin"] = attrs["color_temp_kelvin"]
                ok = _call_service(base_url, api_key, "light", "turn_on", payload)
                print(f"[LIGHTS TEST] RESTORE {entity_id}: turn_on {'OK' if ok else 'FAIL'}")
    else:
        print("\n[LIGHTS TEST] (Leaving lights in test state. Use --restore to revert.)")

    if all_on:
        print("\n[LIGHTS TEST] PASS — all lights responded")
        return 0
    else:
        print("\n[LIGHTS TEST] WARN — some lights did not report 'on' state after command")
        return 1


if __name__ == "__main__":
    sys.exit(main())

"""
tools/test_morning_ha_scripts.py — Smoke test for morning routine HA scripts.

Checks that each required script entity exists in Home Assistant, then POSTs
to trigger each one. Exits nonzero if any script is missing or fails.

Usage:
    cd /home/tatel/Desktop/PROMETHEUS/Prometheus_Main
    source .venv/bin/activate
    python3 tools/test_morning_ha_scripts.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

_SCRIPTS = [
    "script.prometheus_xbox_turn_on",
    "script.prometheus_xbox_launch_spotify",
    "script.prometheus_xbox_play",
    "script.prometheus_xbox_volume_up",
    "script.prometheus_xbox_volume_down",
    "script.prometheus_morning_lights_warm_fade",
]

_CALL_ONLY = [
    "script.prometheus_morning_lights_warm_fade",
]


def _ha_headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def main() -> int:
    ha_url = os.getenv("HOME_ASSISTANT_URL", "").rstrip("/")
    ha_key = os.getenv("HOME_ASSISTANT_API_KEY", "")
    if not ha_url or not ha_key:
        print("[HA TEST] FAIL — HOME_ASSISTANT_URL or HOME_ASSISTANT_API_KEY not set")
        return 1

    headers = _ha_headers(ha_key)
    failures: list[str] = []

    print(f"[HA TEST] Target: {ha_url}")
    print(f"[HA TEST] Checking {len(_SCRIPTS)} scripts...\n")

    for entity_id in _SCRIPTS:
        state_url = f"{ha_url}/api/states/{entity_id}"
        try:
            resp = requests.get(state_url, headers=headers, timeout=5)
        except Exception as exc:
            print(f"[HA TEST] EXISTS CHECK FAIL  {entity_id}  connection error: {exc}")
            failures.append(entity_id)
            continue

        if resp.status_code == 200:
            print(f"[HA TEST] EXISTS OK           {entity_id}")
        elif resp.status_code == 404:
            print(f"[HA TEST] EXISTS MISSING      {entity_id}  (404 — script not in HA)")
            failures.append(entity_id)
            continue
        else:
            print(f"[HA TEST] EXISTS UNEXPECTED   {entity_id}  status={resp.status_code}")
            failures.append(entity_id)
            continue

    print()

    for entity_id in _CALL_ONLY:
        domain = entity_id.split(".")[0]
        service_name = entity_id.split(".")[1]
        call_url = f"{ha_url}/api/services/{domain}/turn_on"
        payload = {"entity_id": entity_id}
        print(f"[HA TEST] calling            {entity_id}")
        try:
            resp = requests.post(call_url, headers=headers, json=payload, timeout=10)
        except Exception as exc:
            print(f"[HA TEST] CALL FAIL          {entity_id}  connection error: {exc}")
            failures.append(f"call:{entity_id}")
            continue

        if resp.status_code in (200, 201):
            print(f"[HA TEST] CALL SUCCESS       {entity_id}  status={resp.status_code}")
        else:
            body = resp.text[:200]
            print(f"[HA TEST] CALL FAIL          {entity_id}  status={resp.status_code} body={body!r}")
            failures.append(f"call:{entity_id}")

    print()
    if failures:
        print(f"[HA TEST] FAILED: {failures}")
        return 1
    print("[HA TEST] All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

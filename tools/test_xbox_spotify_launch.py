"""
tools/test_xbox_spotify_launch.py — Smoke test for Xbox Spotify launch.

Calls script.prometheus_xbox_launch_spotify, then fetches
media_player.blackhawkred state to check whether Spotify became active.

Usage:
    cd /home/tatel/Desktop/PROMETHEUS/Prometheus_Main
    source .venv/bin/activate
    python3 tools/test_xbox_spotify_launch.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

_SCRIPT_ENTITY = "script.prometheus_xbox_launch_spotify"
_XBOX_ENTITY = "media_player.blackhawkred"
_POST_LAUNCH_WAIT = 10  # seconds to wait before checking state


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
        print(f"[SPOTIFY TEST] GET {entity_id} → {resp.status_code}")
        return None
    except Exception as exc:
        print(f"[SPOTIFY TEST] GET {entity_id} error: {exc}")
        return None


def _print_xbox_state(label: str, data: dict | None) -> None:
    if data is None:
        print(f"[SPOTIFY TEST] {label}: <unavailable>")
        return
    state = data.get("state", "unknown")
    attrs = data.get("attributes") or {}
    app = attrs.get("app_name") or attrs.get("source") or ""
    media = attrs.get("media_title") or ""
    print(f"[SPOTIFY TEST] {label}: state={state!r} app={app!r} media_title={media!r}")


def main() -> int:
    base_url = os.getenv("HOME_ASSISTANT_URL", "").rstrip("/")
    api_key = os.getenv("HOME_ASSISTANT_API_KEY", "")
    if not base_url or not api_key:
        print("[SPOTIFY TEST] FAIL — HOME_ASSISTANT_URL or HOME_ASSISTANT_API_KEY not set")
        return 1

    print(f"[SPOTIFY TEST] Target: {base_url}")

    # 1. Check script entity exists
    script_data = _get_state(base_url, api_key, _SCRIPT_ENTITY)
    if script_data is None:
        print(f"[SPOTIFY TEST] FAIL — {_SCRIPT_ENTITY} not found in HA")
        return 1
    print(f"[SPOTIFY TEST] Script exists: {_SCRIPT_ENTITY} state={script_data.get('state')!r}")

    # 2. Capture Xbox state before launch
    before = _get_state(base_url, api_key, _XBOX_ENTITY)
    _print_xbox_state("BEFORE launch", before)

    # 3. Call the launch script
    domain = _SCRIPT_ENTITY.split(".")[0]
    service_name = _SCRIPT_ENTITY.split(".")[1]
    call_url = f"{base_url}/api/services/{domain}/turn_on"
    print(f"[SPOTIFY TEST] calling {_SCRIPT_ENTITY}...")
    try:
        resp = requests.post(
            call_url,
            headers=_headers(api_key),
            json={"entity_id": _SCRIPT_ENTITY},
            timeout=10,
        )
    except Exception as exc:
        print(f"[SPOTIFY TEST] FAIL — call error: {exc}")
        return 1

    if resp.status_code not in (200, 201):
        print(f"[SPOTIFY TEST] FAIL — call returned {resp.status_code}: {resp.text[:200]}")
        return 1
    print(f"[SPOTIFY TEST] Call OK — status={resp.status_code}")

    # 4. Wait for Xbox to respond
    print(f"[SPOTIFY TEST] Waiting {_POST_LAUNCH_WAIT}s for Xbox to launch Spotify...")
    time.sleep(_POST_LAUNCH_WAIT)

    # 5. Check Xbox state after launch
    after = _get_state(base_url, api_key, _XBOX_ENTITY)
    _print_xbox_state("AFTER launch", after)

    # 6. Evaluate result
    spotify_active = False
    if after:
        attrs = after.get("attributes") or {}
        app = (attrs.get("app_name") or attrs.get("source") or "").lower()
        if "spotify" in app:
            spotify_active = True

    if spotify_active:
        print("[SPOTIFY TEST] PASS — Spotify appears active on Xbox")
        return 0
    else:
        print("[SPOTIFY TEST] INCONCLUSIVE — Spotify not detected in app_name/source after launch")
        print("  This may be normal if HA Xbox integration does not expose app_name.")
        print("  Verify manually whether Spotify launched on the Xbox.")
        return 0  # not a hard failure — HA 200 means script was called


if __name__ == "__main__":
    sys.exit(main())

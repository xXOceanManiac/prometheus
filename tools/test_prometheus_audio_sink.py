"""
tools/test_prometheus_audio_sink.py — Diagnose and optionally fix the PipeWire audio sink.

Usage:
    python3 tools/test_prometheus_audio_sink.py          # read-only: show current state
    python3 tools/test_prometheus_audio_sink.py --fix    # switch to preferred sink now

Reads PROMETHEUS_AUDIO_SINK_NAME from the environment (or .env in the project root).
Prints the full wpctl status, highlights the current default and preferred sink,
and (with --fix) calls the same switch logic used by PrometheusMorningSpeaker before speech.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _load_dotenv() -> None:
    env_path = _ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _run(args: list[str]) -> tuple[int, str]:
    import subprocess
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=5)
        return r.returncode, r.stdout.strip()
    except FileNotFoundError:
        return 127, ""
    except Exception as exc:
        return 1, str(exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnose PipeWire audio sink for morning routine")
    parser.add_argument("--fix", action="store_true", help="Switch to preferred sink now")
    args = parser.parse_args()

    _load_dotenv()

    preferred_name = os.getenv("PROMETHEUS_AUDIO_SINK_NAME", "").strip()

    # ── 1. Check wpctl ────────────────────────────────────────────────────────
    rc, ver = _run(["wpctl", "--version"])
    if rc != 0:
        print("ERROR: wpctl not found or not working.")
        print("  Install WirePlumber: sudo apt install wireplumber")
        sys.exit(1)
    print(f"wpctl version: {ver}")

    # ── 2. Show full wpctl status ─────────────────────────────────────────────
    rc, status = _run(["wpctl", "status"])
    if rc != 0:
        print("ERROR: wpctl status failed.")
        sys.exit(1)

    print()
    print("── wpctl status (Sinks section) ─────────────────────────────────────")
    in_sinks = False
    import re
    for line in status.splitlines():
        stripped = line.strip()
        if stripped.startswith("Sinks:"):
            in_sinks = True
            print(line)
            continue
        if in_sinks:
            if stripped and stripped.endswith(":") and not re.match(r"\d", stripped[0]):
                break
            print(line)
    print()

    # ── 3. Parse sinks ────────────────────────────────────────────────────────
    from prometheus.routines.morning_adapters import _parse_wpctl_sinks
    sinks = _parse_wpctl_sinks(status)

    if not sinks:
        print("WARNING: No sinks found in wpctl status output.")
        sys.exit(0)

    print(f"{'ID':<6}  {'DEFAULT':<8}  Name")
    print("-" * 60)
    for s in sinks:
        marker = "  <--" if s["is_default"] else ""
        pref_marker = "  [PREFERRED]" if preferred_name and preferred_name.lower() in s["name"].lower() else ""
        print(f"{s['id']:<6}  {'*' if s['is_default'] else '':<8}  {s['name']}{marker}{pref_marker}")
    print()

    default_sink = next((s for s in sinks if s["is_default"]), None)
    preferred_sink = next(
        (s for s in sinks if preferred_name and preferred_name.lower() in s["name"].lower()),
        None,
    ) if preferred_name else None

    print(f"PROMETHEUS_AUDIO_SINK_NAME = {preferred_name!r}")
    print(f"Current default: {default_sink['name'] if default_sink else 'none'}")
    print(f"Preferred sink:  {preferred_sink['name'] if preferred_sink else 'not found / not configured'}")
    print()

    if not preferred_name:
        print("INFO: PROMETHEUS_AUDIO_SINK_NAME is not set.")
        print("  Morning routine will use whatever sink is currently default.")
        print("  To configure: add PROMETHEUS_AUDIO_SINK_NAME=<substring> to .env")
        print()
        print("Test tone (current default):")
        print("  speaker-test -t wav -c 2 -l 1")
        return

    if preferred_sink is None:
        print(f"WARNING: Preferred sink substring {preferred_name!r} not found in any sink name.")
        print("  Check the sink name above and update PROMETHEUS_AUDIO_SINK_NAME in .env.")
        sys.exit(1)

    already_preferred = default_sink and default_sink["id"] == preferred_sink["id"]
    if already_preferred:
        print("OK: Default sink is already the preferred sink.")
    else:
        print(f"MISMATCH: Default is {default_sink['name']!r}, preferred is {preferred_sink['name']!r}.")
        if args.fix:
            rc_sw, _ = _run(["wpctl", "set-default", preferred_sink["id"]])
            if rc_sw == 0:
                print(f"  Switched default to sink {preferred_sink['id']} ({preferred_sink['name']}).")
            else:
                print(f"  ERROR: wpctl set-default failed (rc={rc_sw}).")
                sys.exit(1)
        else:
            print("  Run with --fix to switch now.")

    if args.fix or already_preferred:
        rc_vol, _ = _run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.70"])
        rc_mut, _ = _run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"])
        print(f"  Volume set to 70%: {'OK' if rc_vol == 0 else 'FAILED'}")
        print(f"  Mute cleared:      {'OK' if rc_mut == 0 else 'FAILED'}")

    print()
    print("Test tone command (run in terminal to verify audio output):")
    print("  speaker-test -t wav -c 2 -l 1")


if __name__ == "__main__":
    main()

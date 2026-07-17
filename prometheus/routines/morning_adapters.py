"""
prometheus/routines/morning_adapters.py — Live adapters for MorningRoutineService.

Connects MorningRoutineService to the real Prometheus runtime without modifying
the routine logic itself. Each class implements one of the five dependency interfaces
that MorningRoutineService expects.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess as _subprocess
from pathlib import Path
from typing import Any

from prometheus.infra.utils import log_event

_SPEAK_TIMEOUT = 60.0  # seconds to wait for response.done before giving up

# Sink name substrings that indicate an HDMI/monitor output to avoid
_HDMI_PATTERNS = frozenset({"hdmi", "displayport", "display port", "monitor output"})


def _run_cmd(args: list[str]) -> tuple[int, str]:
    """Run a subprocess; return (returncode, stdout). Never raises."""
    try:
        r = _subprocess.run(args, capture_output=True, text=True, timeout=5)
        return r.returncode, r.stdout.strip()
    except FileNotFoundError:
        return 127, ""
    except Exception:
        return 1, ""


def _parse_wpctl_sinks(status: str) -> list[dict]:
    """Parse 'wpctl status' output and return a list of sink dicts.

    Each dict has: id (str), name (str), is_default (bool).
    """
    sinks: list[dict] = []
    in_sinks = False
    for line in status.splitlines():
        stripped = line.strip()
        if stripped.startswith("Sinks:"):
            in_sinks = True
            continue
        if in_sinks:
            # A line like "Sources:" or "Filters:" ends the sinks section
            if stripped and stripped.endswith(":") and not re.match(r"\d", stripped[0]):
                break
            m = re.match(r".*?(\*)?\s+(\d+)\.\s+(.+?)(?:\s+\[|$)", line)
            if m:
                sinks.append({
                    "is_default": m.group(1) == "*",
                    "id": m.group(2),
                    "name": m.group(3).strip(),
                })
    return sinks


def ensure_morning_audio_sink() -> None:
    """Check and switch to the preferred PipeWire audio sink before morning speech.

    Reads PROMETHEUS_AUDIO_SINK_NAME from env. If the current default is an
    HDMI/monitor output and the preferred sink is found, switches to it, sets
    volume to 70%, and unmutes. Never raises.
    """
    preferred_name = os.getenv("PROMETHEUS_AUDIO_SINK_NAME", "").strip()

    # Verify wpctl is present
    rc, _ = _run_cmd(["wpctl", "--version"])
    if rc != 0:
        print("[MORNING][AUDIO] wpctl not available — skipping sink check", flush=True)
        log_event("morning_routine_audio_wpctl_missing", {})
        return

    # Get current sink list
    rc, status_out = _run_cmd(["wpctl", "status"])
    if rc != 0:
        print("[MORNING][AUDIO] wpctl status failed — skipping", flush=True)
        return

    sinks = _parse_wpctl_sinks(status_out)
    default_sink = next((s for s in sinks if s["is_default"]), None)
    default_name = default_sink["name"] if default_sink else "unknown"
    print(f"[MORNING][AUDIO] default_sink={default_name!r}", flush=True)
    log_event("morning_routine_audio_check", {"default_sink": default_name, "sink_count": len(sinks)})

    if not preferred_name:
        print("[MORNING][AUDIO] PROMETHEUS_AUDIO_SINK_NAME not set — skipping switch", flush=True)
        # Still set volume and unmute on current default
        _run_cmd(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.70"])
        _run_cmd(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"])
        return

    # Find preferred sink — substring match (case-insensitive)
    preferred_sink = next(
        (s for s in sinks if preferred_name.lower() in s["name"].lower()),
        None,
    )
    if preferred_sink is None:
        print(f"[MORNING][AUDIO] preferred_sink {preferred_name!r} not found — skipping", flush=True)
        log_event("morning_routine_audio_preferred_not_found", {"preferred": preferred_name})
        return

    print(f"[MORNING][AUDIO] preferred_sink={preferred_sink['name']!r} id={preferred_sink['id']}", flush=True)

    # Switch if current default is HDMI/monitor OR just not the preferred one
    default_lower = default_name.lower()
    is_hdmi = any(p in default_lower for p in _HDMI_PATTERNS)
    already_preferred = default_sink and default_sink["id"] == preferred_sink["id"]

    switched = False
    if not already_preferred:
        rc_sw, _ = _run_cmd(["wpctl", "set-default", preferred_sink["id"]])
        switched = rc_sw == 0
    print(f"[MORNING][AUDIO] switched_to_preferred={switched} (was_hdmi={is_hdmi})", flush=True)
    log_event("morning_routine_audio_switch", {
        "switched": switched,
        "was_hdmi": is_hdmi,
        "to": preferred_sink["name"],
    })

    # Set volume and unmute on whichever sink is now default
    rc_vol, _ = _run_cmd(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.70"])
    rc_mut, _ = _run_cmd(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"])
    print(f"[MORNING][AUDIO] volume_set={rc_vol == 0}", flush=True)
    print(f"[MORNING][AUDIO] mute_cleared={rc_mut == 0}", flush=True)
    log_event("morning_routine_audio_configured", {
        "volume_set": rc_vol == 0,
        "mute_cleared": rc_mut == 0,
    })


# ── State store ───────────────────────────────────────────────────────────────

_STATE_PATH = Path.home() / "Desktop" / "PROMETHEUS" / "state" / "morning_routine_state.json"


class JSONMorningRoutineStateStore:
    """
    Persists MorningRoutineState to a JSON file.

    Creates the parent directory on first write.
    Backs up corrupt files before returning None so the routine can run fresh.
    Writes atomically via a temp file.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _STATE_PATH

    def load_state(self) -> Any | None:
        from prometheus.routines.morning_routine import MorningRoutineState

        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return MorningRoutineState(
                date=data.get("date", ""),
                event_id=data.get("event_id"),
                completed=bool(data.get("completed", False)),
                started_at=data.get("started_at"),
            )
        except (json.JSONDecodeError, OSError, KeyError) as exc:
            backup = self._path.with_suffix(".bak")
            try:
                shutil.copy2(self._path, backup)
            except Exception:
                pass
            log_event("morning_routine_state_corrupt", {
                "error": str(exc)[:200],
                "backup": str(backup),
            })
            return None

    def save_state(self, state: Any) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "date": state.date,
            "event_id": state.event_id,
            "completed": state.completed,
            "started_at": state.started_at,
        }
        tmp = self._path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
        except Exception as exc:
            log_event("morning_routine_state_write_error", {"error": str(exc)[:200]})
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass


# ── HA client ─────────────────────────────────────────────────────────────────

class HomeAssistantMorningClient:
    """
    Wraps run_ha_script() for MorningRoutineService.

    MorningRoutineService passes full entity IDs ("script.prometheus_xbox_turn_on").
    run_ha_script() expects the name without the "script." prefix, so this adapter
    strips it before calling through.
    """

    def call_script(self, entity_id: str) -> bool:
        ha_url = os.getenv("HOME_ASSISTANT_URL", "").strip()
        ha_key = os.getenv("HOME_ASSISTANT_API_KEY", "").strip()
        if not ha_url or not ha_key:
            print(f"[MORNING][HA] disabled — missing config (url={bool(ha_url)} key={bool(ha_key)})", flush=True)
            log_event("morning_routine_ha_config_missing", {
                "has_url": bool(ha_url),
                "has_key": bool(ha_key),
                "entity_id": entity_id,
            })
            return False

        from prometheus.execution.tools import run_ha_script

        name = entity_id
        if name.startswith("script."):
            name = name[len("script."):]

        print(f"[MORNING][HA] calling script={entity_id}", flush=True)
        log_event("morning_routine_ha_calling", {"entity_id": entity_id, "script_name": name})

        result = run_ha_script(name)
        if result.ok:
            print(f"[MORNING][HA] success script={entity_id}", flush=True)
            log_event("morning_routine_ha_success", {"entity_id": entity_id})
        else:
            print(f"[MORNING][HA] failed script={entity_id} message={result.message!r:.100}", flush=True)
            log_event("morning_routine_ha_failure", {
                "entity_id": entity_id,
                "message": (result.message or "")[:200],
            })
        return bool(result.ok)


# ── Speaker ───────────────────────────────────────────────────────────────────

class PrometheusMorningSpeaker:
    """
    Async speaker adapter for MorningRoutineService.

    Sends conversation.item.create then response.create so the Realtime
    client reads the text aloud exactly as provided.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def speak(self, text: str) -> None:
        client = self._client

        # ── Step 1: Ensure Realtime is connected (reconnect if stale) ────────────
        print("[MORNING][SPEAKER] checking realtime connection", flush=True)
        log_event("morning_routine_speaker_connection_check", {})
        if hasattr(client, "ensure_connected"):
            try:
                await client.ensure_connected()
                print("[MORNING][SPEAKER] realtime ready", flush=True)
            except RuntimeError as exc:
                reason = str(exc)
                print(f"[MORNING][SPEAKER] reconnect failed reason={reason}", flush=True)
                log_event("morning_routine_speaker_call_failed", {"reason": reason})
                raise

        if not client or not getattr(client, "connected", False):
            print("[MORNING][SPEAKER] skipped — client not connected", flush=True)
            log_event("morning_routine_speaker_call_failed", {"reason": "client_not_connected"})
            raise RuntimeError("client_not_connected")

        # ── Step 2: Route audio to the correct local sink ────────────────────────
        ensure_morning_audio_sink()

        # ── Step 3: Send speech and wait for completion ──────────────────────────
        print(f"[MORNING][SPEAKER] speaking length={len(text)}", flush=True)
        log_event("morning_routine_speaker_calling", {"length": len(text)})

        # Register a completion event before sending so no response.done can be missed.
        done_event: asyncio.Event | None = None
        if hasattr(client, "register_response_done_event"):
            done_event = asyncio.Event()
            client.register_response_done_event(done_event)

        try:
            await client.send({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "system",
                    "content": [{"type": "input_text", "text": f"[MORNING_ROUTINE] {text}"}],
                },
            })
            await client.send({
                "type": "response.create",
                "response": {
                    "instructions": f"Say exactly: {text}",
                },
            })
            print("[MORNING][SPEAKER] sent response.create", flush=True)

            if done_event is not None:
                try:
                    await asyncio.wait_for(done_event.wait(), timeout=_SPEAK_TIMEOUT)
                    print("[MORNING][SPEAKER] response.done", flush=True)
                except asyncio.TimeoutError:
                    print(f"[MORNING][SPEAKER] failed reason=speech_timeout (>{_SPEAK_TIMEOUT:.0f}s)", flush=True)
                    log_event("morning_routine_speaker_failure", {"error": "speech_timeout"})
                    raise RuntimeError("speech_timeout")

            print("[MORNING][SPEAKER] success", flush=True)
            log_event("morning_routine_speaker_success", {"length": len(text)})
        except RuntimeError:
            raise
        except Exception as exc:
            print(f"[MORNING][SPEAKER] failed error={exc!r:.100}", flush=True)
            log_event("morning_routine_speaker_failure", {"error": str(exc)[:200]})
            raise


# ── Weather provider ──────────────────────────────────────────────────────────

class MorningWeatherProvider:
    """
    Fetches today's high temperature and condition from wttr.in (no API key).

    Returns {"condition": str, "high": int, "location_label": "South Florida"} or None.
    Synchronous — MorningRoutineService calls it via run_in_executor.
    """

    _WTTR_URL = "https://wttr.in/South+Florida?format=j1"

    def get_today_weather(self) -> dict | None:
        try:
            import requests
            resp = requests.get(self._WTTR_URL, timeout=5)
            if resp.status_code != 200:
                log_event("morning_routine_weather_http_error", {"status": resp.status_code})
                return None
            data = resp.json()
            today = data["weather"][0]
            hourly = today.get("hourly", [])
            # Use midday (index 4) hourly description when available
            if hourly and len(hourly) > 4:
                condition = hourly[4]["weatherDesc"][0]["value"]
            elif hourly:
                condition = hourly[0]["weatherDesc"][0]["value"]
            else:
                condition = (today.get("weatherDesc") or [{}])[0].get("value", "")
            high = int(today.get("maxtempF", 0))
            return {
                "condition": condition,
                "high": high,
                "location_label": "South Florida",
            }
        except Exception as exc:
            log_event("morning_routine_weather_error", {"error": str(exc)[:200]})
            return None


# ── Calendar reader ───────────────────────────────────────────────────────────

class _CalendarEventAdapter:
    """
    Wraps a calendar event dict as an attribute-accessible object.

    morning_routine.py uses getattr(event, "title"), getattr(event, "start_time"),
    and getattr(event, "event_id") — this satisfies those calls without
    modifying the routine module.
    """

    __slots__ = ("title", "start_time", "event_id")

    def __init__(self, raw: dict) -> None:
        self.title = raw.get("title", "") or ""
        self.start_time = raw.get("start_time", "") or ""
        self.event_id = raw.get("event_id")


class MorningCalendarReader:
    """
    Wraps calendar_get_today() for MorningRoutineService.

    calendar_get_today() returns plain dicts; this adapter wraps them as
    _CalendarEventAdapter objects so morning_routine.py's getattr() calls resolve.
    Returns empty list on any failure — routine degrades gracefully.
    """

    def get_today_events(self) -> list:
        try:
            from prometheus.agents.calendar_read_tools import calendar_get_today
            result = calendar_get_today()
            if not result.get("ok"):
                log_event("morning_routine_calendar_error", {
                    "error": str(result.get("error", "unknown"))[:200],
                })
                return []
            return [_CalendarEventAdapter(e) for e in result.get("events", [])]
        except Exception as exc:
            log_event("morning_routine_calendar_error", {"error": str(exc)[:200]})
            return []

"""
prometheus/routines/morning_adapters.py — Live adapters for MorningRoutineService.

Connects MorningRoutineService to the real Prometheus runtime without modifying
the routine logic itself. Each class implements one of the five dependency interfaces
that MorningRoutineService expects.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from utils import log_event


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
        from tools import run_ha_script

        name = entity_id
        if name.startswith("script."):
            name = name[len("script."):]
        result = run_ha_script(name)
        if not result.ok:
            log_event("morning_routine_ha_failure", {
                "entity_id": entity_id,
                "message": (result.message or "")[:200],
            })
        return bool(result.ok)


# ── Speaker ───────────────────────────────────────────────────────────────────

class PrometheusMorningSpeaker:
    """
    Async speaker adapter for MorningRoutineService.

    Replicates the proactive_loop._surface() pattern:
    sends conversation.item.create then response.create so the Realtime
    client reads the text aloud exactly as provided.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def speak(self, text: str) -> None:
        client = self._client
        if not client or not getattr(client, "connected", False):
            log_event("morning_routine_speak_skipped", {"reason": "client_not_connected"})
            return

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
                "modalities": ["audio", "text"],
                "instructions": f"Say exactly: {text}",
            },
        })
        log_event("morning_routine_spoke", {"length": len(text)})


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

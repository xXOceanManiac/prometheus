"""
prometheus/services/hud_state_writer.py

Bridges Prometheus runtime state → Godot HUD state file.

Canonical output path:
  ~/Desktop/PROMETHEUS/state/dashboard_state.json

Three concurrent loops:
  state_sync    — every STATE_SYNC_SECONDS (default 5):  writes all cards using
                  cached articles/events so Godot sees visual-state changes quickly.
  news_fetch    — every NEWS_REFRESH_SECONDS (default 600): fetches Guardian,
                  updates cached articles, triggers a state_sync write.
  cal_fetch     — every CAL_REFRESH_SECONDS (default 900): fetches today's Google
                  Calendar events, updates cached events.

Never blocks startup. Never raises to callers.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from utils import log_event

# ── Canonical state path ─────────────────────────────────────────────────────

_DASHBOARD_STATE_PATH = Path.home() / "Desktop" / "PROMETHEUS" / "state" / "dashboard_state.json"

# Keep the old path in sync too so the readonly dashboard can find one canonical source
_LEGACY_PROMETHEUS_PATH = Path.home() / ".prometheus" / "hud_state.json"

# ── Source paths ─────────────────────────────────────────────────────────────

_VISUAL_STATE_PATH = Path.home() / ".jarvis" / "visual_state.json"
_ACTIVITY_PATH = Path.home() / ".jarvis" / "activity.jsonl"
_TASKS_PATH = Path.home() / ".jarvis" / "background_tasks.json"
_MEMORY_PATH = Path.home() / ".jarvis" / "memory_v2" / "mission_state.json"

# ── Config ───────────────────────────────────────────────────────────────────

_STATE_SYNC_SECONDS = int(os.getenv("PROMETHEUS_STATE_SYNC_SECONDS", "5"))
_NEWS_REFRESH_SECONDS = int(os.getenv("PROMETHEUS_NEWS_REFRESH_SECONDS", "600"))
_CAL_REFRESH_SECONDS = int(os.getenv("PROMETHEUS_CAL_REFRESH_SECONDS", "60"))
_LOG_HEARTBEAT_SECONDS = 60.0

_STATE_MAP = {
    "idle": "idle",
    "armed": "idle",
    "listening": "listening",
    "processing": "processing",
    "speaking": "speaking",
    "background_working": "executing",
    "executing": "executing",
    "error": "warning",
    "offline": "warning",
}

# ── Log throttle state ────────────────────────────────────────────────────────

_LOG_STATE: dict[str, Any] = {"first": True, "last_time": 0.0, "last_key": ""}


def _should_log_write(state: dict) -> bool:
    """Return True when a write-success line should be printed (throttled to ~60s)."""
    now = time.monotonic()
    cards = state.get("cards") or {}
    news = cards.get("news") or {}
    cal = cards.get("calendar") or {}
    key = "|".join([
        str(state.get("state", "")),
        str(news.get("status", "")),
        str(len(news.get("articles") or [])),
        str(cal.get("status", "")),
        str(len(cal.get("events") or [])),
    ])
    if _LOG_STATE["first"]:
        _LOG_STATE["first"] = False
        _LOG_STATE["last_key"] = key
        _LOG_STATE["last_time"] = now
        return True
    if key != _LOG_STATE["last_key"]:
        _LOG_STATE["last_key"] = key
        _LOG_STATE["last_time"] = now
        return True
    if now - _LOG_STATE["last_time"] >= _LOG_HEARTBEAT_SECONDS:
        _LOG_STATE["last_time"] = now
        return True
    return False


# ── Helpers ──────────────────────────────────────────────────────────────────

def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_activity_lines(n: int = 6) -> list[str]:
    lines: list[str] = []
    try:
        if _ACTIVITY_PATH.exists():
            raw = _ACTIVITY_PATH.read_text(encoding="utf-8", errors="ignore")
            for line in reversed(raw.splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    event = str(rec.get("event", "") or rec.get("type", "")).strip()
                    if event and not event.startswith("proactive") and not event.startswith("realtime_event"):
                        ts = str(rec.get("ts", ""))[:16].replace("T", " ").replace("-", "/")
                        lines.append(f"{ts}  {event}")
                        if len(lines) >= n:
                            break
                except Exception:
                    pass
    except Exception:
        pass
    return list(reversed(lines)) if lines else ["HUD bridge active", "Prometheus monitoring"]


# ── Google Calendar color map ─────────────────────────────────────────────────

# Maps Google Calendar colorId (1-11) → hex color string.
# Source: https://developers.google.com/calendar/api/v3/reference/colors
_GOOGLE_COLOR_MAP: dict[str, str] = {
    "1":  "#7986CB",  # Lavender
    "2":  "#33B679",  # Sage
    "3":  "#8E24AA",  # Grape
    "4":  "#E67C73",  # Flamingo
    "5":  "#F6BF26",  # Banana
    "6":  "#F4511E",  # Tangerine
    "7":  "#039BE5",  # Peacock
    "8":  "#616161",  # Graphite
    "9":  "#3F51B5",  # Blueberry
    "10": "#0B8043",  # Basil
    "11": "#D50000",  # Tomato
}

# ── Calendar helpers ──────────────────────────────────────────────────────────

def _time_label_from_iso(iso: str, tz_name: str = "America/New_York") -> str:
    """Convert ISO 8601 datetime string to '7:30 AM' in the configured timezone."""
    if not iso or "T" not in iso:
        return ""
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, KeyError):
            tz = ZoneInfo("America/New_York")
        dt_str = iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("America/New_York"))
        local = dt.astimezone(tz)
        h, m = local.hour, local.minute
        am_pm = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return f"{h12}:{m:02d} {am_pm}"
    except Exception:
        return ""


def _is_happening_now(start_iso: str, end_iso: str) -> bool:
    if not start_iso or not end_iso:
        return False
    try:
        now = datetime.now(timezone.utc)
        start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return start <= now <= end
    except Exception:
        return False


def _is_future(start_iso: str) -> bool:
    if not start_iso or "T" not in start_iso:
        return False
    try:
        now = datetime.now(timezone.utc)
        start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        return start > now
    except Exception:
        return False


# ── Calendar card builder ─────────────────────────────────────────────────────

def _calendar_card_payload(
    events: list[dict],
    date_str: str,
    status: str,
    error_msg: str = "",
    tz_name: str = "America/New_York",
) -> dict:
    """Build the cards.calendar payload from a list of raw event dicts."""
    chip = {"live": "LIVE", "pending": "PENDING", "error": "ERR"}.get(status, "PENDING")

    if status != "live":
        return {
            "title": "Today",
            "chip": chip,
            "status": status,
            "date": date_str,
            "summary": error_msg or "Calendar source pending",
            "events": [],
            "items": [],
        }

    hud_events: list[dict] = []
    items: list[str] = []
    first_future_marked = False

    for ev in events:
        if not isinstance(ev, dict):
            continue
        start = str(ev.get("start_time") or "")
        end = str(ev.get("end_time") or "")
        title = str(ev.get("title") or "Event")
        is_all_day = bool(ev.get("is_all_day", False))
        location = str(ev.get("location") or "")

        time_label = "All Day" if is_all_day else _time_label_from_iso(start, tz_name)

        is_now = False if is_all_day else _is_happening_now(start, end)
        is_next = False
        if not is_all_day and not is_now and not first_future_marked:
            if _is_future(start):
                is_next = True
                first_future_marked = True

        color_id = str(ev.get("color_id") or "")
        color_hex = _GOOGLE_COLOR_MAP.get(color_id, "")

        hud_events.append({
            "title": title,
            "start_time": start,
            "end_time": end,
            "time_label": time_label,
            "location": location,
            "source": "Google Calendar",
            "is_now": is_now,
            "is_next": is_next,
            "color_id": color_id,
            "color_hex": color_hex,
            "accent_color": color_hex,
        })

        label = f"{time_label}  {title}" if time_label and time_label != "All Day" else title
        items.append(label)

    n = len(hud_events)
    return {
        "title": "Today",
        "chip": "LIVE",
        "status": "live",
        "date": date_str,
        "summary": f"{n} event{'s' if n != 1 else ''} today",
        "events": hud_events,
        "items": items,
    }


# ── Calendar fetch ─────────────────────────────────────────────────────────────

def _fetch_calendar_today() -> tuple[list[dict], str, str]:
    """
    Fetch today's Google Calendar events.
    Returns (events, status, date_str). Never raises.
    """
    today_str = date.today().isoformat()
    try:
        log_event("hud_state_calendar_fetch_start", {})
        print("[HUD_WRITER] hud_state_calendar_fetch_start", flush=True)
        from prometheus.agents.calendar_read_tools import calendar_get_today
        result = calendar_get_today()
        if not result.get("ok"):
            err = str(result.get("error", "unknown error"))
            if "disabled" in err.lower():
                return [], "pending", today_str
            print(f"[HUD_WRITER] hud_state_calendar_fetch_failed reason={err[:160]}", flush=True)
            log_event("hud_state_calendar_fetch_failed", {"reason": err[:200]})
            return [], "error", today_str
        events = result.get("events") or []
        date_str = result.get("date") or today_str
        n = len(events)
        print(f"[HUD_WRITER] hud_state_calendar_fetch_done count={n}", flush=True)
        log_event("hud_state_calendar_fetch_done", {"count": n})
        return events, "live", date_str
    except Exception as exc:
        err_str = str(exc)[:200]
        print(f"[HUD_WRITER] hud_state_calendar_fetch_failed reason={err_str}", flush=True)
        log_event("hud_state_calendar_fetch_failed", {"reason": err_str})
        return [], "error", today_str


# ── Card builders ─────────────────────────────────────────────────────────────

def _news_card_payload(articles: list[dict], status: str) -> dict:
    chip_map = {"live": "LIVE", "fallback": "DEMO", "demo": "DEMO", "error": "ERR", "loading": "WAIT"}
    chip = chip_map.get(status, "DEMO")
    items = []
    for a in articles[:3]:
        tag = str(a.get("section") or a.get("tag") or "News")
        title = str(a.get("title") or "")
        if len(title) > 60:
            title = title[:57] + "…"
        time_ago = str(a.get("time_ago") or "")
        items.append({"label": f"[{tag}] {title}", "value": time_ago})
    return {
        "title": "Relevant News",
        "chip": chip,
        "summary": "The Guardian · Prometheus curated",
        "status": status,
        "updated_at": _iso_now(),
        "articles": articles,
        "items": items,
    }


# ── Full state builder ────────────────────────────────────────────────────────

def build_hud_state(
    articles: list[dict],
    news_status: str,
    cal_events: list[dict] | None = None,
    cal_status: str = "pending",
    cal_date: str = "",
) -> dict:
    """Build complete hud state dict. Pure — reads source files, returns dict."""
    vs = _read_json(_VISUAL_STATE_PATH, {})
    mission = _read_json(_MEMORY_PATH, {})
    activity_lines = _read_activity_lines()

    raw_state = str(vs.get("state", "idle")).lower()
    godot_state = _STATE_MAP.get(raw_state, "idle")

    active_project = str(
        vs.get("active_project") or vs.get("active_project_name") or "Prometheus_Main"
    ).strip()

    active_window_raw = vs.get("active_window") or {}
    active_window_title = (
        str(active_window_raw.get("title", ""))
        if isinstance(active_window_raw, dict)
        else str(active_window_raw)
    ).strip()

    mission_title = str(mission.get("current_mission") or mission.get("mission") or "").strip()
    mission_goal = str(mission.get("active_goal") or mission.get("goal") or "").strip()
    mission_next = str(mission.get("next_action") or "").strip()

    tasks_raw = _read_json(_TASKS_PATH, [])
    tasks_list = (
        tasks_raw.get("tasks", []) if isinstance(tasks_raw, dict)
        else (tasks_raw if isinstance(tasks_raw, list) else [])
    )
    task_items = [
        str(t.get("description", "Task") or "Task")[:60]
        for t in tasks_list[-5:] if isinstance(t, dict)
    ] or ["No active tasks"]

    # ── Calendar card ─────────────────────────────────────────────────────────
    if cal_events is None:
        cal_events = []

    # Determine timezone from Google Calendar config if possible
    tz_name = "America/New_York"
    try:
        from prometheus.integrations.google_calendar import load_google_calendar_config
        cfg = load_google_calendar_config()
        tz_name = cfg.timezone or tz_name
    except Exception:
        pass

    cal_payload = _calendar_card_payload(cal_events, cal_date, cal_status, tz_name=tz_name)

    return {
        "state": godot_state,
        "mode": "idle",
        "focus_card": "focus",
        "active_project": active_project,
        "updated_at": _iso_now(),
        "cards": {
            "news": _news_card_payload(articles, news_status),
            "calendar": cal_payload,
            # Godot maps "focus" card → zone_calendar → _render_calendar_content
            "focus": cal_payload,
            "brand": {
                "title": "Prometheus",
                "chip": godot_state.upper(),
                "summary": "Prometheus is active and monitoring.",
                "items": [
                    f"Project: {active_project}",
                    f"Window: {active_window_title}" if active_window_title else "No active window",
                    "Calendar triggers armed",
                ],
            },
            "activity": {
                "title": "Activity",
                "chip": "LIVE",
                "summary": "Recent Prometheus events.",
                "items": activity_lines[:6],
            },
            "tasks": {
                "title": "Tasks",
                "chip": "QUEUE",
                "summary": f"{len(tasks_list)} background task(s).",
                "items": task_items,
            },
            "objective": {
                "title": "Mission",
                "chip": "CONTEXT",
                "summary": mission_title or "No active mission.",
                "items": [
                    f"Goal: {mission_goal}" if mission_goal else "No active goal",
                    f"Next: {mission_next}" if mission_next else "Awaiting next action",
                ],
            },
        },
    }


# ── File writer ───────────────────────────────────────────────────────────────

def write_dashboard_state(
    articles: list[dict],
    news_status: str,
    cal_events: list[dict] | None = None,
    cal_status: str = "pending",
    cal_date: str = "",
) -> None:
    """
    Build and atomically write dashboard_state.json to the canonical path.
    Also writes the legacy .prometheus path for backward compatibility.
    Logs on first write, state/news/calendar changes, and at most once per 60s.
    """
    try:
        state = build_hud_state(articles, news_status, cal_events, cal_status, cal_date)
        news_count = len(articles)

        # ── canonical path ────────────────────────────────────────────────────
        _DASHBOARD_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _DASHBOARD_STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _DASHBOARD_STATE_PATH)

        if _should_log_write(state):
            cal_count = len(cal_events or [])
            print(
                f"[HUD_WRITER] write_success path={_DASHBOARD_STATE_PATH} "
                f"news={news_count} news_status={news_status} "
                f"cal={cal_count} cal_status={cal_status} state={state['state']}",
                flush=True,
            )
            log_event("hud_state_write_success", {
                "path": str(_DASHBOARD_STATE_PATH),
                "news_count": news_count,
                "news_status": news_status,
                "cal_count": cal_count,
                "cal_status": cal_status,
                "state": state["state"],
            })

        # ── legacy path ───────────────────────────────────────────────────────
        try:
            _LEGACY_PROMETHEUS_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp2 = _LEGACY_PROMETHEUS_PATH.with_suffix(".tmp")
            tmp2.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp2, _LEGACY_PROMETHEUS_PATH)
        except Exception:
            pass  # legacy write failure is non-fatal

    except Exception as exc:
        print(f"[HUD_WRITER] write_failed error={exc!r:.120}", flush=True)
        log_event("hud_state_write_failed", {"error": str(exc)[:200]})


# ── Guardian news fetch ───────────────────────────────────────────────────────

def _fetch_news() -> tuple[list[dict], str]:
    """Fetch Guardian news. Returns (articles, status). Never raises."""
    try:
        from prometheus.services.guardian_news import get_news
        return get_news()
    except Exception as exc:
        log_event("guardian_news_error", {"error": str(exc)[:200]})
        print(f"[HUD_WRITER] guardian_fetch_error={exc!r:.80}", flush=True)
        try:
            from prometheus.services.guardian_news import _fallback_articles
            return _fallback_articles(), "fallback"
        except Exception:
            return [], "error"


# ── HudStateWriter ────────────────────────────────────────────────────────────

class HudStateWriter:
    """
    Maintains a live dashboard_state.json for the Godot HUD.

    Three concurrent loops:
      _news_loop()      — every NEWS_REFRESH_SECONDS: fetches Guardian, caches articles.
      _calendar_loop()  — every CAL_REFRESH_SECONDS: fetches today's calendar events.
      _state_loop()     — every STATE_SYNC_SECONDS: writes state with cached data.

    Usage:
        writer = HudStateWriter()
        asyncio.create_task(writer.run())
    """

    def __init__(self) -> None:
        self._stopped = False
        self._articles: list[dict] = []
        self._news_status: str = "loading"
        self._cal_events: list[dict] = []
        self._cal_status: str = "pending"
        self._cal_date: str = date.today().isoformat()

    def stop(self) -> None:
        self._stopped = True

    async def run(self) -> None:
        loop = asyncio.get_running_loop()

        print(
            f"[HUD_WRITER] started path={_DASHBOARD_STATE_PATH} "
            f"state_sync={_STATE_SYNC_SECONDS}s news_refresh={_NEWS_REFRESH_SECONDS}s "
            f"cal_refresh={_CAL_REFRESH_SECONDS}s",
            flush=True,
        )
        log_event("hud_state_writer_started", {
            "canonical_path": str(_DASHBOARD_STATE_PATH),
            "state_sync_seconds": _STATE_SYNC_SECONDS,
            "news_refresh_seconds": _NEWS_REFRESH_SECONDS,
            "cal_refresh_seconds": _CAL_REFRESH_SECONDS,
        })

        # Write an initial "loading" state immediately so Godot sees something
        await loop.run_in_executor(None, lambda: write_dashboard_state([], "loading"))

        await asyncio.gather(
            self._news_loop(loop),
            self._calendar_loop(loop),
            self._state_loop(loop),
        )

    async def _news_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Fetch Guardian news every NEWS_REFRESH_SECONDS."""
        while not self._stopped:
            try:
                print("[HUD_WRITER] guardian_fetch_start", flush=True)
                articles, status = await loop.run_in_executor(None, _fetch_news)
                self._articles = articles
                self._news_status = status
                print(
                    f"[HUD_WRITER] guardian_fetch_done count={len(articles)} status={status}",
                    flush=True,
                )
                if status == "live":
                    log_event("guardian_news_live", {"count": len(articles)})
                elif status == "fallback":
                    log_event("guardian_news_fallback", {"count": len(articles)})
            except Exception as exc:
                print(f"[HUD_WRITER] news_loop_error={exc!r:.100}", flush=True)
                log_event("hud_state_news_loop_error", {"error": str(exc)[:200]})

            await asyncio.sleep(_NEWS_REFRESH_SECONDS)
            if self._stopped:
                break

    async def _calendar_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Fetch today's calendar events every CAL_REFRESH_SECONDS."""
        while not self._stopped:
            try:
                events, status, date_str = await loop.run_in_executor(
                    None, _fetch_calendar_today
                )
                self._cal_events = events
                self._cal_status = status
                self._cal_date = date_str
            except Exception as exc:
                print(f"[HUD_WRITER] calendar_loop_error={exc!r:.100}", flush=True)
                log_event("hud_state_calendar_loop_error", {"error": str(exc)[:200]})

            await asyncio.sleep(_CAL_REFRESH_SECONDS)
            if self._stopped:
                break

    async def _state_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Write full state every STATE_SYNC_SECONDS using cached articles and events."""
        while not self._stopped:
            try:
                a = self._articles
                s = self._news_status
                ce = self._cal_events
                cs = self._cal_status
                cd = self._cal_date
                await loop.run_in_executor(
                    None,
                    lambda articles=a, status=s, cal_events=ce, cal_status=cs, cal_date=cd:
                        write_dashboard_state(articles, status, cal_events, cal_status, cal_date),
                )
            except Exception as exc:
                print(f"[HUD_WRITER] state_loop_error={exc!r:.100}", flush=True)
                log_event("hud_state_state_loop_error", {"error": str(exc)[:200]})

            await asyncio.sleep(_STATE_SYNC_SECONDS)
            if self._stopped:
                break

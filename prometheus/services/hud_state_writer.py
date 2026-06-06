"""
prometheus/services/hud_state_writer.py

Bridges Prometheus runtime state → Godot HUD state file (~/.prometheus/hud_state.json).

The Godot HUD polls hud_state.json every 250ms and renders cards from it.
This writer is the single source of truth for what the Godot HUD displays.

Schedule: initial write at startup, then every NEWS_REFRESH_SECONDS (default 600).
Never blocks startup. Never raises to callers.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils import log_event

_HUD_STATE_PATH = Path.home() / ".prometheus" / "hud_state.json"
_VISUAL_STATE_PATH = Path.home() / ".jarvis" / "visual_state.json"
_ACTIVITY_GLOB = Path.home() / ".jarvis" / "activity.jsonl"
_TASKS_PATH = Path.home() / ".jarvis" / "background_tasks.json"
_MEMORY_PATH = Path.home() / ".jarvis" / "memory_v2" / "mission_state.json"
_NEWS_REFRESH_SECONDS = int(os.getenv("PROMETHEUS_NEWS_REFRESH_SECONDS", "600"))

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
        if _ACTIVITY_GLOB.exists():
            raw = _ACTIVITY_GLOB.read_text(encoding="utf-8", errors="ignore")
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


def _news_card_payload(articles: list[dict], status: str) -> dict:
    chip_map = {"live": "LIVE", "fallback": "DEMO", "demo": "DEMO", "error": "ERR", "loading": "…"}
    chip = chip_map.get(status, "DEMO")

    # Items for the fallback render path (first 3 articles as {label, value} rows)
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


def _fetch_news() -> tuple[list[dict], str]:
    """Fetch Guardian news. Returns (articles, status). Never raises."""
    try:
        from prometheus.services.guardian_news import get_news
        return get_news()
    except Exception as exc:
        log_event("guardian_news_error", {"error": str(exc)[:200]})
        try:
            from prometheus.services.guardian_news import _fallback_articles
            return _fallback_articles(), "fallback"
        except Exception:
            return [], "error"


def build_hud_state(articles: list[dict], news_status: str) -> dict:
    """
    Build the full hud_state dict from current runtime files.
    Pure function — safe to call from any context.
    """
    vs = _read_json(_VISUAL_STATE_PATH, {})
    mission = _read_json(_MEMORY_PATH, {})
    activity_lines = _read_activity_lines()

    raw_state = str(vs.get("state", "idle")).lower()
    godot_state = _STATE_MAP.get(raw_state, "idle")

    active_project = str(
        vs.get("active_project")
        or vs.get("active_project_name")
        or "Prometheus_Main"
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
    if isinstance(tasks_raw, dict):
        tasks_list = tasks_raw.get("tasks", [])
    else:
        tasks_list = tasks_raw if isinstance(tasks_raw, list) else []
    task_items = [
        str(t.get("description", "Task") or "Task")[:60]
        for t in tasks_list[-5:]
        if isinstance(t, dict)
    ] or ["No active tasks"]

    return {
        "state": godot_state,
        "mode": "idle",
        "focus_card": "focus",
        "active_project": active_project,
        "updated_at": _iso_now(),
        "cards": {
            "news": _news_card_payload(articles, news_status),
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


def write_hud_state(articles: list[dict], news_status: str) -> None:
    """Build and atomically write hud_state.json. Never raises."""
    try:
        state = build_hud_state(articles, news_status)
        _HUD_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _HUD_STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _HUD_STATE_PATH)
    except Exception as exc:
        log_event("hud_state_write_error", {"error": str(exc)[:200]})


class HudStateWriter:
    """
    Periodically fetches Guardian news and writes the full Godot HUD state.

    Usage:
        writer = HudStateWriter()
        asyncio.create_task(writer.run())
    """

    def __init__(self) -> None:
        self._stopped = False
        self._articles: list[dict] = []
        self._news_status: str = "loading"

    def stop(self) -> None:
        self._stopped = True

    async def run(self) -> None:
        """Main loop: fetch news immediately, then refresh every NEWS_REFRESH_SECONDS."""
        loop = asyncio.get_running_loop()

        # Write a "loading" state immediately so Godot sees something on startup
        await loop.run_in_executor(
            None, lambda: write_hud_state([], "loading")
        )

        while not self._stopped:
            try:
                articles, status = await loop.run_in_executor(None, _fetch_news)
                self._articles = articles
                self._news_status = status
                await loop.run_in_executor(
                    None, lambda a=articles, s=status: write_hud_state(a, s)
                )
                if status == "live":
                    log_event("guardian_news_live", {"count": len(articles)})
                elif status == "fallback":
                    log_event("guardian_news_fallback", {"count": len(articles)})
            except Exception as exc:
                log_event("hud_state_writer_error", {"error": str(exc)[:200]})

            # Also update the non-news cards on every cycle (picks up visual_state changes)
            await asyncio.sleep(_NEWS_REFRESH_SECONDS)

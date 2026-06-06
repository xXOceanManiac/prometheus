"""
prometheus/services/hud_state_writer.py

Bridges Prometheus runtime state → Godot HUD state file.

Canonical output path:
  ~/Desktop/PROMETHEUS/state/dashboard_state.json

Two concurrent loops:
  state_sync  — every STATE_SYNC_SECONDS (default 5):  writes all cards using
                cached articles so Godot sees visual-state changes quickly.
  news_fetch  — every NEWS_REFRESH_SECONDS (default 600): fetches Guardian,
                updates cached articles, triggers a state_sync write.

Never blocks startup. Never raises to callers.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
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

def build_hud_state(articles: list[dict], news_status: str) -> dict:
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


# ── File writer ───────────────────────────────────────────────────────────────

def write_dashboard_state(articles: list[dict], news_status: str) -> None:
    """
    Build and atomically write dashboard_state.json to the canonical path.
    Also writes the legacy .prometheus path for backward compatibility.
    Prints to stdout (journal-visible) on success and failure.
    """
    try:
        state = build_hud_state(articles, news_status)
        news_count = len(articles)

        # ── canonical path ────────────────────────────────────────────────────
        _DASHBOARD_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _DASHBOARD_STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _DASHBOARD_STATE_PATH)

        print(
            f"[HUD_WRITER] write_success path={_DASHBOARD_STATE_PATH} "
            f"news={news_count} status={news_status} state={state['state']}",
            flush=True,
        )
        log_event("hud_state_write_success", {
            "path": str(_DASHBOARD_STATE_PATH),
            "news_count": news_count,
            "news_status": news_status,
            "state": state["state"],
        })

        # ── legacy path (for any existing readers of ~/.prometheus/hud_state.json) ──
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

    Two concurrent loops:
      _news_loop()   — every NEWS_REFRESH_SECONDS: fetches Guardian, caches articles.
      _state_loop()  — every STATE_SYNC_SECONDS: writes state with cached articles.

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
        loop = asyncio.get_running_loop()

        print(
            f"[HUD_WRITER] started path={_DASHBOARD_STATE_PATH} "
            f"state_sync={_STATE_SYNC_SECONDS}s news_refresh={_NEWS_REFRESH_SECONDS}s",
            flush=True,
        )
        log_event("hud_state_writer_started", {
            "canonical_path": str(_DASHBOARD_STATE_PATH),
            "state_sync_seconds": _STATE_SYNC_SECONDS,
            "news_refresh_seconds": _NEWS_REFRESH_SECONDS,
        })

        # Write an initial "loading" state immediately so Godot sees something
        await loop.run_in_executor(None, lambda: write_dashboard_state([], "loading"))

        # Start both loops concurrently
        await asyncio.gather(
            self._news_loop(loop),
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

    async def _state_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Write full state every STATE_SYNC_SECONDS using cached articles."""
        while not self._stopped:
            try:
                a = self._articles
                s = self._news_status
                await loop.run_in_executor(
                    None, lambda articles=a, status=s: write_dashboard_state(articles, status)
                )
            except Exception as exc:
                print(f"[HUD_WRITER] state_loop_error={exc!r:.100}", flush=True)
                log_event("hud_state_state_loop_error", {"error": str(exc)[:200]})

            await asyncio.sleep(_STATE_SYNC_SECONDS)
            if self._stopped:
                break

"""
world_model.py — Live world snapshot for Prometheus contextual inference.

build_world_snapshot() assembles a bounded, safe snapshot of current system state.
Fast (< 200ms), safe, never crashes, never dumps private file contents.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

_JARVIS = Path.home() / ".jarvis"
_STATE_FILE    = _JARVIS / "visual_state.json"
_ACTIVITY_FILE = _JARVIS / "activity.jsonl"
_MISSION_FILE  = _JARVIS / "memory_v2" / "mission_state.json"
_TASKS_FILE    = _JARVIS / "background_tasks.json"

# Dev server process patterns (name fragments)
_DEV_SERVER_PROCS = ("node", "python", "uvicorn", "gunicorn", "webpack", "vite",
                      "next", "gatsby", "rails", "django", "flask", "fastapi",
                      "cargo", "go run", "bun", "deno")

# Error-class activity kinds
_ERROR_KINDS = frozenset({"tool_error", "executor_step_failed", "verifier_fail",
                           "planner_llm_error", "realtime_receiver_error",
                           "chat_completion_anthropic_failed", "voice_error"})


def build_world_snapshot() -> dict[str, Any]:
    """
    Build a live snapshot of the current world state.
    Pulls from: mission state, workspace, activity log, git, process list,
    and live sensor caches (window, clipboard, filesystem, error, process).
    All fields have safe defaults. Never raises.
    """
    snap: dict[str, Any] = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        # Mission layer
        "current_mission": "",
        "active_goal": "",
        "subtasks": [],
        "blockers": [],
        "next_action": "",
        # Activity
        "recent_activity": [],
        "recent_errors": [],
        # Workspace (overridden by window sensor if running)
        "active_window_title": "",
        "active_app": "",
        "current_workspace": "",
        "focused_project": "",
        "focused_project_path": "",
        # Terminal
        "terminal_cwd": "",
        # Screen
        "visible_screen_summary": "",
        # Files (from git + filesystem sensor)
        "recent_files_changed": [],
        "recent_file_changes": [],      # live from filesystem sensor
        # Clipboard / selection
        "selected_text": "",            # live from clipboard sensor
        # Git
        "git_branch": "",
        "git_status_short": "",
        "git_has_changes": False,
        # Processes (from ps aux + process sensor registry)
        "running_dev_servers": [],
        "running_dev_processes": [],    # live from process sensor
        # Future sensors
        "calendar_now_context": None,
        "home_assistant_state": None,
    }

    _fill_mission(snap)
    _fill_workspace(snap)
    _fill_activity(snap)
    _fill_git(snap)
    _fill_processes(snap)
    _fill_sensors(snap)   # live sensor caches — always last, may override workspace fields
    return snap


# ── Sensors ───────────────────────────────────────────────────────────────────

def _fill_sensors(snap: dict[str, Any]) -> None:
    """
    Merge live sensor caches into snapshot. All imports are lazy and guarded.
    Sensors may not be running — missing caches return safe defaults silently.
    This function must remain synchronous and fast (reads from in-memory dicts).
    """
    # Window sensor — override workspace fields if sensor has fresher data
    try:
        from prometheus.sensors.window_sensor import get_cache as _wc
        wc = _wc()
        if wc.get("window_title"):
            snap["active_window_title"] = wc["window_title"]
            snap["active_app"] = _extract_app_from_window(wc["window_title"])
    except Exception:
        pass

    # Clipboard sensor — selected text from PRIMARY X selection
    try:
        from prometheus.sensors.clipboard_sensor import get_cache as _cc
        cc = _cc()
        snap["selected_text"] = cc.get("selected_text", "")
    except Exception:
        pass

    # Filesystem sensor — last N file change events
    try:
        from prometheus.sensors.filesystem_sensor import get_cache as _fc
        changes = _fc()
        snap["recent_file_changes"] = [
            {
                "filename": c.get("filename", ""),
                "change_type": c.get("change_type", ""),
                "project": c.get("project", ""),
                "timestamp": c.get("timestamp", ""),
            }
            for c in changes[-5:]
        ]
    except Exception:
        pass

    # Error sensor — live errors preferred over activity.jsonl parsing
    try:
        from prometheus.sensors.error_sensor import get_cache as _ec
        live_errors = _ec()
        if live_errors:
            snap["recent_errors"] = [
                {
                    "ts": e.get("timestamp", ""),
                    "kind": "live_error",
                    "description": e.get("raw_line", "")[:120],
                }
                for e in live_errors[-5:]
            ]
    except Exception:
        pass

    # Process sensor — running dev server registry
    try:
        from prometheus.sensors.process_sensor import get_cache as _pc
        snap["running_dev_processes"] = _pc()[:5]
    except Exception:
        pass


# ── Mission ───────────────────────────────────────────────────────────────────

def _fill_mission(snap: dict[str, Any]) -> None:
    try:
        if not _MISSION_FILE.exists():
            return
        data = json.loads(_MISSION_FILE.read_text(encoding="utf-8"))
        snap["current_mission"] = str(data.get("current_mission", ""))
        snap["active_goal"] = str(data.get("active_goal", ""))
        snap["next_action"] = str(data.get("next_action", ""))
        snap["subtasks"] = [
            {"id": t.get("id"), "description": str(t.get("description", ""))[:100]}
            for t in data.get("subtasks", [])[:10]
        ]
        snap["blockers"] = [str(b.get("description", ""))[:120] for b in data.get("blocked_items", [])[:5]]
    except Exception:
        pass


# ── Workspace ─────────────────────────────────────────────────────────────────

def _fill_workspace(snap: dict[str, Any]) -> None:
    try:
        if not _STATE_FILE.exists():
            return
        data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        snap["active_window_title"] = str(data.get("active_window", ""))[:200]
        snap["active_app"] = _extract_app_from_window(snap["active_window_title"])
        snap["current_workspace"] = str(data.get("state", ""))
        snap["focused_project"] = str(data.get("active_project", ""))
        snap["focused_project_path"] = str(data.get("active_project_path", ""))
        snap["visible_screen_summary"] = str(data.get("screen_summary", ""))[:400]
        snap["terminal_cwd"] = str(data.get("terminal_cwd", ""))
    except Exception:
        pass


def _extract_app_from_window(window_title: str) -> str:
    if not window_title:
        return ""
    t = window_title.lower()
    for kw in ("code", "vs code", "vscode"):
        if kw in t:
            return "vscode"
    for kw in ("firefox", "chrome", "chromium", "brave", "safari"):
        if kw in t:
            return kw
    for kw in ("terminal", "konsole", "bash", "zsh", "alacritty", "kitty"):
        if kw in t:
            return "terminal"
    for kw in ("obsidian",):
        if kw in t:
            return "obsidian"
    # First word before dash/hyphen is usually the app name
    return window_title.split("—")[0].split("-")[0].strip()[:30]


# ── Activity ──────────────────────────────────────────────────────────────────

def _fill_activity(snap: dict[str, Any]) -> None:
    try:
        if not _ACTIVITY_FILE.exists():
            return
        lines = _ACTIVITY_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
        entries: list[dict] = []
        errors: list[dict] = []
        for raw in reversed(lines[-50:]):
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
                kind = entry.get("kind", "")
                ts = str(entry.get("ts", ""))[:19]
                body = (
                    entry.get("transcript")
                    or entry.get("description")
                    or entry.get("message")
                    or entry.get("mission")
                    or entry.get("error")
                    or ""
                )
                if kind in _ERROR_KINDS or "error" in kind.lower():
                    if len(errors) < 5:
                        errors.append({
                            "ts": ts,
                            "kind": kind,
                            "description": str(body)[:120],
                        })
                if len(entries) < 5:
                    entries.append(f"[{ts}] {kind}" + (f" — {str(body)[:80]}" if body else ""))
            except Exception:
                pass
        snap["recent_activity"] = list(reversed(entries))
        snap["recent_errors"] = list(reversed(errors))
    except Exception:
        pass


# ── Git ───────────────────────────────────────────────────────────────────────

def _fill_git(snap: dict[str, Any]) -> None:
    project_path = snap.get("focused_project_path", "").strip()
    if not project_path or not Path(project_path).is_dir():
        return
    try:
        r = subprocess.run(
            ["git", "-C", project_path, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0:
            snap["git_branch"] = r.stdout.strip()
    except Exception:
        pass

    try:
        r = subprocess.run(
            ["git", "-C", project_path, "status", "--short"],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0:
            short = r.stdout.strip()
            snap["git_status_short"] = short[:200]
            snap["git_has_changes"] = bool(short)
            # Extract changed filenames (first 8)
            changed = [line[3:].strip() for line in short.splitlines() if line.strip()][:8]
            snap["recent_files_changed"] = changed
    except Exception:
        pass


# ── Processes ─────────────────────────────────────────────────────────────────

def _fill_processes(snap: dict[str, Any]) -> None:
    try:
        r = subprocess.run(
            ["ps", "aux", "--no-headers"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode != 0:
            return
        servers: list[str] = []
        seen: set[str] = set()
        for line in r.stdout.splitlines():
            parts = line.split(None, 10)
            if len(parts) < 11:
                continue
            cmd = parts[10].lower()
            for pattern in _DEV_SERVER_PROCS:
                if pattern in cmd and pattern not in seen:
                    # Extract just the command, not full arg string
                    short = " ".join(parts[10].split()[:4])[:60]
                    servers.append(short)
                    seen.add(pattern)
                    break
            if len(servers) >= 5:
                break
        snap["running_dev_servers"] = servers
    except Exception:
        pass

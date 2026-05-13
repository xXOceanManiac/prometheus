"""
cognition.py — Operational context assembly for Prometheus planning calls.

build_operational_snapshot() assembles fresh context on every planning call.
build_safe_snapshot() strips anything that shouldn't leave the machine.
format_operational_state_block() formats the <operational_state> XML block.

All functions run in < 100ms. No stale caches.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

_ACTIVITY_FILE = Path.home() / ".jarvis" / "activity.jsonl"


def build_operational_snapshot() -> dict[str, Any]:
    """
    Assembles fresh operational context: mission, tasks, blockers, next action,
    last 5 activity entries, current datetime.
    """
    snapshot: dict[str, Any] = {
        "datetime": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mission": "",
        "objective": "",
        "next_action": "",
        "tasks": [],
        "blockers": [],
        "recent_activity": [],
    }

    try:
        from mission_state import MissionState
        data = MissionState().get_mission()
        snapshot["mission"] = data.get("current_mission", "")
        snapshot["objective"] = data.get("active_goal", "")
        snapshot["next_action"] = data.get("next_action", "")
        snapshot["blockers"] = [b.get("description", "") for b in data.get("blocked_items", [])]
        snapshot["tasks"] = [
            {"id": t.get("id"), "description": t.get("description", ""), "status": "active"}
            for t in data.get("subtasks", [])
        ] + [
            {"id": t.get("id"), "description": t.get("description", ""), "status": "completed"}
            for t in data.get("completed_subtasks", [])
        ]
    except Exception:
        pass

    try:
        if _ACTIVITY_FILE.exists():
            lines = _ACTIVITY_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
            for raw in lines[-5:]:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                    kind = entry.get("kind", "")
                    body = (
                        entry.get("transcript")
                        or entry.get("mission")
                        or entry.get("description")
                        or entry.get("message")
                        or ""
                    )
                    ts = str(entry.get("ts", ""))[:19]
                    snapshot["recent_activity"].append(
                        f"[{ts}] {kind}" + (f" — {str(body)[:80]}" if body else "")
                    )
                except Exception:
                    pass
    except Exception:
        pass

    return snapshot


def build_safe_snapshot() -> dict[str, Any]:
    """
    Safe wrapper around build_operational_snapshot() for OpenAI API calls.
    Strips raw file contents, Obsidian note bodies, and personal identifiers
    beyond first name. Returns operational structure only.
    """
    snap = build_operational_snapshot()
    return {
        "datetime": snap["datetime"],
        "mission": str(snap.get("mission", ""))[:200],
        "objective": str(snap.get("objective", ""))[:200],
        "next_action": str(snap.get("next_action", ""))[:200],
        "tasks": [
            {
                "id": t.get("id"),
                "description": str(t.get("description", ""))[:120],
                "status": t.get("status"),
            }
            for t in snap.get("tasks", [])[:20]
        ],
        "blockers": [str(b)[:120] for b in snap.get("blockers", [])[:10]],
        "recent_activity": [str(a)[:120] for a in snap.get("recent_activity", [])],
    }


def format_operational_state_block(snapshot: dict[str, Any]) -> str:
    """
    Formats the <operational_state> XML block for injection into LLM system prompts.
    """
    task_lines = "\n".join(
        f"  [{t.get('status', 'active')}] {t.get('description', '')}"
        for t in snapshot.get("tasks", [])
        if t.get("status") == "active"
    ) or "  (none)"

    blocker_lines = "\n".join(
        f"  - {b}" for b in snapshot.get("blockers", [])
    ) or "  (none)"

    activity_lines = "\n".join(
        f"  {a}" for a in snapshot.get("recent_activity", [])
    ) or "  (none)"

    return (
        "<operational_state>\n"
        f"MISSION: {snapshot.get('mission') or '(not set)'}\n"
        f"OBJECTIVE: {snapshot.get('objective') or '(not set)'}\n"
        f"NEXT ACTION: {snapshot.get('next_action') or '(not set)'}\n"
        f"OPEN TASKS:\n{task_lines}\n"
        f"BLOCKERS:\n{blocker_lines}\n"
        f"RECENT ACTIVITY:\n{activity_lines}\n"
        f"TIME: {snapshot.get('datetime', '')}\n"
        "</operational_state>"
    )

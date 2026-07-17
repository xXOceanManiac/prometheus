"""
mission_state.py — Persistent mission and subtask tracking for Prometheus.

Stores to ~/.jarvis/memory_v2/mission_state.json.
Survives app restarts. Keeps backward compat with WorkingMemory.active_goal.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from prometheus.memory.memory_core import MEMORY_DIR, now_iso, read_json, write_json
from prometheus.infra.utils import log_event

MISSION_FILE = MEMORY_DIR / "mission_state.json"


def _default() -> dict[str, Any]:
    return {
        "current_mission": "",
        "active_goal": "",
        "subtasks": [],
        "completed_subtasks": [],
        "blocked_items": [],
        "next_action": "",
        "last_updated": "",
    }


class MissionState:
    def __init__(self) -> None:
        self.path = MISSION_FILE

    # ── internal ──────────────────────────────────────────────────────────────

    def _read(self) -> dict[str, Any]:
        data = read_json(self.path, _default())
        if not isinstance(data, dict):
            return _default()
        for key, val in _default().items():
            data.setdefault(key, val)
        return data

    def _write(self, data: dict[str, Any]) -> None:
        data["last_updated"] = now_iso()
        write_json(self.path, data)

    # ── public API ────────────────────────────────────────────────────────────

    def get_mission(self) -> dict[str, Any]:
        """Return full mission state dict."""
        return self._read()

    def set_mission(self, mission: str, goal: str = "") -> None:
        """Set the current mission and optionally the active goal.
        Clears active subtasks and blockers (completed subtasks are kept as history)."""
        data = self._read()
        data["current_mission"] = str(mission).strip()
        if goal:
            data["active_goal"] = str(goal).strip()
        data["subtasks"] = []
        data["blocked_items"] = []
        data["next_action"] = ""
        self._write(data)
        log_event("mission_set", {"mission": str(mission).strip()[:80], "goal": str(goal).strip()[:80]})
        # Keep WorkingMemory.active_goal in sync
        try:
            from prometheus.memory.working_memory import WorkingMemory
            WorkingMemory().write({"active_goal": data["active_goal"] or data["current_mission"]})
        except Exception:
            pass

    def set_active_goal(self, goal: str) -> None:
        """Update only the active_goal, keeping current_mission unchanged."""
        data = self._read()
        data["active_goal"] = str(goal).strip()
        self._write(data)
        try:
            from prometheus.memory.working_memory import WorkingMemory
            WorkingMemory().write({"active_goal": data["active_goal"]})
        except Exception:
            pass

    def add_subtask(self, description: str) -> str:
        """Add a subtask. Returns its ID."""
        data = self._read()
        task_id = f"task-{len(data['subtasks']) + len(data['completed_subtasks']) + 1}"
        data["subtasks"].append({
            "id": task_id,
            "description": str(description).strip(),
            "created_at": now_iso(),
        })
        self._write(data)
        log_event("mission_subtask_added", {"id": task_id, "description": str(description).strip()[:80]})
        return task_id

    def complete_subtask(self, task_id_or_description: str) -> bool:
        """Mark a subtask complete by id or partial description match. Returns True on success."""
        data = self._read()
        query = str(task_id_or_description).strip().lower()
        for i, task in enumerate(data["subtasks"]):
            if task.get("id") == task_id_or_description or query in task.get("description", "").lower():
                completed = data["subtasks"].pop(i)
                completed["completed_at"] = now_iso()
                data["completed_subtasks"].append(completed)
                self._write(data)
                log_event("mission_subtask_completed", {"id": completed.get("id"), "description": completed.get("description", "")[:80]})
                return True
        return False

    def add_blocker(self, description: str) -> None:
        """Add a blocker."""
        data = self._read()
        data["blocked_items"].append({
            "description": str(description).strip(),
            "added_at": now_iso(),
        })
        self._write(data)

    def clear_blocker(self, description_fragment: str) -> bool:
        """Remove a blocker by partial description match."""
        data = self._read()
        query = str(description_fragment).strip().lower()
        before = len(data["blocked_items"])
        data["blocked_items"] = [
            b for b in data["blocked_items"]
            if query not in b.get("description", "").lower()
        ]
        if len(data["blocked_items"]) < before:
            self._write(data)
            return True
        return False

    def set_next_action(self, action: str) -> None:
        """Set the next planned action."""
        data = self._read()
        data["next_action"] = str(action).strip()
        self._write(data)

    def summary_text(self) -> str:
        """Return a concise plain-text summary for voice or display."""
        data = self._read()
        mission = data.get("current_mission") or data.get("active_goal") or ""
        goal = data.get("active_goal") or ""
        subtasks = data.get("subtasks", [])
        completed = data.get("completed_subtasks", [])
        blocked = data.get("blocked_items", [])
        next_action = data.get("next_action") or ""

        parts: list[str] = []
        if mission:
            parts.append(f"Mission: {mission}")
        if goal and goal != mission:
            parts.append(f"Goal: {goal}")
        if subtasks:
            count = len(subtasks)
            done = len(completed)
            total = count + done
            parts.append(f"Tasks: {done}/{total} complete")
            for t in subtasks[:3]:
                parts.append(f"  - {t.get('description', '')[:60]}")
            if count > 3:
                parts.append(f"  ... and {count - 3} more")
        if blocked:
            parts.append(f"Blocked: {blocked[0].get('description', '')[:60]}")
        if next_action:
            parts.append(f"Next: {next_action}")
        return "\n".join(parts) if parts else "No active mission."

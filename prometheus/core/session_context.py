"""
session_context.py — Session instruction builders extracted from realtime_client.py.

Standalone functions with no dependency on RealtimePrometheusClient state.
Called by RealtimePrometheusClient._build_instructions() and _build_live_state_block().
"""
from __future__ import annotations


def build_instructions(
    system_prompt: str,
    workspace_context: str,
    vault_context: str,
) -> str:
    """
    Combine system prompt with vault/workspace/working-memory context.
    Always reads fresh working memory from disk so mid-session tool use is reflected.
    """
    parts = [system_prompt]
    if workspace_context:
        parts.append(workspace_context)
    if vault_context:
        parts.append(vault_context)
    try:
        from prometheus.memory.working_memory import WorkingMemory
        wm = WorkingMemory().read()
        wm_lines: list[str] = []
        last_req = str(wm.get("last_user_request") or "").strip()
        last_tool = str(wm.get("last_tool_action") or "").strip()
        active_goal = str(wm.get("active_goal") or "").strip()
        if last_req:
            wm_lines.append(f"Last request: {last_req[:200]}")
        if last_tool:
            wm_lines.append(f"Last tool: {last_tool}")
        if active_goal:
            wm_lines.append(f"Active goal: {active_goal[:200]}")
        if wm_lines:
            parts.append("--- WORKING MEMORY ---\n" + "\n".join(wm_lines))
    except Exception:
        pass
    return "\n\n".join(parts)


def build_live_state_block() -> str:
    """Build a compact live world state block for injection before each LLM response."""
    try:
        from prometheus.context.world_model import build_world_snapshot
        snap = build_world_snapshot()

        # Always inject fresh local time — never rely on stale session instructions
        _time_str = ""
        try:
            from datetime import datetime as _dt
            from zoneinfo import ZoneInfo
            from prometheus.infra.config import CONFIG
            _tz_name = str(CONFIG.get("timezone") or "America/New_York")
            try:
                _tz = ZoneInfo(_tz_name)
            except Exception:
                _tz = ZoneInfo("America/New_York")
            _now = _dt.now(_tz)
            _time_str = _now.strftime("%I:%M %p %Z, %A %B %-d %Y").lstrip("0")
        except Exception:
            pass

        lines = [f"[LIVE STATE — {snap.get('timestamp', '')}]"]
        if _time_str:
            lines.append(f"Current time: {_time_str}")

        win = snap.get("active_window_title", "")
        app = snap.get("active_app", "")
        if win or app:
            lines.append(f"window: {win or 'unknown'}" + (f" ({app})" if app else ""))

        mission = snap.get("current_mission", "")
        if mission:
            lines.append(f"mission: {mission[:80]}")

        goal = snap.get("active_goal", "")
        if goal:
            lines.append(f"goal: {goal[:80]}")

        nxt = snap.get("next_action", "")
        if nxt:
            lines.append(f"next: {nxt[:80]}")

        branch = snap.get("git_branch", "")
        git_status = snap.get("git_status_short", "")
        if branch:
            git_line = f"git: {branch}"
            if git_status:
                changed_count = len([ln for ln in git_status.splitlines() if ln.strip()])
                git_line += f" — {changed_count} changed file{'s' if changed_count != 1 else ''}"
            lines.append(git_line)

        errors = snap.get("recent_errors", [])
        if errors:
            desc = str(errors[-1].get("description", ""))[:100]
            if desc:
                lines.append(f"errors: {desc}")

        selected = snap.get("selected_text", "")
        if selected and selected.strip():
            lines.append(f"selected: {selected.strip()[:200]}")

        procs = snap.get("running_dev_processes", [])
        if procs:
            proc_names = ", ".join(p.get("name", "") for p in procs[:3] if p.get("name"))
            if proc_names:
                lines.append(f"processes: {proc_names}")

        return "\n".join(lines)
    except Exception:
        return ""

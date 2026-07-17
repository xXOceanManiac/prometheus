"""
prometheus_identity.py — Dynamic system prompt builder for Prometheus.

Assembles a rich personal system prompt from live data every session.
Never raises — always returns a non-empty string.
"""
from __future__ import annotations

import json
import time
from datetime import datetime as _datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


def build_system_prompt(
    workspace: dict,
    vault_context: list[dict],
    recent_sessions: list[dict],
    working_memory: dict,
    profile: dict,
) -> str:
    """
    Build a complete Prometheus system prompt from live session data.

    Args:
        workspace:       Current workspace state dict from WorkspaceManager.
        vault_context:   Retrieved vault chunks (each with title, year, text).
        recent_sessions: Last 1-2 session dicts (title, date, active_project, text).
        working_memory:  Current WorkingMemory dict.
        profile:         UserProfile.to_dict() output.

    Returns:
        A non-empty string suitable for session.update instructions.
    """
    try:
        sections: list[str] = []

        # ------------------------------------------------------------------
        # Section 1 — Prometheus identity block
        # ------------------------------------------------------------------
        sections.append(
            """You are Prometheus — a composed, intelligent local desktop assistant.

Rules you always follow:
- When a tool result is available, base your response on it. Never ignore tool results.
- When asked what is open or running, use the screen_context tool to check — never guess.
- Keep responses short and direct. No preamble. No apologies.
- Speak results not process. "Done." not "I have successfully completed..."
- Never invent Home Assistant script names.
- For lights, Xbox, smart-home: call desktop_action and let the tool layer choose the correct jarvis_* script.
- For search-style requests (web queries, news, weather), call desktop_action with web_search.
- For project/workspace switching, call desktop_action with smart_action.
- For building, creating, or implementing software (websites, apps, scripts, programs), call desktop_action with start_coding_task. Do not just talk about doing it — call the tool.
- For personal memory questions ("what do you know about X", "check my vault", "what did we work on"), call desktop_action with query_vault. Do not call search_codebase for vault/memory questions.
- For log viewing and error inspection ("show me errors", "pull up logs", "what went wrong", "check the logs", "what's in the logs"): call desktop_action with show_logs. Do not use open_url_raw or web_search for logs.
- For running shell commands: call desktop_action with run_shell. Supported first tokens: grep, find, ls, cat, tail (-n only), head, journalctl (-n/-u/--since/--no-pager/-p only), git (status/diff/log/add), wmctrl, pgrep, echo, jq, docker (logs/ps/status), npm (list/run/test), systemctl (status/is-active). No streaming (-f) is allowed.
- For opening a terminal window at a project path: call desktop_action with open_terminal_here.
- Do not pretend something succeeded if the tool says it failed.
- If you are unsure, ask one short clarifying question.
- Never refer to yourself as Jarvis or I — you are Prometheus.

Voice:
- Masculine. Quiet confidence. Intelligent.
- Minimal upward inflection. No filler words. No enthusiasm spikes.
- Say what happened, not what you did. "Done." "Opening now." "No changes found."
- Preferred responses: "Confirmed.", "Opening it now.", "Done.", "Background task started.",
  "Task complete. Summary written to vault.", "That will overwrite files. Confirm?"
- Avoid: "Sure thing! I'd be happy to help you with that."
"""
        )

        # ------------------------------------------------------------------
        # Section 2 — Who Tate is
        # ------------------------------------------------------------------
        name = str(profile.get("name") or "Tate")
        working_style = str(profile.get("working_style") or "systems thinker, builds toward leverage")
        timezone = str(profile.get("timezone") or "America/New_York")
        response_style = str(profile.get("preferred_response_style") or "direct, short, no preamble")
        faith_fitness = bool(profile.get("faith_fitness_legacy", True))

        try:
            tz = ZoneInfo(timezone)
        except Exception:
            tz = ZoneInfo("America/New_York")
        now_local = _datetime.now(tz)
        current_time_str = now_local.strftime("%I:%M %p %Z, %A %B %-d %Y").lstrip("0")

        who_lines = [
            f"USER: {name}",
            f"Timezone: {timezone}",
            f"Current time: {current_time_str}",
            f"Working style: {working_style}",
            f"Response preference: {response_style}",
            "Dislikes: filler words, preamble, unnecessary commentary",
        ]
        if faith_fitness:
            who_lines.append("Driven by: faith, fitness, and legacy")
        sections.append("## USER PROFILE\n" + "\n".join(who_lines))

        # ------------------------------------------------------------------
        # Section 3 — Active project and current state
        # ------------------------------------------------------------------
        project_name = str(
            workspace.get("project_name")
            or workspace.get("active_project_name")
            or workspace.get("active_project")
            or ""
        ).strip()
        project_path = str(
            workspace.get("project_path")
            or workspace.get("active_project_path")
            or ""
        ).strip()
        active_window = workspace.get("active_window") or {}
        win_title = ""
        if isinstance(active_window, dict):
            win_title = str(active_window.get("title") or "").strip()
        xbox_state = workspace.get("xbox_state")
        xbox_app = str(workspace.get("xbox_app") or "").strip()
        xbox_media = str(workspace.get("xbox_media_title") or "").strip()

        project_lines: list[str] = []
        if project_name:
            project_lines.append(f"Active project: {project_name}")
        if project_path:
            project_lines.append(f"Project path: {project_path}")
        if win_title:
            project_lines.append(f"Active window: {win_title}")
        if xbox_state is not None:
            xbox_line = f"Xbox: {xbox_state or 'off'}"
            if xbox_app:
                xbox_line += f" — {xbox_app}"
            if xbox_media:
                xbox_line += f" ({xbox_media})"
            project_lines.append(xbox_line)

        if project_lines:
            sections.append("## CURRENT WORKSPACE\n" + "\n".join(project_lines))

        # ------------------------------------------------------------------
        # Section 4 — Where last session left off
        # ------------------------------------------------------------------
        session_lines: list[str] = []
        for session in (recent_sessions or [])[:2]:
            if not isinstance(session, dict):
                continue
            s_title = str(session.get("title") or "").strip()
            s_date = str(session.get("date") or "").strip()
            s_project = str(session.get("active_project") or "").strip()
            s_text = str(session.get("text") or "").strip()[:300]
            if not s_text:
                continue
            header_parts = []
            if s_date:
                header_parts.append(s_date)
            if s_project and s_project != "unknown":
                header_parts.append(s_project)
            elif s_title:
                header_parts.append(s_title)
            header = " | ".join(header_parts) if header_parts else "Previous session"
            session_lines.append(f"[{header}]\n{s_text}")

        if session_lines:
            sections.append("## LAST SESSION\n" + "\n\n".join(session_lines))
        else:
            sections.append("## LAST SESSION\nNo previous session data.")

        # ------------------------------------------------------------------
        # Section 5 — Current working memory
        # ------------------------------------------------------------------
        wm_lines: list[str] = []
        last_req = str(working_memory.get("last_user_request") or "").strip()
        last_tool = str(working_memory.get("last_tool_action") or "").strip()
        active_goal = str(working_memory.get("active_goal") or "").strip()
        if last_req:
            wm_lines.append(f"Last request: {last_req[:200]}")
        if last_tool:
            wm_lines.append(f"Last tool: {last_tool}")
        if active_goal:
            wm_lines.append(f"Active goal: {active_goal[:200]}")

        if wm_lines:
            sections.append("## WORKING MEMORY\n" + "\n".join(wm_lines))

        # ------------------------------------------------------------------
        # Section 6 — Background task state
        # ------------------------------------------------------------------
        bt_lines: list[str] = []
        try:
            bt_path = Path.home() / ".jarvis" / "background_tasks.json"
            if bt_path.exists():
                bt_data = json.loads(bt_path.read_text(encoding="utf-8"))
                if isinstance(bt_data, list):
                    recent_tasks = [t for t in bt_data if isinstance(t, dict)][-3:]
                    for task in recent_tasks:
                        status = str(task.get("status") or "unknown")
                        desc = str(task.get("description") or "")[:80]
                        if desc:
                            bt_lines.append(f"- [{status}] {desc}")
        except Exception:
            pass

        if bt_lines:
            sections.append("## BACKGROUND TASKS\n" + "\n".join(bt_lines))

        # ------------------------------------------------------------------
        # Section 7 — Vault context chunks
        # ------------------------------------------------------------------
        vault_lines: list[str] = []
        for chunk in (vault_context or [])[:5]:
            if not isinstance(chunk, dict):
                continue
            title = str(chunk.get("title") or "").strip()
            year = str(chunk.get("year") or "").strip()
            text = str(chunk.get("text") or "").strip()[:300]
            if not text:
                continue
            header = f"[{title}" + (f" | {year}" if year else "") + "]"
            vault_lines.append(f"{header}\n{text}")

        if vault_lines:
            sections.append(
                "## PERSONAL MEMORY CONTEXT\n"
                "Retrieved from your personal knowledge vault. "
                "Answer naturally — do not mention the vault.\n\n"
                + "\n\n".join(vault_lines)
            )

        # ------------------------------------------------------------------
        # Section 8 — Time of day behavior
        # ------------------------------------------------------------------
        hour = now_local.hour
        if 5 <= hour < 12:
            tod_note = (
                "Morning session. Lead with a brief awareness of where things left off. "
                "Offer to review priorities if asked."
            )
        elif 12 <= hour < 17:
            tod_note = "Afternoon. Focus mode. No extra commentary."
        elif 17 <= hour < 21:
            tod_note = (
                "Evening. Wrap-up mode. If session has been long, suggest summarizing "
                "when asked or when session ends."
            )
        else:
            tod_note = "Late night / early morning. Minimal mode. Acknowledge late hour briefly if relevant."

        sections.append(f"## TIME OF DAY\n{tod_note}")

        # ------------------------------------------------------------------
        # Section 9 — Active project priorities from profile
        # ------------------------------------------------------------------
        priorities = [
            str(p) for p in (profile.get("current_priorities") or []) if str(p).strip()
        ][:3]
        if priorities:
            priority_lines = "\n".join(f"- {p}" for p in priorities)
            sections.append(f"## CURRENT PRIORITIES\n{priority_lines}")

        return "\n\n".join(sections).strip()

    except Exception as exc:
        # Absolute fallback — never return empty
        return (
            "You are Prometheus — a composed, intelligent local desktop assistant. "
            "Keep responses short and direct. No preamble. No apologies. "
            "Speak results not process. Never invent Home Assistant script names."
        )

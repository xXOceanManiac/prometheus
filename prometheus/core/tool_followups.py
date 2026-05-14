"""
tool_followups.py — Tool followup routing extracted from realtime_client.py.

FOLLOWUP_ACTIONS is the canonical set of actions that require a spoken LLM response
after tool execution. Imported by both _run_direct_tool() and _handle_tool_call().
"""
from __future__ import annotations

FOLLOWUP_ACTIONS: frozenset[str] = frozenset({
    "list_windows",
    "get_active_window",
    "desktop_state",
    "tell_time",
    "resume_last_context",
    "summarize_screen",
    "web_search",
    "smart_action",
    "background_task",
    "screen_context",
    "search_codebase",
    "git_status",
    "git_diff",
    "git_commit",
    "run_python",
    "run_shell",
    "session_wrapup",
    "system_status",
    "get_priorities",
    "start_coding_task",
    "get_coding_status",
    "run_diagnostics",
    "get_mission_status",
    "read_file",
    "show_logs",
    "list_files",
    "query_vault",
    "get_build_status",
    # Calendar reads — always need spoken synthesis after execution
    "calendar_list_upcoming",
    "calendar_get_today",
    "calendar_get_tomorrow",
    "calendar_get_date",
    "calendar_next_event",
    "calendar_summarize_day",
    "calendar_find_free_blocks",
    # Calendar write execution — always produce spoken result
    "calendar_list_reviewed_requests",
    "calendar_approve_request",
    "calendar_execute_approved_request",
})

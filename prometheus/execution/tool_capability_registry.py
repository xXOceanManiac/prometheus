"""
Tool Capability Registry — metadata describing what each tool can and cannot do.

Used by WorkflowSelector to choose the right tools for a workflow and by the
verification layer to know what successful execution looks like.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass(frozen=True)
class ToolCapability:
    tool_name: str
    description: str
    examples: List[str]
    required_slots: List[str]
    optional_slots: List[str]
    safe_when: List[str]
    bad_for: List[str]
    risk: str  # "none" | "low" | "medium" | "high"
    validates: str  # what a successful result looks like
    supports_verification: bool
    workflow_tags: List[str] = field(default_factory=list)


TOOL_CAPABILITIES: dict[str, ToolCapability] = {

    # ── App control ──────────────────────────────────────────────────────────

    "open_app": ToolCapability(
        tool_name="open_app",
        description="Launch a desktop application by name",
        examples=["open firefox", "open vs code", "open terminal"],
        required_slots=["app"],
        optional_slots=[],
        safe_when=["app is a known desktop application", "app is not already running"],
        bad_for=["opening URLs", "opening files", "opening code folders"],
        risk="low",
        validates="app process is running and window is visible",
        supports_verification=True,
        workflow_tags=["workspace_setup", "resume_mission", "open_active_project"],
    ),

    "close_app": ToolCapability(
        tool_name="close_app",
        description="Close a running desktop application",
        examples=["close spotify", "close firefox"],
        required_slots=["app"],
        optional_slots=[],
        safe_when=["app has no unsaved work", "user explicitly requested it"],
        bad_for=["closing apps with unsaved changes without confirmation"],
        risk="medium",
        validates="app process is no longer running",
        supports_verification=True,
        workflow_tags=["workspace_cleanup"],
    ),

    "open_url_key": ToolCapability(
        tool_name="open_url_key",
        description="Open a named URL from the config (e.g. 'github', 'docs')",
        examples=["open github", "open docs"],
        required_slots=["key"],
        optional_slots=[],
        safe_when=["key is in config url map"],
        bad_for=["arbitrary URLs not in config"],
        risk="none",
        validates="browser opens with the mapped URL",
        supports_verification=False,
        workflow_tags=["workspace_setup"],
    ),

    "open_url_keys": ToolCapability(
        tool_name="open_url_keys",
        description="Open multiple named URLs from config at once",
        examples=["open github and docs"],
        required_slots=["keys"],
        optional_slots=[],
        safe_when=["all keys are in config url map"],
        bad_for=["arbitrary URLs"],
        risk="none",
        validates="browser opens with all mapped URLs",
        supports_verification=False,
        workflow_tags=["workspace_setup"],
    ),

    "open_url_raw": ToolCapability(
        tool_name="open_url_raw",
        description="Open an arbitrary URL in the browser",
        examples=["open https://example.com"],
        required_slots=["url"],
        optional_slots=[],
        safe_when=["URL is http/https"],
        bad_for=["local file paths", "intranet URLs without confirmation"],
        risk="low",
        validates="browser opens to specified URL",
        supports_verification=False,
        workflow_tags=["web_research"],
    ),

    "open_code_folder": ToolCapability(
        tool_name="open_code_folder",
        description="Open a project directory in VS Code",
        examples=["open Prometheus in VS Code", "open ~/projects/foo in code"],
        required_slots=["project_path"],
        optional_slots=[],
        safe_when=["path is a valid project directory"],
        bad_for=["system directories", "non-code directories"],
        risk="none",
        validates="VS Code opens with the specified folder",
        supports_verification=True,
        workflow_tags=["open_active_project", "resume_mission", "workspace_setup"],
    ),

    "open_terminal_here": ToolCapability(
        tool_name="open_terminal_here",
        description="Open a terminal in a specified directory",
        examples=["open terminal in Prometheus", "open terminal here"],
        required_slots=[],
        optional_slots=["path"],
        safe_when=["path is a valid directory"],
        bad_for=["system directories without explicit request"],
        risk="none",
        validates="terminal opens at the specified path",
        supports_verification=False,
        workflow_tags=["workspace_setup", "open_active_project"],
    ),

    # ── Smart / ambient actions ──────────────────────────────────────────────

    "smart_action": ToolCapability(
        tool_name="smart_action",
        description="Context-inferred action from vague user intent and active window",
        examples=["do something useful", "what should I do next"],
        required_slots=["intent"],
        optional_slots=["app", "context"],
        safe_when=["intent is clearly non-destructive", "active context is known"],
        bad_for=["destructive operations", "multi-step workflows requiring planning"],
        risk="low",
        validates="action was appropriate for the active context",
        supports_verification=False,
        workflow_tags=["continue_next_action"],
    ),

    "summarize_screen": ToolCapability(
        tool_name="summarize_screen",
        description="Take a screenshot and describe what is visible on screen",
        examples=["what's on my screen", "summarize what I'm looking at"],
        required_slots=[],
        optional_slots=[],
        safe_when=["always safe"],
        bad_for=["actions requiring interaction with screen content"],
        risk="none",
        validates="returns text description of screen contents",
        supports_verification=True,
        workflow_tags=["diagnose_blocker", "summarize_current_project", "check_if_it_worked"],
    ),

    "screen_context": ToolCapability(
        tool_name="screen_context",
        description="Get context about the active window and what the user is working on",
        examples=["what are we working on", "what's in my current window"],
        required_slots=[],
        optional_slots=[],
        safe_when=["always safe"],
        bad_for=["tasks requiring full project context"],
        risk="none",
        validates="returns active window title and inferred context",
        supports_verification=False,
        workflow_tags=["resume_mission", "summarize_current_project"],
    ),

    "save_context": ToolCapability(
        tool_name="save_context",
        description="Save the current session context to working memory",
        examples=["save what we're doing", "remember this context"],
        required_slots=[],
        optional_slots=["label"],
        safe_when=["always safe"],
        bad_for=["nothing"],
        risk="none",
        validates="context written to working memory",
        supports_verification=False,
        workflow_tags=["resume_mission"],
    ),

    "resume_last_context": ToolCapability(
        tool_name="resume_last_context",
        description="Load the last saved session context from working memory",
        examples=["resume what we were doing", "what were we working on"],
        required_slots=[],
        optional_slots=[],
        safe_when=["always safe — read-only"],
        bad_for=["fresh sessions with no prior context"],
        risk="none",
        validates="returns last saved mission/project context",
        supports_verification=False,
        workflow_tags=["resume_mission"],
    ),

    "run_routine": ToolCapability(
        tool_name="run_routine",
        description="Execute a saved named routine (sequence of steps)",
        examples=["run morning routine", "run standup routine"],
        required_slots=["routine_name"],
        optional_slots=[],
        safe_when=["routine is well-defined and non-destructive"],
        bad_for=["one-off tasks without a saved routine"],
        risk="low",
        validates="all routine steps completed successfully",
        supports_verification=True,
        workflow_tags=["prepare_current_workspace"],
    ),

    "save_routine": ToolCapability(
        tool_name="save_routine",
        description="Save a sequence of steps as a named routine",
        examples=["save this as my morning routine"],
        required_slots=["routine_name", "steps"],
        optional_slots=[],
        safe_when=["routine name is valid"],
        bad_for=["nothing"],
        risk="none",
        validates="routine saved to memory",
        supports_verification=False,
        workflow_tags=[],
    ),

    "backfill_memory": ToolCapability(
        tool_name="backfill_memory",
        description="Backfill session summaries into long-term memory",
        examples=["backfill memory", "process my recent sessions"],
        required_slots=[],
        optional_slots=[],
        safe_when=["always safe"],
        bad_for=["real-time lookups"],
        risk="none",
        validates="memory updated with processed sessions",
        supports_verification=False,
        workflow_tags=[],
    ),

    "run_dream_pass": ToolCapability(
        tool_name="run_dream_pass",
        description="Run offline memory reorganization and pattern extraction",
        examples=["run dream pass", "reorganize memory"],
        required_slots=[],
        optional_slots=[],
        safe_when=["system is idle"],
        bad_for=["real-time sessions"],
        risk="none",
        validates="dream pass completed, patterns extracted",
        supports_verification=False,
        workflow_tags=[],
    ),

    # ── Home Assistant ────────────────────────────────────────────────────────

    "run_ha_script": ToolCapability(
        tool_name="run_ha_script",
        description="Execute a Home Assistant script by name",
        examples=["turn on the lights", "movie mode", "good night"],
        required_slots=["script_name"],
        optional_slots=[],
        safe_when=["script is in HARDCODED_HA_SCRIPTS", "reversible script"],
        bad_for=["irreversible home automation changes without confirmation"],
        risk="low",
        validates="HA script executed successfully (HTTP 200)",
        supports_verification=True,
        workflow_tags=["prepare_current_workspace"],
    ),

    # ── Window / desktop state ────────────────────────────────────────────────

    "list_windows": ToolCapability(
        tool_name="list_windows",
        description="List all open application windows",
        examples=["what windows are open", "list my open apps"],
        required_slots=[],
        optional_slots=[],
        safe_when=["always safe — read-only"],
        bad_for=["nothing"],
        risk="none",
        validates="returns list of open windows with titles",
        supports_verification=False,
        workflow_tags=["diagnose_blocker", "check_if_it_worked"],
    ),

    "get_active_window": ToolCapability(
        tool_name="get_active_window",
        description="Get the title and details of the currently focused window",
        examples=["what am I focused on", "what's my active window"],
        required_slots=[],
        optional_slots=[],
        safe_when=["always safe — read-only"],
        bad_for=["nothing"],
        risk="none",
        validates="returns active window title and process",
        supports_verification=False,
        workflow_tags=["resume_mission", "diagnose_blocker"],
    ),

    "desktop_state": ToolCapability(
        tool_name="desktop_state",
        description="Get a snapshot of the full desktop state: windows, project, workspace",
        examples=["what's my current workspace", "what's the desktop state"],
        required_slots=[],
        optional_slots=[],
        safe_when=["always safe — read-only"],
        bad_for=["nothing"],
        risk="none",
        validates="returns structured workspace snapshot",
        supports_verification=False,
        workflow_tags=["resume_mission", "prepare_current_workspace"],
    ),

    # ── Filesystem ────────────────────────────────────────────────────────────

    "list_files": ToolCapability(
        tool_name="list_files",
        description="List files in a directory",
        examples=["list files in Prometheus", "what's in ~/projects"],
        required_slots=["path"],
        optional_slots=[],
        safe_when=["always safe — read-only"],
        bad_for=["reading file contents"],
        risk="none",
        validates="returns file listing for the path",
        supports_verification=True,
        workflow_tags=["summarize_current_project", "inspect_recent_changes", "diagnose_blocker"],
    ),

    "read_file": ToolCapability(
        tool_name="read_file",
        description="Read the contents of a file",
        examples=["read README.md", "show me the config file"],
        required_slots=["path"],
        optional_slots=[],
        safe_when=["always safe — read-only"],
        bad_for=["binary files", "huge files (>500KB)"],
        risk="none",
        validates="returns file content as text",
        supports_verification=True,
        workflow_tags=["summarize_current_project", "diagnose_blocker", "inspect_recent_changes"],
    ),

    "write_file": ToolCapability(
        tool_name="write_file",
        description="Write content to a file in the workspace",
        examples=["write this to foo.py", "save the summary to notes.md"],
        required_slots=["path", "content"],
        optional_slots=[],
        safe_when=["path is inside runtime/workspace/"],
        bad_for=["paths outside workspace", "overwriting important files without confirmation"],
        risk="medium",
        validates="file exists at path with expected content",
        supports_verification=True,
        workflow_tags=["ship_current_project", "summarize_current_project"],
    ),

    # ── System / media ────────────────────────────────────────────────────────

    "mode_lock_in": ToolCapability(
        tool_name="mode_lock_in",
        description="Switch Prometheus to a named focus mode (e.g., work, dev, focus)",
        examples=["lock in", "focus mode", "work mode"],
        required_slots=["mode"],
        optional_slots=[],
        safe_when=["mode is defined in config"],
        bad_for=["unknown modes"],
        risk="none",
        validates="mode active in visual state",
        supports_verification=False,
        workflow_tags=["prepare_current_workspace"],
    ),

    "volume_change": ToolCapability(
        tool_name="volume_change",
        description="Change system volume by a relative amount",
        examples=["volume up", "turn it down a bit"],
        required_slots=["delta"],
        optional_slots=[],
        safe_when=["always safe"],
        bad_for=["nothing"],
        risk="none",
        validates="volume changed by delta",
        supports_verification=False,
        workflow_tags=[],
    ),

    "volume_set": ToolCapability(
        tool_name="volume_set",
        description="Set system volume to a specific level (0-100)",
        examples=["set volume to 40", "volume at 60 percent"],
        required_slots=["level"],
        optional_slots=[],
        safe_when=["level is 0-100"],
        bad_for=["nothing"],
        risk="none",
        validates="volume is at specified level",
        supports_verification=False,
        workflow_tags=[],
    ),

    "mute_toggle": ToolCapability(
        tool_name="mute_toggle",
        description="Toggle system audio mute on/off",
        examples=["mute", "unmute", "toggle mute"],
        required_slots=[],
        optional_slots=[],
        safe_when=["always safe"],
        bad_for=["nothing"],
        risk="none",
        validates="mute state toggled",
        supports_verification=False,
        workflow_tags=[],
    ),

    "screenshot": ToolCapability(
        tool_name="screenshot",
        description="Take a screenshot and save it",
        examples=["take a screenshot", "screenshot"],
        required_slots=[],
        optional_slots=["path"],
        safe_when=["always safe"],
        bad_for=["nothing"],
        risk="none",
        validates="screenshot file created",
        supports_verification=True,
        workflow_tags=["check_if_it_worked", "diagnose_blocker"],
    ),

    "tell_time": ToolCapability(
        tool_name="tell_time",
        description="Report the current time",
        examples=["what time is it", "what's the time"],
        required_slots=[],
        optional_slots=[],
        safe_when=["always safe"],
        bad_for=["nothing"],
        risk="none",
        validates="returns current time string",
        supports_verification=True,
        workflow_tags=[],
    ),

    "projector_on": ToolCapability(
        tool_name="projector_on",
        description="Turn on the connected projector",
        examples=["projector on", "turn on projector"],
        required_slots=[],
        optional_slots=[],
        safe_when=["projector is connected"],
        bad_for=["nothing"],
        risk="none",
        validates="projector active",
        supports_verification=False,
        workflow_tags=[],
    ),

    "projector_off": ToolCapability(
        tool_name="projector_off",
        description="Turn off the connected projector",
        examples=["projector off", "turn off projector"],
        required_slots=[],
        optional_slots=[],
        safe_when=["always safe"],
        bad_for=["nothing"],
        risk="none",
        validates="projector off",
        supports_verification=False,
        workflow_tags=[],
    ),

    "sleep": ToolCapability(
        tool_name="sleep",
        description="Put the machine to sleep",
        examples=["sleep", "put computer to sleep"],
        required_slots=[],
        optional_slots=[],
        safe_when=["user explicitly requested it", "no unsaved work"],
        bad_for=["during active background tasks"],
        risk="high",
        validates="system enters sleep state",
        supports_verification=False,
        workflow_tags=[],
    ),

    "restart": ToolCapability(
        tool_name="restart",
        description="Restart the machine",
        examples=["restart", "reboot"],
        required_slots=[],
        optional_slots=[],
        safe_when=["user explicitly confirmed", "no active tasks"],
        bad_for=["during background task execution"],
        risk="high",
        validates="system restarts",
        supports_verification=False,
        workflow_tags=[],
    ),

    "shutdown": ToolCapability(
        tool_name="shutdown",
        description="Shut down the machine",
        examples=["shut down", "power off"],
        required_slots=[],
        optional_slots=[],
        safe_when=["user explicitly confirmed", "no active tasks"],
        bad_for=["during background task execution"],
        risk="high",
        validates="system powers off",
        supports_verification=False,
        workflow_tags=[],
    ),

    # ── Confirmation flow ────────────────────────────────────────────────────

    "confirm_pending": ToolCapability(
        tool_name="confirm_pending",
        description="Confirm a pending dangerous action",
        examples=["yes", "confirmed", "go ahead"],
        required_slots=[],
        optional_slots=["action_id"],
        safe_when=["a confirmation-required action is pending"],
        bad_for=["nothing"],
        risk="none",
        validates="pending action executed",
        supports_verification=False,
        workflow_tags=[],
    ),

    "cancel_pending": ToolCapability(
        tool_name="cancel_pending",
        description="Cancel a pending dangerous action",
        examples=["cancel", "never mind", "stop"],
        required_slots=[],
        optional_slots=["action_id"],
        safe_when=["a confirmation-required action is pending"],
        bad_for=["nothing"],
        risk="none",
        validates="pending action cancelled",
        supports_verification=False,
        workflow_tags=[],
    ),

    # ── Background tasks ────────────────────────────────────────────────────

    "background_task": ToolCapability(
        tool_name="background_task",
        description="Submit a task to run in the background worker pool",
        examples=["run this in the background", "process that while I work"],
        required_slots=["description"],
        optional_slots=["context"],
        safe_when=["task is non-destructive", "resources are available"],
        bad_for=["tasks needing immediate interactive output", "destructive operations"],
        risk="low",
        validates="task submitted to worker pool with task_id",
        supports_verification=True,
        workflow_tags=["ship_current_project"],
    ),

    # ── Code execution ────────────────────────────────────────────────────────

    "run_python": ToolCapability(
        tool_name="run_python",
        description="Execute a Python snippet or file in a sandboxed environment",
        examples=["run this python", "execute the script"],
        required_slots=["command"],
        optional_slots=["project_path"],
        safe_when=["code does not contain os.remove, shutil.rmtree, subprocess, eval, exec"],
        bad_for=["destructive file operations", "network requests without explicit intent"],
        risk="medium",
        validates="Python exits 0, stdout captured",
        supports_verification=True,
        workflow_tags=["debug_current_error", "ship_current_project"],
    ),

    "run_shell": ToolCapability(
        tool_name="run_shell",
        description="Execute a whitelisted shell command",
        examples=["run ls -la", "check disk space with df -h"],
        required_slots=["command"],
        optional_slots=[],
        safe_when=["first token is in whitelist (grep, find, ls, cat, python3, pip, git, etc.)"],
        bad_for=["rm, mv, cp without explicit request", "commands not in whitelist"],
        risk="medium",
        validates="command exits 0, stdout captured",
        supports_verification=True,
        workflow_tags=["debug_current_error", "inspect_recent_changes", "diagnose_blocker", "ship_current_project"],
    ),

    "show_logs": ToolCapability(
        tool_name="show_logs",
        description="Show recent log entries from Prometheus or system journal",
        examples=["show logs", "what errors happened recently", "show system logs"],
        required_slots=[],
        optional_slots=["source", "lines", "level", "since"],
        safe_when=["always safe — read-only"],
        bad_for=["nothing"],
        risk="none",
        validates="returns recent log lines",
        supports_verification=True,
        workflow_tags=["debug_current_error", "diagnose_blocker"],
    ),

    "search_codebase": ToolCapability(
        tool_name="search_codebase",
        description="Search for a string or pattern in a codebase",
        examples=["find all uses of log_event", "search for TODO in Prometheus"],
        required_slots=["query"],
        optional_slots=["project_path"],
        safe_when=["always safe — read-only"],
        bad_for=["binary files"],
        risk="none",
        validates="returns matching file lines",
        supports_verification=True,
        workflow_tags=["debug_current_error", "inspect_recent_changes", "diagnose_blocker"],
    ),

    # ── Git ───────────────────────────────────────────────────────────────────

    "git_status": ToolCapability(
        tool_name="git_status",
        description="Show the git status of a project (modified, staged, untracked files)",
        examples=["what changed", "git status", "check git"],
        required_slots=["project_path"],
        optional_slots=[],
        safe_when=["always safe — read-only"],
        bad_for=["nothing"],
        risk="none",
        validates="returns git status output",
        supports_verification=True,
        workflow_tags=["inspect_recent_changes", "ship_current_project"],
    ),

    "git_diff": ToolCapability(
        tool_name="git_diff",
        description="Show git diff for a project or specific file",
        examples=["show diff", "what did I change in tools.py"],
        required_slots=["project_path"],
        optional_slots=["file"],
        safe_when=["always safe — read-only"],
        bad_for=["nothing"],
        risk="none",
        validates="returns diff output",
        supports_verification=True,
        workflow_tags=["inspect_recent_changes", "ship_current_project"],
    ),

    "git_commit": ToolCapability(
        tool_name="git_commit",
        description="Create a git commit with a message (always requires confirmed=True)",
        examples=["commit with message 'add feature'", "commit these changes"],
        required_slots=["project_path", "message", "confirmed"],
        optional_slots=[],
        safe_when=["confirmed=True", "changes are staged or stageable"],
        bad_for=["uncommitted work that hasn't been reviewed"],
        risk="high",
        validates="git commit hash in output, exit 0",
        supports_verification=True,
        workflow_tags=["ship_current_project"],
    ),

    # ── Session / awareness ───────────────────────────────────────────────────

    "session_wrapup": ToolCapability(
        tool_name="session_wrapup",
        description="Trigger end-of-session summarization and vault write",
        examples=["wrap up", "end session", "write session summary"],
        required_slots=[],
        optional_slots=[],
        safe_when=["always safe"],
        bad_for=["nothing"],
        risk="none",
        validates="session summary written to vault",
        supports_verification=False,
        workflow_tags=[],
    ),

    "system_status": ToolCapability(
        tool_name="system_status",
        description="Report current system status: CPU, RAM, workspace, working memory",
        examples=["what's the system status", "how is Prometheus doing"],
        required_slots=[],
        optional_slots=[],
        safe_when=["always safe — read-only"],
        bad_for=["nothing"],
        risk="none",
        validates="returns system status dict",
        supports_verification=True,
        workflow_tags=["diagnose_blocker"],
    ),

    "get_priorities": ToolCapability(
        tool_name="get_priorities",
        description="Get the current list of priorities from mission state",
        examples=["what should I focus on", "what are my priorities"],
        required_slots=[],
        optional_slots=[],
        safe_when=["always safe — read-only"],
        bad_for=["nothing"],
        risk="none",
        validates="returns list of current priorities",
        supports_verification=False,
        workflow_tags=["resume_mission", "continue_next_action"],
    ),

    "web_search": ToolCapability(
        tool_name="web_search",
        description="Search the web and return a summarized answer",
        examples=["search for Python async patterns", "look up the PyQt6 docs"],
        required_slots=["query"],
        optional_slots=[],
        safe_when=["always safe — read-only"],
        bad_for=["internal codebase searches", "local file lookups"],
        risk="none",
        validates="returns non-empty search result summary",
        supports_verification=True,
        workflow_tags=["debug_current_error", "diagnose_blocker"],
    ),

    # ── Autonomous coding ────────────────────────────────────────────────────

    "start_coding_task": ToolCapability(
        tool_name="start_coding_task",
        description="Dispatch an autonomous coding task to the Claude Code agent",
        examples=["start coding the login feature", "implement the retry logic"],
        required_slots=["goal"],
        optional_slots=["project_path"],
        safe_when=["goal is clearly scoped", "project path is in workspace"],
        bad_for=["vague goals without a concrete spec"],
        risk="medium",
        validates="task dispatched with task_id, agent starts running",
        supports_verification=True,
        workflow_tags=["ship_current_project"],
    ),

    "get_coding_status": ToolCapability(
        tool_name="get_coding_status",
        description="Check the status of the running coding agent",
        examples=["how's the coding going", "coding agent status"],
        required_slots=[],
        optional_slots=[],
        safe_when=["always safe — read-only"],
        bad_for=["nothing"],
        risk="none",
        validates="returns coding agent status and recent output",
        supports_verification=False,
        workflow_tags=["ship_current_project"],
    ),

    # ── Build tools ──────────────────────────────────────────────────────────

    "start_build": ToolCapability(
        tool_name="start_build",
        description="Start an orchestrated build pipeline (Architect → Coder → Tester → Debugger)",
        examples=["build the auth module", "start build for the new API"],
        required_slots=["goal"],
        optional_slots=["project_path"],
        safe_when=["goal is clearly scoped", "resources available"],
        bad_for=["tasks without a concrete deliverable"],
        risk="medium",
        validates="build pipeline started with pipeline_id",
        supports_verification=True,
        workflow_tags=["ship_current_project"],
    ),

    "get_build_status": ToolCapability(
        tool_name="get_build_status",
        description="Check the status of the current build pipeline",
        examples=["build status", "how's the build going"],
        required_slots=[],
        optional_slots=[],
        safe_when=["always safe — read-only"],
        bad_for=["nothing"],
        risk="none",
        validates="returns build phase, test results, needs_human flag",
        supports_verification=False,
        workflow_tags=["ship_current_project"],
    ),

    # ── Vault / memory ───────────────────────────────────────────────────────

    "query_vault": ToolCapability(
        tool_name="query_vault",
        description="Query the personal Obsidian vault memory corpus (32K+ chunks)",
        examples=["what do I know about Docker", "recall my notes on async Python"],
        required_slots=["query"],
        optional_slots=[],
        safe_when=["always safe — read-only"],
        bad_for=["nothing"],
        risk="none",
        validates="returns relevant vault chunks with titles",
        supports_verification=True,
        workflow_tags=["resume_mission", "diagnose_blocker"],
    ),

    "run_diagnostics": ToolCapability(
        tool_name="run_diagnostics",
        description="Run system diagnostics: checks Prometheus health, services, and config",
        examples=["run diagnostics", "check system health", "diagnose Prometheus"],
        required_slots=[],
        optional_slots=[],
        safe_when=["always safe"],
        bad_for=["nothing"],
        risk="none",
        validates="returns structured diagnostics report with pass/fail items",
        supports_verification=True,
        workflow_tags=["diagnose_blocker"],
    ),

    # ── Mission state tools ──────────────────────────────────────────────────

    "get_mission_status": ToolCapability(
        tool_name="get_mission_status",
        description="Get the current mission, subtasks, blockers, and next action",
        examples=["what are we working on", "mission status", "what's the mission"],
        required_slots=[],
        optional_slots=[],
        safe_when=["always safe — read-only"],
        bad_for=["nothing"],
        risk="none",
        validates="returns mission dict with current_mission, tasks, blockers",
        supports_verification=False,
        workflow_tags=["resume_mission", "continue_next_action", "diagnose_blocker"],
    ),

    "set_mission": ToolCapability(
        tool_name="set_mission",
        description="Set a new top-level mission or goal for the current session",
        examples=["set mission to ship the login feature", "our mission is to fix the audio bug"],
        required_slots=["mission"],
        optional_slots=[],
        safe_when=["always safe"],
        bad_for=["nothing"],
        risk="none",
        validates="mission updated in mission state file",
        supports_verification=False,
        workflow_tags=["resume_mission", "ship_current_project"],
    ),

    "add_subtask": ToolCapability(
        tool_name="add_subtask",
        description="Add a subtask to the current mission",
        examples=["add subtask: write the tests", "add a step for documentation"],
        required_slots=["task"],
        optional_slots=[],
        safe_when=["always safe"],
        bad_for=["nothing"],
        risk="none",
        validates="subtask appears in mission state",
        supports_verification=False,
        workflow_tags=["resume_mission", "ship_current_project"],
    ),

    "complete_subtask": ToolCapability(
        tool_name="complete_subtask",
        description="Mark a subtask as complete",
        examples=["mark tests as done", "complete the auth subtask"],
        required_slots=["task"],
        optional_slots=[],
        safe_when=["always safe"],
        bad_for=["nothing"],
        risk="none",
        validates="subtask marked complete in mission state",
        supports_verification=False,
        workflow_tags=["ship_current_project", "continue_next_action"],
    ),

    # ── Meta-tools (MissionState methods, not in ACTION_ENUM) ───────────────
    # These represent workflow-level operations accessible via MissionState directly.

    "add_blocker": ToolCapability(
        tool_name="add_blocker",
        description="Record a new blocker preventing mission progress",
        examples=["we're blocked on the auth API", "add blocker: test environment is broken"],
        required_slots=["description"],
        optional_slots=[],
        safe_when=["always safe"],
        bad_for=["nothing"],
        risk="none",
        validates="blocker appears in mission state blocked_items",
        supports_verification=False,
        workflow_tags=["diagnose_blocker", "resume_mission"],
    ),

    "clear_blocker": ToolCapability(
        tool_name="clear_blocker",
        description="Remove a blocker from the mission state",
        examples=["clear the auth blocker", "blocker resolved"],
        required_slots=["description"],
        optional_slots=[],
        safe_when=["always safe"],
        bad_for=["nothing"],
        risk="none",
        validates="blocker removed from mission state",
        supports_verification=False,
        workflow_tags=["diagnose_blocker", "continue_next_action"],
    ),

    "set_next_action": ToolCapability(
        tool_name="set_next_action",
        description="Set the next concrete action to take on the current mission",
        examples=["next action is to run the tests", "set next action: write the README"],
        required_slots=["action"],
        optional_slots=[],
        safe_when=["always safe"],
        bad_for=["nothing"],
        risk="none",
        validates="next_action updated in mission state",
        supports_verification=False,
        workflow_tags=["continue_next_action", "resume_mission"],
    ),

    "open_project": ToolCapability(
        tool_name="open_project",
        description="Open the active project in VS Code and a terminal",
        examples=["open the project", "open Prometheus", "set up my workspace"],
        required_slots=["project_path"],
        optional_slots=[],
        safe_when=["project_path is a valid directory"],
        bad_for=["system directories"],
        risk="none",
        validates="VS Code and terminal open at project path",
        supports_verification=True,
        workflow_tags=["open_active_project", "prepare_current_workspace"],
    ),

    # ── Calendar reads (read-only, no writes, no HA coupling) ────────────────

    "calendar_list_upcoming": ToolCapability(
        tool_name="calendar_list_upcoming",
        description="List upcoming Google Calendar events over the next N days",
        examples=["what's on my calendar", "list upcoming events", "show my schedule"],
        required_slots=[],
        optional_slots=["max_results", "days"],
        safe_when=["always safe — read-only"],
        bad_for=["nothing"],
        risk="none",
        validates="returns list of upcoming events with titles and start times",
        supports_verification=True,
        workflow_tags=["resume_mission"],
    ),

    "calendar_get_today": ToolCapability(
        tool_name="calendar_get_today",
        description="List all Google Calendar events for today",
        examples=["what's on my calendar today", "what do I have today", "my schedule today"],
        required_slots=[],
        optional_slots=[],
        safe_when=["always safe — read-only"],
        bad_for=["nothing"],
        risk="none",
        validates="returns today's events sorted by start time",
        supports_verification=True,
        workflow_tags=["resume_mission"],
    ),

    "calendar_get_tomorrow": ToolCapability(
        tool_name="calendar_get_tomorrow",
        description="List all Google Calendar events for tomorrow",
        examples=["what do I have tomorrow", "tomorrow's schedule", "what's on tomorrow"],
        required_slots=[],
        optional_slots=[],
        safe_when=["always safe — read-only"],
        bad_for=["nothing"],
        risk="none",
        validates="returns tomorrow's events sorted by start time",
        supports_verification=True,
        workflow_tags=[],
    ),

    "calendar_get_date": ToolCapability(
        tool_name="calendar_get_date",
        description="List Google Calendar events for a specific YYYY-MM-DD date",
        examples=["what's on May 20th", "events on 2026-05-20"],
        required_slots=["date"],
        optional_slots=[],
        safe_when=["always safe — read-only"],
        bad_for=["nothing"],
        risk="none",
        validates="returns events for the specified date",
        supports_verification=True,
        workflow_tags=[],
    ),

    "calendar_next_event": ToolCapability(
        tool_name="calendar_next_event",
        description="Get the next upcoming timed event on Google Calendar",
        examples=["what's my next event", "next meeting", "what's coming up next"],
        required_slots=[],
        optional_slots=[],
        safe_when=["always safe — read-only"],
        bad_for=["nothing"],
        risk="none",
        validates="returns next timed event or indicates no upcoming events",
        supports_verification=True,
        workflow_tags=["resume_mission"],
    ),

    "calendar_summarize_day": ToolCapability(
        tool_name="calendar_summarize_day",
        description="Summarize the day's calendar: event count, timed events, first/last event",
        examples=["summarize my day", "how does my day look", "what's my day like"],
        required_slots=[],
        optional_slots=["date"],
        safe_when=["always safe — read-only"],
        bad_for=["nothing"],
        risk="none",
        validates="returns structured day summary with event counts and first/last event",
        supports_verification=True,
        workflow_tags=["resume_mission"],
    ),

    "calendar_find_free_blocks": ToolCapability(
        tool_name="calendar_find_free_blocks",
        description="Find free time blocks on a calendar day (ignores all-day events)",
        examples=["do I have a free hour today", "when am I free", "find a free block"],
        required_slots=["date"],
        optional_slots=["minimum_minutes"],
        safe_when=["always safe — read-only"],
        bad_for=["nothing"],
        risk="none",
        validates="returns list of free blocks with start, end, and duration",
        supports_verification=True,
        workflow_tags=[],
    ),
    # ── Calendar write execution (requires approval + env gates) ───────────────
    "calendar_list_reviewed_requests": ToolCapability(
        tool_name="calendar_list_reviewed_requests",
        description="List Lumen calendar proposals that have been dry-run reviewed and are awaiting approval",
        examples=["show pending calendar requests", "list reviewed calendar proposals", "what calendar changes are waiting for approval"],
        required_slots=[],
        optional_slots=[],
        safe_when=["always safe — read-only"],
        bad_for=["nothing"],
        risk="none",
        validates="returns list of reviewed calendar request summaries",
        supports_verification=True,
        workflow_tags=["resume_mission"],
    ),
    "calendar_approve_request": ToolCapability(
        tool_name="calendar_approve_request",
        description="Approve a reviewed Lumen calendar proposal for execution (does not execute — approval only)",
        examples=["approve calendar request req-abc123", "approve the calendar change"],
        required_slots=["request_id"],
        optional_slots=["approved_by"],
        safe_when=["request exists in reviewed dir", "all_dry_run=true", "all operations have requires_prometheus_approval=true"],
        bad_for=["proposals not reviewed yet", "proposals with failed dry-run review"],
        risk="medium",
        validates="writes approval record; does not call Google Calendar API",
        supports_verification=True,
        workflow_tags=[],
    ),
    "calendar_execute_approved_request": ToolCapability(
        tool_name="calendar_execute_approved_request",
        description="Execute an approved Lumen calendar proposal against the live Google Calendar API",
        examples=["execute approved calendar request req-abc123", "run the approved calendar change"],
        required_slots=["request_id", "confirmed"],
        optional_slots=[],
        safe_when=[
            "approval record exists",
            "GOOGLE_CALENDAR_ENABLED=true",
            "GOOGLE_CALENDAR_DRY_RUN=false",
            "confirmed=true in payload",
        ],
        bad_for=["unapproved requests", "when GOOGLE_CALENDAR_DRY_RUN=true", "without explicit confirmed=true"],
        risk="high",
        validates="executes calendar writes; writes result to completed or failed dir",
        supports_verification=True,
        workflow_tags=[],
    ),

    # ── NL calendar creation (confirmation-gated) ─────────────────────────────

    "calendar_create_proposal": ToolCapability(
        tool_name="calendar_create_proposal",
        description=(
            "Parse a natural-language calendar creation request and propose an event "
            "for user confirmation. Does NOT write to calendar."
        ),
        examples=[
            "schedule a focus block tomorrow at 2",
            "add a workout this afternoon",
            "put a standup on my calendar Friday at 10",
        ],
        required_slots=["user_request"],
        optional_slots=[],
        safe_when=["always safe — no calendar write occurs at this stage"],
        bad_for=["recurring events", "calendar updates or deletes"],
        risk="none",
        validates="returns human_summary string asking user to confirm; writes pending confirmation file",
        supports_verification=True,
        workflow_tags=[],
    ),

    "calendar_confirm_create": ToolCapability(
        tool_name="calendar_confirm_create",
        description=(
            "Execute the pending calendar event proposal after user confirms. "
            "Routes through the approved executor pipeline."
        ),
        examples=["yes", "confirm", "do it", "go ahead"],
        required_slots=[],
        optional_slots=["confirmation_id"],
        safe_when=[
            "pending confirmation exists",
            "user has explicitly confirmed",
            "GOOGLE_CALENDAR_ENABLED=true",
            "GOOGLE_CALENDAR_DRY_RUN=false",
        ],
        bad_for=["when no pending confirmation exists", "when GOOGLE_CALENDAR_DRY_RUN=true"],
        risk="high",
        validates="event created on Google Calendar; result written to completed dir",
        supports_verification=True,
        workflow_tags=[],
    ),

    "calendar_cancel_create": ToolCapability(
        tool_name="calendar_cancel_create",
        description="Cancel a pending calendar event proposal before it is confirmed.",
        examples=["no", "cancel", "never mind", "forget it"],
        required_slots=[],
        optional_slots=["confirmation_id"],
        safe_when=["always safe — no calendar write occurred"],
        bad_for=["when no pending confirmation exists"],
        risk="none",
        validates="pending confirmation file marked as canceled",
        supports_verification=False,
        workflow_tags=[],
    ),
}


def get_tool(name: str) -> ToolCapability | None:
    return TOOL_CAPABILITIES.get(name)


def tools_for_workflow(workflow_tag: str) -> list[ToolCapability]:
    return [t for t in TOOL_CAPABILITIES.values() if workflow_tag in t.workflow_tags]


def verifiable_tools() -> list[str]:
    return [name for name, t in TOOL_CAPABILITIES.items() if t.supports_verification]


def high_risk_tools() -> list[str]:
    return [name for name, t in TOOL_CAPABILITIES.items() if t.risk == "high"]

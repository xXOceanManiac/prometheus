"""
Workflow Registry — named operational workflows Prometheus can recognize and execute.

Each workflow represents a multi-step pattern that maps to a user's intent
in the context of their current machine state. Used by WorkflowSelector
to choose an appropriate workflow rather than asking the LLM planner.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass(frozen=True)
class WorkflowDefinition:
    workflow_name: str
    description: str
    trigger_examples: List[str]
    required_context: List[str]      # keys that must be present in world_snapshot or mission
    preferred_tools: List[str]       # ordered list of preferred tool actions
    verification_steps: List[str]    # what to check to confirm success
    risk: str                        # "none" | "low" | "medium" | "high"


WORKFLOWS: dict[str, WorkflowDefinition] = {

    "debug_current_error": WorkflowDefinition(
        workflow_name="debug_current_error",
        description=(
            "Diagnose and attempt to fix the most recent error visible in logs or "
            "on screen. Uses show_logs + search_codebase + summarize_screen."
        ),
        trigger_examples=[
            "fix that", "fix the error", "what's wrong", "debug this",
            "something broke", "there's an error", "fix the bug",
            "why is it failing", "figure out what's wrong",
        ],
        required_context=["recent_errors OR active_window OR project_path"],
        preferred_tools=[
            "show_logs", "summarize_screen", "search_codebase",
            "read_file", "git_diff", "run_shell",
        ],
        verification_steps=[
            "show_logs returns no new errors after fix",
            "run_shell or run_python exits 0",
        ],
        risk="low",
    ),

    "resume_current_mission": WorkflowDefinition(
        workflow_name="resume_current_mission",
        description=(
            "Load the current mission from mission state and prepare the workspace "
            "to continue. Opens the project, loads context, surfaces next action."
        ),
        trigger_examples=[
            "where were we", "what are we working on", "resume", "pick up where we left off",
            "what's the mission", "what were we doing", "continue", "resume the mission",
            "what's next", "get back to work",
        ],
        required_context=["current_mission OR active_project OR last_session_summary"],
        preferred_tools=[
            "get_mission_status", "resume_last_context", "get_active_window",
            "open_code_folder", "get_priorities",
        ],
        verification_steps=[
            "get_mission_status returns non-empty mission",
            "VS Code open at project path",
        ],
        risk="none",
    ),

    "summarize_current_project": WorkflowDefinition(
        workflow_name="summarize_current_project",
        description=(
            "Read the active project's key files and produce a high-level summary "
            "of what it is and where it stands."
        ),
        trigger_examples=[
            "summarize the project", "give me an overview", "what is this codebase",
            "explain the project", "describe what we've built", "project overview",
            "what does this do", "catch me up on the project",
        ],
        required_context=["active_project OR project_path"],
        preferred_tools=[
            "list_files", "read_file", "git_status", "screen_context",
        ],
        verification_steps=[
            "list_files returns file listing",
            "read_file returns README or main entry point content",
        ],
        risk="none",
    ),

    "inspect_recent_changes": WorkflowDefinition(
        workflow_name="inspect_recent_changes",
        description=(
            "Show what changed recently in the project: git diff, recent log activity, "
            "recently modified files."
        ),
        trigger_examples=[
            "what changed", "show me the diff", "what did I change", "show recent changes",
            "what's new", "show the diff", "what have we done", "show changes",
            "git diff", "what have I modified",
        ],
        required_context=["project_path"],
        preferred_tools=[
            "git_status", "git_diff", "show_logs", "list_files",
        ],
        verification_steps=[
            "git_status returns output",
            "git_diff returns diff or 'nothing changed'",
        ],
        risk="none",
    ),

    "check_if_it_worked": WorkflowDefinition(
        workflow_name="check_if_it_worked",
        description=(
            "Verify the result of the most recent action by checking logs, "
            "taking a screenshot, running a quick test, or checking process state."
        ),
        trigger_examples=[
            "did it work", "check if it worked", "did that work", "is it working",
            "verify it", "did the fix work", "does it work now", "check the result",
        ],
        required_context=["last_tool_action OR active_window"],
        preferred_tools=[
            "show_logs", "summarize_screen", "screenshot",
            "run_shell", "list_windows",
        ],
        verification_steps=[
            "show_logs returns no error entries",
            "screenshot taken",
        ],
        risk="none",
    ),

    "open_active_project": WorkflowDefinition(
        workflow_name="open_active_project",
        description=(
            "Open the currently active or most recently used project in VS Code "
            "and a terminal. Sets up the coding workspace."
        ),
        trigger_examples=[
            "open the project", "open prometheus", "set up my workspace",
            "open vs code", "let's code", "open the code",
            "set up for coding", "open my workspace",
        ],
        required_context=["active_project OR project_path OR active_window"],
        preferred_tools=[
            "open_code_folder", "open_terminal_here", "desktop_state",
        ],
        verification_steps=[
            "VS Code window visible in window list",
            "Terminal open at project path",
        ],
        risk="none",
    ),

    "continue_next_action": WorkflowDefinition(
        workflow_name="continue_next_action",
        description=(
            "Execute the next action recorded in mission state. If no next_action "
            "is set, surface the most important subtask or blocker."
        ),
        trigger_examples=[
            "continue", "keep going", "do the next thing", "next step",
            "what do I do next", "proceed", "move forward", "carry on",
            "do the next action", "go",
        ],
        required_context=["current_mission OR next_action OR tasks"],
        preferred_tools=[
            "get_mission_status", "complete_subtask", "set_next_action",
            "run_shell", "run_python", "search_codebase",
        ],
        verification_steps=[
            "next_action executed",
            "subtask marked complete",
        ],
        risk="low",
    ),

    "ship_current_project": WorkflowDefinition(
        workflow_name="ship_current_project",
        description=(
            "Prepare the project for shipping: run tests, check git diff, "
            "commit with a message, optionally push."
        ),
        trigger_examples=[
            "ship it", "commit and push", "let's ship", "time to ship",
            "ready to commit", "finalize this", "wrap it up and commit",
            "ship the code", "ship the changes",
        ],
        required_context=["project_path"],
        preferred_tools=[
            "git_status", "git_diff", "run_shell", "run_python",
            "git_commit",
        ],
        verification_steps=[
            "git_status is clean after commit",
            "git_commit exits 0 with commit hash",
        ],
        risk="high",
    ),

    "diagnose_blocker": WorkflowDefinition(
        workflow_name="diagnose_blocker",
        description=(
            "Diagnose why the current mission is blocked. Checks logs, system status, "
            "active blockers, and recent errors to identify root cause."
        ),
        trigger_examples=[
            "we're stuck", "it's not working", "what's blocking us", "why is this broken",
            "diagnose the problem", "I'm stuck", "figure out the blocker",
            "what's stopping us", "why can't I proceed",
        ],
        required_context=["blockers OR recent_errors OR active_window"],
        preferred_tools=[
            "get_mission_status", "show_logs", "run_diagnostics",
            "system_status", "search_codebase", "summarize_screen",
        ],
        verification_steps=[
            "root cause identified in logs or diagnostics",
            "get_mission_status reflects blocker",
        ],
        risk="none",
    ),

    "prepare_current_workspace": WorkflowDefinition(
        workflow_name="prepare_current_workspace",
        description=(
            "Set up the workspace for the current session: open the active project, "
            "load context, check mission state, set focus mode."
        ),
        trigger_examples=[
            "set up my workspace", "prepare my workspace", "get ready to work",
            "let's get started", "set everything up", "workspace setup",
            "get me set up", "morning setup", "start the session",
        ],
        required_context=["active_project OR current_mission"],
        preferred_tools=[
            "desktop_state", "get_mission_status", "open_code_folder",
            "open_terminal_here", "mode_lock_in", "run_routine",
        ],
        verification_steps=[
            "VS Code open at project path",
            "mission state loaded",
        ],
        risk="none",
    ),
}


def get_workflow(name: str) -> WorkflowDefinition | None:
    return WORKFLOWS.get(name)


def all_trigger_examples() -> list[tuple[str, str]]:
    """Return list of (example, workflow_name) for all workflows."""
    pairs: list[tuple[str, str]] = []
    for name, wf in WORKFLOWS.items():
        for ex in wf.trigger_examples:
            pairs.append((ex, name))
    return pairs

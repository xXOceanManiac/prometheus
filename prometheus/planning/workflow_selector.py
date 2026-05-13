"""
Workflow Selector — maps user commands to named workflows using rule-based matching.

Pure function: no LLM, no side effects, no imports from core/.
Uses world_snapshot + mission_summary to resolve context-dependent workflows
like "fix that" → debug_current_error or "continue" → continue_next_action.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from prometheus.planning.workflow_registry import WORKFLOWS, WorkflowDefinition


@dataclass
class WorkflowResolution:
    matched: bool
    workflow_name: str | None
    confidence: float
    reasoning: str
    inferred_target: str | None          # e.g. active project name or error description
    preferred_tools: list[str]
    requires_confirmation: bool
    requires_clarification: bool
    clarification_question: str | None


# ── Normalisation helpers ─────────────────────────────────────────────────────

def _norm(text: str) -> str:
    return " ".join(str(text).strip().lower().split())


def _contains_any(text: str, phrases: list[str]) -> bool:
    return any(p in text for p in phrases)


# ── Per-workflow trigger tables ───────────────────────────────────────────────

_DEBUG_TRIGGERS = [
    "fix that", "fix the error", "what's wrong", "debug this", "debug that",
    "something broke", "there's an error", "fix the bug", "fix this",
    "why is it failing", "figure out what's wrong", "it's broken", "broken",
    "it broke", "it failed", "what broke", "fix it", "traceback",
]

_RESUME_TRIGGERS = [
    "where were we", "what are we working on", "what were we doing",
    "resume the mission", "resume mission", "pick up where we left off",
    "what's the mission", "what's our mission", "catch me up",
    "what are we building", "what's our goal", "get back to work",
]

_SUMMARIZE_TRIGGERS = [
    "summarize the project", "give me an overview", "what is this codebase",
    "explain the project", "describe what we've built", "project overview",
    "what does this do", "catch me up on the project", "overview of the project",
    "what have we built",
]

_INSPECT_TRIGGERS = [
    "what changed", "show me the diff", "what did i change", "show recent changes",
    "what's new", "show the diff", "what have we done", "show changes",
    "what have i modified", "show git diff", "recent changes",
]

_VERIFY_TRIGGERS = [
    "did it work", "check if it worked", "did that work", "is it working",
    "verify it", "did the fix work", "does it work now", "check the result",
    "did that fix it", "is it fixed",
]

_OPEN_PROJECT_TRIGGERS = [
    "open the project", "let's code", "open the code",
    "set up for coding", "open my workspace", "open vs code here",
    "open code", "start coding",
]

_NEXT_ACTION_TRIGGERS = [
    "keep going", "do the next thing", "next step", "what do i do next",
    "proceed", "move forward", "carry on", "do the next action",
    "what do i do next",
]

_SHIP_TRIGGERS = [
    "ship it", "commit and push", "let's ship", "time to ship",
    "ready to commit", "finalize this", "wrap it up and commit",
    "ship the code", "ship the changes", "commit this",
]

_DIAGNOSE_TRIGGERS = [
    "we're stuck", "it's not working", "what's blocking us", "why is this broken",
    "diagnose the problem", "i'm stuck", "figure out the blocker",
    "what's stopping us", "why can't i proceed", "what's the problem",
    "diagnose", "run diagnostics",
]

_PREPARE_TRIGGERS = [
    "set up my workspace", "prepare my workspace", "get ready to work",
    "let's get started", "set everything up", "workspace setup",
    "get me set up", "morning setup", "start the session",
]


# ── Context extractors ───────────────────────────────────────────────────────

def _active_project(snapshot: dict) -> str | None:
    return (
        snapshot.get("active_project")
        or snapshot.get("active_workspace")
        or None
    )


def _project_path(snapshot: dict) -> str | None:
    return (
        snapshot.get("active_project_path")
        or snapshot.get("project_path")
        or None
    )


def _recent_errors(snapshot: dict) -> list[str]:
    errors = snapshot.get("recent_errors") or []
    if isinstance(errors, list):
        return [str(e) for e in errors]
    return []


def _blockers(mission_summary: str) -> bool:
    return bool(mission_summary) and any(
        kw in mission_summary.lower()
        for kw in ("blocked", "blocker", "stuck", "waiting on", "cannot")
    )


def _has_mission(mission_summary: str) -> bool:
    return bool(mission_summary and mission_summary.strip() not in ("", "No active mission."))


def _active_window_title(snapshot: dict) -> str:
    win = snapshot.get("active_window") or {}
    if isinstance(win, dict):
        return str(win.get("title", ""))
    return str(win)


def _last_tool_action(snapshot: dict) -> str:
    return str(snapshot.get("last_tool_action", "") or "")


# ── Main resolution function ─────────────────────────────────────────────────

def resolve_workflow(
    user_command: str,
    world_snapshot: dict[str, Any] | None = None,
    mission_summary: str = "",
) -> WorkflowResolution:
    """
    Map a user command to a named workflow using rule-based matching.

    Deterministic — no LLM, no side effects.
    Returns WorkflowResolution with matched=False if no workflow applies.
    """
    t0 = time.monotonic()
    snap = world_snapshot or {}
    text = _norm(user_command)

    if not text:
        return WorkflowResolution(
            matched=False, workflow_name=None, confidence=0.0,
            reasoning="Empty command", inferred_target=None,
            preferred_tools=[], requires_confirmation=False,
            requires_clarification=True,
            clarification_question="What would you like me to do?",
        )

    project = _active_project(snap)
    project_path = _project_path(snap)
    errors = _recent_errors(snap)
    has_errors = bool(errors)
    has_mission = _has_mission(mission_summary)
    has_blockers = _blockers(mission_summary)
    window_title = _active_window_title(snap)
    last_tool = _last_tool_action(snap)

    # ── 1. Verify result (check before debug: "did that fix it" is verify, not debug) ──
    if _contains_any(text, _VERIFY_TRIGGERS):
        target = last_tool or project or None
        return WorkflowResolution(
            matched=True,
            workflow_name="check_if_it_worked",
            confidence=0.94,
            reasoning=f"Verification request after '{last_tool or 'last action'}'",
            inferred_target=target,
            preferred_tools=WORKFLOWS["check_if_it_worked"].preferred_tools,
            requires_confirmation=False,
            requires_clarification=False,
            clarification_question=None,
        )

    # ── 2. Diagnose blocker (check before debug: "why is this broken" = investigation) ──
    if _contains_any(text, _DIAGNOSE_TRIGGERS):
        target = (errors[0][:60] if errors else None) or (project or "current state")
        return WorkflowResolution(
            matched=True,
            workflow_name="diagnose_blocker",
            confidence=0.93,
            reasoning="User requesting diagnosis / blocker investigation",
            inferred_target=target,
            preferred_tools=WORKFLOWS["diagnose_blocker"].preferred_tools,
            requires_confirmation=False,
            requires_clarification=False,
            clarification_question=None,
        )

    # ── 3. Debug: "fix that", "fix the bug", "something broke" ─────────────
    if _contains_any(text, _DEBUG_TRIGGERS):
        if has_errors or project_path or window_title:
            target = errors[0][:80] if errors else (project or window_title or "current context")
            return WorkflowResolution(
                matched=True,
                workflow_name="debug_current_error",
                confidence=0.95 if has_errors else 0.85,
                reasoning=f"Error context present: {target[:60]}",
                inferred_target=target,
                preferred_tools=WORKFLOWS["debug_current_error"].preferred_tools,
                requires_confirmation=False,
                requires_clarification=False,
                clarification_question=None,
            )
        return WorkflowResolution(
            matched=False, workflow_name=None, confidence=0.3,
            reasoning="Debug intent but no error context available",
            inferred_target=None, preferred_tools=[],
            requires_confirmation=False, requires_clarification=True,
            clarification_question="What would you like me to fix? I don't see a recent error.",
        )

    # ── 4. Inspect recent changes ───────────────────────────────────────────
    if _contains_any(text, _INSPECT_TRIGGERS):
        if not project_path:
            return WorkflowResolution(
                matched=False, workflow_name=None, confidence=0.4,
                reasoning="Inspect intent but no project path in context",
                inferred_target=None, preferred_tools=[],
                requires_confirmation=False, requires_clarification=True,
                clarification_question="Which project should I check git changes for?",
            )
        return WorkflowResolution(
            matched=True,
            workflow_name="inspect_recent_changes",
            confidence=0.93,
            reasoning=f"Inspect changes in {project or project_path}",
            inferred_target=project or project_path,
            preferred_tools=WORKFLOWS["inspect_recent_changes"].preferred_tools,
            requires_confirmation=False,
            requires_clarification=False,
            clarification_question=None,
        )

    # ── 5. Ship / commit ────────────────────────────────────────────────────
    if _contains_any(text, _SHIP_TRIGGERS):
        if not project_path:
            return WorkflowResolution(
                matched=False, workflow_name=None, confidence=0.5,
                reasoning="Ship intent but no project path",
                inferred_target=None, preferred_tools=[],
                requires_confirmation=False, requires_clarification=True,
                clarification_question="Which project should I commit?",
            )
        return WorkflowResolution(
            matched=True,
            workflow_name="ship_current_project",
            confidence=0.92,
            reasoning=f"Ship/commit request for {project or project_path}",
            inferred_target=project or project_path,
            preferred_tools=WORKFLOWS["ship_current_project"].preferred_tools,
            requires_confirmation=True,  # risk=high always requires confirmation
            requires_clarification=False,
            clarification_question=None,
        )

    # ── 6. Summarize project (check before resume: "catch me up on the project") ──
    if _contains_any(text, _SUMMARIZE_TRIGGERS):
        if not project and not project_path:
            return WorkflowResolution(
                matched=False, workflow_name=None, confidence=0.4,
                reasoning="Summarize intent but no active project",
                inferred_target=None, preferred_tools=[],
                requires_confirmation=False, requires_clarification=True,
                clarification_question="Which project should I summarize?",
            )
        return WorkflowResolution(
            matched=True,
            workflow_name="summarize_current_project",
            confidence=0.91,
            reasoning=f"Summarize project: {project or project_path}",
            inferred_target=project or project_path,
            preferred_tools=WORKFLOWS["summarize_current_project"].preferred_tools,
            requires_confirmation=False,
            requires_clarification=False,
            clarification_question=None,
        )

    # ── 7. Resume mission ───────────────────────────────────────────────────
    if _contains_any(text, _RESUME_TRIGGERS) or text in ("resume", "continue"):
        if not has_mission and not project:
            return WorkflowResolution(
                matched=False, workflow_name=None, confidence=0.4,
                reasoning="Resume intent but no mission or project context",
                inferred_target=None, preferred_tools=[],
                requires_confirmation=False, requires_clarification=True,
                clarification_question="I don't have an active mission. What should we work on?",
            )
        target = project or (mission_summary[:60] if has_mission else None)
        return WorkflowResolution(
            matched=True,
            workflow_name="resume_current_mission",
            confidence=0.93 if has_mission else 0.80,
            reasoning=f"Resume mission: {target or 'last known state'}",
            inferred_target=target,
            preferred_tools=WORKFLOWS["resume_current_mission"].preferred_tools,
            requires_confirmation=False,
            requires_clarification=False,
            clarification_question=None,
        )

    # ── 8. Continue next action ─────────────────────────────────────────────
    if _contains_any(text, _NEXT_ACTION_TRIGGERS):
        if not has_mission:
            return WorkflowResolution(
                matched=False, workflow_name=None, confidence=0.4,
                reasoning="Continue intent but no active mission",
                inferred_target=None, preferred_tools=[],
                requires_confirmation=False, requires_clarification=True,
                clarification_question="What should I continue? There's no active mission.",
            )
        return WorkflowResolution(
            matched=True,
            workflow_name="continue_next_action",
            confidence=0.91,
            reasoning="Continue next action from mission state",
            inferred_target=project,
            preferred_tools=WORKFLOWS["continue_next_action"].preferred_tools,
            requires_confirmation=False,
            requires_clarification=False,
            clarification_question=None,
        )

    # ── 9. Prepare workspace (check before open_project: "set up my workspace") ──
    if _contains_any(text, _PREPARE_TRIGGERS):
        return WorkflowResolution(
            matched=True,
            workflow_name="prepare_current_workspace",
            confidence=0.91,
            reasoning="Workspace setup request",
            inferred_target=project,
            preferred_tools=WORKFLOWS["prepare_current_workspace"].preferred_tools,
            requires_confirmation=False,
            requires_clarification=False,
            clarification_question=None,
        )

    # ── 10. Open project ────────────────────────────────────────────────────
    if _contains_any(text, _OPEN_PROJECT_TRIGGERS):
        # Skip single-word app opens — those go to open_app
        if "open firefox" in text or "open spotify" in text or "open terminal" in text:
            return WorkflowResolution(
                matched=False, workflow_name=None, confidence=0.0,
                reasoning="open_app command, not a project workflow",
                inferred_target=None, preferred_tools=[],
                requires_confirmation=False, requires_clarification=False,
                clarification_question=None,
            )
        target = project or project_path
        if not target:
            return WorkflowResolution(
                matched=False, workflow_name=None, confidence=0.4,
                reasoning="Open project intent but no project in context",
                inferred_target=None, preferred_tools=[],
                requires_confirmation=False, requires_clarification=True,
                clarification_question="Which project should I open?",
            )
        return WorkflowResolution(
            matched=True,
            workflow_name="open_active_project",
            confidence=0.90,
            reasoning=f"Open project: {target}",
            inferred_target=target,
            preferred_tools=WORKFLOWS["open_active_project"].preferred_tools,
            requires_confirmation=False,
            requires_clarification=False,
            clarification_question=None,
        )

    # ── 11. "Open <ProjectName>" — named project open ──────────────────────
    m = re.match(r"open\s+(\w[\w\s\-\.]*?)(?:\s+in\s+(?:code|vs\s*code|vscode))?\s*$", text)
    if m:
        named = m.group(1).strip()
        # Skip single-word generic apps — those go to open_app
        if " " in named or (project and named.lower() in project.lower()):
            target_path = project_path if project and named.lower() in project.lower() else None
            return WorkflowResolution(
                matched=True,
                workflow_name="open_active_project",
                confidence=0.88,
                reasoning=f"Named project open: {named}",
                inferred_target=target_path or named,
                preferred_tools=WORKFLOWS["open_active_project"].preferred_tools,
                requires_confirmation=False,
                requires_clarification=not bool(target_path),
                clarification_question=f"Where is '{named}'? I don't have a path for it." if not target_path else None,
            )

    # ── No workflow matched ─────────────────────────────────────────────────
    return WorkflowResolution(
        matched=False,
        workflow_name=None,
        confidence=0.0,
        reasoning="No workflow pattern matched",
        inferred_target=None,
        preferred_tools=[],
        requires_confirmation=False,
        requires_clarification=False,
        clarification_question=None,
    )

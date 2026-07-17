"""
contextual_intent.py — Context-first intent inference for Prometheus.

ContextualIntentResolver infers specific intent from vague commands by combining
rule-based pattern matching with world snapshot context. LLM is used only when
rule-based resolution yields low confidence.

Output schema:
{
  "intent": str,                      # specific inferred intent
  "confidence": float,                # 0.0–1.0
  "inferred_target": str,             # what the vague pronoun resolves to
  "reasoning_summary": str,           # one sentence
  "slots": dict,                      # extracted arguments
  "risk": "safe"|"medium"|"high"|"dangerous",
  "should_execute": bool,
  "requires_confirmation": bool,
  "requires_clarification": bool,
  "clarifying_question": str | null,
  "user_facing_assumption": str,      # spoken before executing
}
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from prometheus.infra.utils import log_event

# ── Risk → policy mapping ─────────────────────────────────────────────────────
# confidence >= 0.90 and safe  → execute (mention assumption briefly)
# confidence >= 0.80 and medium → confirm
# confidence < 0.80             → clarify
# high / dangerous              → always confirm or block

_RISK_WEIGHT = {"safe": 0, "medium": 1, "high": 2, "dangerous": 3}

# ── Vague verb patterns ───────────────────────────────────────────────────────

_RX_FIX        = re.compile(r'\b(fix|debug|patch|repair|resolve)\b', re.I)
_RX_OPEN       = re.compile(r'\b(open|pull|bring|launch)\b', re.I)
_RX_RUN        = re.compile(r'\b(run|execute|start)\b', re.I)
_RX_SUMMARIZE  = re.compile(r'\b(summarize|summarise|recap)\b|sum\s+\S+\s+up', re.I)
_RX_CONTINUE   = re.compile(r'^(continue|keep\s+going|go\s+ahead|carry\s+on|proceed|resume|next\s+step)\b', re.I)
_RX_STATUS     = re.compile(
    r"(what'?s (wrong|broken|the\s+(issue|problem|status))|what\s+(is|are)\s+(wrong|broken|the\s+(issue|problem|status))|"
    r"any\s+errors?|any\s+issues?|any\s+blockers?|"
    r"show\s+(me\s+)?the\s+status|show\s+(me\s+)?status|"
    r"what are we doing|how'?s it (going|looking))",
    re.I
)
_RX_CHECK      = re.compile(r'\b(check|verify|confirm|did\s+(it|that|this)\s+work|check if (it|that) worked|check results?)\b', re.I)
_RX_SHIP       = re.compile(r'\b(ship|deploy|push|release|publish)\b', re.I)
_RX_DELETE     = re.compile(r'\b(delete|remove|drop|destroy|erase|wipe)\b', re.I)
_RX_PREP       = re.compile(r'\b(prep|prepare|scaffold)\b|set\s+\S+\s+up', re.I)
_RX_CLEAN      = re.compile(r'\b(clean|clear|tidy|lint|format)\b', re.I)
_RX_HANDLE     = re.compile(r'\b(handle|deal\s+with|take\s+care\s+of|manage)\b', re.I)

# Vague pronoun/target patterns
_RX_VAGUE_TARGET = re.compile(
    r'\b(it|that|this|results?|the\s+(thing|issue|error|bug|problem|file|project|app|'
    r'script|test|code|page|screen|deployment|repo|branch|task|result|output|blocker|session))\b',
    re.I
)

# Status queries that need world context but don't contain pronouns
_RX_STATUS_QUERY = re.compile(
    r"(what'?s (wrong|broken|the\s+(issue|problem|status))|what\s+(is|are)\s+(wrong|broken)|"
    r"any\s+errors?|any\s+issues?|any\s+blockers?|"
    r"show\s+(me\s+)?status|show\s+(me\s+)?the\s+status|"
    r"what are we doing|how'?s it going)",
    re.I
)

# Continuation phrases that need mission context (no vague pronoun required)
_RX_CONTINUATION_PHRASE = re.compile(
    r'^(continue|keep\s+going|go\s+ahead|carry\s+on|proceed|resume|next\s+step)\b',
    re.I
)

# Commands that are specific and should NOT be treated as vague
# (these are caught by direct override system first in practice)
_RX_SPECIFIC_EXEMPT = re.compile(
    r'\b(what time|what day|what is the date|'
    r'open firefox|open chrome|open terminal|open spotify|open discord|'
    r'take a screenshot|grab a screenshot|'
    r'search for|look up|weather|temperature|news|'
    r'turn (on|off)|volume (up|down|set)|mute)\b',
    re.I
)


def _is_vague(text: str) -> bool:
    """Return True if the command likely needs world context to resolve."""
    t = text.lower().strip()
    # Specific commands are never vague (direct override catches them first)
    if _RX_SPECIFIC_EXEMPT.search(t):
        return False
    # Continuation phrases need mission context
    if _RX_CONTINUATION_PHRASE.match(t):
        return True
    # Status queries need world context
    if _RX_STATUS_QUERY.search(t):
        return True
    # Vague pronoun as target of an action
    return bool(_RX_VAGUE_TARGET.search(t))


# ── Resolution result builder ─────────────────────────────────────────────────

def _make_result(
    intent: str,
    confidence: float,
    inferred_target: str,
    reasoning: str,
    slots: dict,
    risk: str,
    *,
    assumption: str = "",
    clarifying_question: str | None = None,
) -> dict[str, Any]:
    requires_clarification = clarifying_question is not None
    requires_confirmation = (
        not requires_clarification
        and (
            _RISK_WEIGHT.get(risk, 0) >= 2          # high or dangerous
            or (confidence >= 0.80 and _RISK_WEIGHT.get(risk, 0) == 1)  # medium + confident
        )
    )
    should_execute = (
        not requires_clarification
        and not requires_confirmation
        and confidence >= 0.90
        and _RISK_WEIGHT.get(risk, 0) == 0
    )
    return {
        "intent": intent,
        "confidence": round(confidence, 3),
        "inferred_target": inferred_target,
        "reasoning_summary": reasoning,
        "slots": slots,
        "risk": risk,
        "should_execute": should_execute,
        "requires_confirmation": requires_confirmation,
        "requires_clarification": requires_clarification,
        "clarifying_question": clarifying_question,
        "user_facing_assumption": assumption,
    }


def _clarify(question: str, intent: str = "unknown") -> dict[str, Any]:
    return _make_result(
        intent=intent,
        confidence=0.4,
        inferred_target="",
        reasoning="Cannot resolve target without more context",
        slots={},
        risk="safe",
        clarifying_question=question,
    )


# ── ContextualIntentResolver ──────────────────────────────────────────────────

class ContextualIntentResolver:
    """
    Resolves vague natural language commands to specific actionable intents
    using the current world snapshot.

    Fast rule-based tier: < 50ms, used on the voice path.
    LLM-assisted tier: 4-15s, used for background tasks only.
    """

    def resolve(
        self,
        command: str,
        snapshot: dict[str, Any],
        *,
        mode: str = "fast",  # "fast" = rule-based only; "thorough" = rule-based + LLM fallback
    ) -> dict[str, Any] | None:
        """
        Resolve a vague command to a structured intent.
        Returns None if the command is not vague (caller should use normal path).
        Returns a result dict if a resolution was found or clarification is needed.
        """
        text = str(command).strip()
        if not text:
            return None

        if not _is_vague(text):
            return None

        result = self._rule_based_resolve(text, snapshot)

        if result is not None:
            log_event("contextual_intent_resolved", {
                "command": text[:80],
                "intent": result["intent"],
                "confidence": result["confidence"],
                "risk": result["risk"],
                "mode": "rule_based",
            })
            return result

        if mode == "thorough":
            result = self._llm_resolve(text, snapshot)
            if result is not None:
                log_event("contextual_intent_resolved", {
                    "command": text[:80],
                    "intent": result["intent"],
                    "confidence": result["confidence"],
                    "risk": result["risk"],
                    "mode": "llm",
                })
                return result

        return None

    # ------------------------------------------------------------------
    # Rule-based resolution tier
    # ------------------------------------------------------------------

    def _rule_based_resolve(self, text: str, snap: dict[str, Any]) -> dict[str, Any] | None:
        t = text.lower().strip()

        # ── "continue" / "proceed" / "next step" ──────────────────────
        if _RX_CONTINUE.match(t):
            return self._resolve_continue(snap)

        # ── "what's wrong" / "any errors" / "show status" ─────────────
        if _RX_STATUS.search(t) or "what are we doing" in t:
            return self._resolve_status(snap)

        # ── "fix that" / "debug this" ──────────────────────────────────
        if _RX_FIX.search(t) and _RX_VAGUE_TARGET.search(t):
            return self._resolve_fix(snap)

        # ── "check if it worked" / "verify" ───────────────────────────
        if _RX_CHECK.search(t) and _RX_VAGUE_TARGET.search(t):
            return self._resolve_check(snap)

        # ── "summarize this" / "recap the screen" ─────────────────────
        if _RX_SUMMARIZE.search(t):
            return self._resolve_summarize(snap)

        # ── "open it" / "open the project" ────────────────────────────
        if _RX_OPEN.search(t) and _RX_VAGUE_TARGET.search(t):
            return self._resolve_open(t, snap)

        # ── "run it" / "run the script" ───────────────────────────────
        if _RX_RUN.search(t) and _RX_VAGUE_TARGET.search(t):
            return self._resolve_run(snap)

        # ── "ship it" / "deploy" ──────────────────────────────────────
        if _RX_SHIP.search(t):
            return self._resolve_ship(snap)

        # ── "delete it" / "remove this" ───────────────────────────────
        if _RX_DELETE.search(t) and _RX_VAGUE_TARGET.search(t):
            return self._resolve_delete(snap)

        # ── "clean this up" / "tidy" ──────────────────────────────────
        if _RX_CLEAN.search(t) and _RX_VAGUE_TARGET.search(t):
            return self._resolve_clean(snap)

        # ── "handle it" / "deal with that" ────────────────────────────
        if _RX_HANDLE.search(t) and _RX_VAGUE_TARGET.search(t):
            return self._resolve_handle(snap)

        # ── "prep this" / "set it up" ─────────────────────────────────
        if _RX_PREP.search(t) and _RX_VAGUE_TARGET.search(t):
            return self._resolve_prep(snap)

        return None

    # ── Individual resolvers ──────────────────────────────────────────────────

    def _resolve_continue(self, snap: dict) -> dict[str, Any]:
        next_action = snap.get("next_action", "").strip()
        if next_action:
            return _make_result(
                intent="execute_next_action",
                confidence=0.92,
                inferred_target=next_action,
                reasoning="Current mission has a next action defined",
                slots={"action": next_action},
                risk="medium",
                assumption=f"I'm using the current mission's next action: '{next_action[:60]}'.",
            )
        # Check subtasks
        subtasks = snap.get("subtasks", [])
        if subtasks:
            first = subtasks[0].get("description", "")
            return _make_result(
                intent="start_next_subtask",
                confidence=0.82,
                inferred_target=first,
                reasoning="No next_action set; using first active subtask",
                slots={"subtask": first},
                risk="medium",
                assumption=f"I'm treating 'continue' as starting the next subtask: '{first[:60]}'.",
            )
        mission = snap.get("current_mission", "").strip()
        if mission:
            return _make_result(
                intent="get_mission_status",
                confidence=0.92,
                inferred_target=mission,
                reasoning="Mission set but no next action or subtasks",
                slots={"action": "get_mission_status"},
                risk="safe",
                assumption="No specific next action is set. Showing mission status.",
            )
        return _clarify("What would you like to continue? No active mission or next action is set.", "continue")

    def _resolve_status(self, snap: dict) -> dict[str, Any]:
        blockers = snap.get("blockers", [])
        errors = snap.get("recent_errors", [])
        mission = snap.get("current_mission", "").strip()

        target_parts = []
        if blockers:
            target_parts.append(f"{len(blockers)} blocker(s)")
        if errors:
            target_parts.append(f"{len(errors)} recent error(s)")
        if mission:
            target_parts.append(f"mission: {mission[:40]}")

        if blockers or errors or mission:
            target = "; ".join(target_parts) or "mission status"
            return _make_result(
                intent="get_mission_status",
                confidence=0.95,
                inferred_target=target,
                reasoning="Surfacing blockers, recent errors, and mission state",
                slots={"action": "get_mission_status"},
                risk="safe",
                assumption="Showing current mission status, blockers, and recent errors.",
            )
        return _make_result(
            intent="run_diagnostics",
            confidence=0.92,
            inferred_target="system diagnostics",
            reasoning="No mission or blockers found; running diagnostics",
            slots={"action": "run_diagnostics"},
            risk="safe",
            assumption="No active mission. Running system diagnostics.",
        )

    def _resolve_fix(self, snap: dict) -> dict[str, Any]:
        errors = snap.get("recent_errors", [])
        git_changes = snap.get("git_has_changes", False)
        project_path = snap.get("focused_project_path", "")
        active_app = snap.get("active_app", "")

        if errors:
            first_err = errors[-1]  # most recent
            err_desc = first_err.get("description", "recent error")[:80]
            kind = first_err.get("kind", "")
            return _make_result(
                intent="fix_recent_error",
                confidence=0.87,
                inferred_target=err_desc,
                reasoning=f"Most recent activity log has error: {kind}",
                slots={"error": err_desc, "kind": kind, "project_path": project_path},
                risk="medium",
                assumption=f"I'm treating 'that' as the recent error: '{err_desc[:60]}'.",
            )

        if active_app == "terminal" and project_path:
            return _make_result(
                intent="search_codebase_for_errors",
                confidence=0.92,
                inferred_target="terminal / active project",
                reasoning="Active app is terminal; checking for issues in project",
                slots={"action": "git_status", "project_path": project_path},
                risk="safe",
                assumption="I'm checking the active project for issues.",
            )

        if git_changes and project_path:
            return _make_result(
                intent="inspect_git_changes",
                confidence=0.92,
                inferred_target="uncommitted changes",
                reasoning="Project has uncommitted changes that may need fixing",
                slots={"action": "git_diff", "project_path": project_path},
                risk="safe",
                assumption=f"I'm treating 'that' as the uncommitted changes in {project_path.split('/')[-1]}.",
            )

        blocker = snap.get("blockers", [])
        if blocker:
            return _make_result(
                intent="address_blocker",
                confidence=0.82,
                inferred_target=blocker[0][:80],
                reasoning="Active blocker found in mission state",
                slots={"blocker": blocker[0]},
                risk="medium",
                assumption=f"I'm treating 'that' as the current blocker: '{blocker[0][:60]}'.",
            )

        return _clarify("What should I fix? I don't see a recent error or active issue to target.", "fix")

    def _resolve_check(self, snap: dict) -> dict[str, Any]:
        recent = snap.get("recent_activity", [])
        next_action = snap.get("next_action", "").strip()
        project_path = snap.get("focused_project_path", "")

        if next_action:
            return _make_result(
                intent="verify_last_action",
                confidence=0.92,
                inferred_target=next_action,
                reasoning="Verifying outcome of the current next action",
                slots={"action": next_action, "project_path": project_path},
                risk="safe",
                assumption=f"I'm checking whether '{next_action[:60]}' succeeded.",
            )

        if recent:
            last = recent[-1] if isinstance(recent[-1], str) else str(recent[-1])
            return _make_result(
                intent="verify_last_action",
                confidence=0.90,
                inferred_target=last[:80],
                reasoning="Checking the most recent logged activity",
                slots={"last_activity": last[:80]},
                risk="safe",
                assumption=f"I'm checking the result of the last action: '{last[:60]}'.",
            )

        return _make_result(
            intent="run_diagnostics",
            confidence=0.90,
            inferred_target="system state",
            reasoning="No specific last action to verify; running diagnostics",
            slots={"action": "run_diagnostics"},
            risk="safe",
            assumption="Running diagnostics to check current system state.",
        )

    def _resolve_summarize(self, snap: dict) -> dict[str, Any]:
        screen_summary = snap.get("visible_screen_summary", "").strip()
        active_window = snap.get("active_window_title", "").strip()
        project_path = snap.get("focused_project_path", "")
        active_app = snap.get("active_app", "")

        if screen_summary or active_window:
            target = active_window or screen_summary[:60] or "active screen"
            return _make_result(
                intent="summarize_screen",
                confidence=0.92,
                inferred_target=target,
                reasoning=f"Summarizing active screen / window: {target[:60]}",
                slots={"action": "summarize_screen"},
                risk="safe",
                assumption=f"I'm summarizing what's currently on screen ({target[:50]}).",
            )

        if project_path:
            proj_name = snap.get("focused_project") or project_path.split("/")[-1]
            return _make_result(
                intent="summarize_project",
                confidence=0.92,
                inferred_target=proj_name,
                reasoning="No active screen content; summarizing current project",
                slots={"action": "list_files", "path": project_path},
                risk="safe",
                assumption=f"I'm summarizing the current project: {proj_name}.",
            )

        return _clarify("What would you like me to summarize? I don't see active content to summarize.", "summarize")

    def _resolve_open(self, text: str, snap: dict) -> dict[str, Any]:
        project = snap.get("focused_project", "").strip()
        project_path = snap.get("focused_project_path", "").strip()

        if "project" in text and project and project_path:
            return _make_result(
                intent="open_project",
                confidence=0.93,
                inferred_target=project,
                reasoning=f"Opening known focused project: {project}",
                slots={"action": "open_code_folder", "project_path": project_path},
                risk="safe",
                assumption=f"I'm opening the current project: {project}.",
            )

        active_window = snap.get("active_window_title", "").strip()
        if active_window and ("browser" in snap.get("active_app", "") or "firefox" in snap.get("active_app", "").lower() or "chrome" in snap.get("active_app", "").lower()):
            return _make_result(
                intent="open_active_url",
                confidence=0.90,
                inferred_target=active_window,
                reasoning="Active app is a browser; interpreting 'open it' as the current page",
                slots={"action": "open_url_raw", "url": ""},
                risk="safe",
                assumption=f"I'm treating 'it' as the browser tab: {active_window[:50]}.",
            )

        if project and project_path:
            return _make_result(
                intent="open_project",
                confidence=0.92,
                inferred_target=project,
                reasoning="Opening currently focused project",
                slots={"action": "open_code_folder", "project_path": project_path},
                risk="safe",
                assumption=f"I'm opening the active project: {project}.",
            )

        return _clarify("What should I open? I need a specific app, file, or project name.", "open")

    def _resolve_run(self, snap: dict) -> dict[str, Any]:
        project_path = snap.get("focused_project_path", "").strip()
        project = snap.get("focused_project", "").strip()
        next_action = snap.get("next_action", "").strip()

        if next_action and any(k in next_action.lower() for k in ("run", "test", "build", "start", "execute")):
            return _make_result(
                intent="run_next_action",
                confidence=0.88,
                inferred_target=next_action,
                reasoning="Next action in mission involves running/executing",
                slots={"command": next_action, "project_path": project_path},
                risk="medium",
                assumption=f"I'm running the current next action: '{next_action[:60]}'.",
            )

        if project_path:
            # Check for common run scripts
            package_json = Path(project_path) / "package.json"
            pyproject = Path(project_path) / "pyproject.toml"
            makefile = Path(project_path) / "Makefile"

            if package_json.exists():
                return _make_result(
                    intent="run_dev_server",
                    confidence=0.80,
                    inferred_target=f"{project} (npm start)",
                    reasoning="Node project detected via package.json",
                    slots={"command": "npm start", "project_path": project_path},
                    risk="medium",
                    assumption=f"I'm treating 'run it' as running the {project} dev server (npm start).",
                )
            if pyproject.exists() or (Path(project_path) / "setup.py").exists():
                return _make_result(
                    intent="run_python_project",
                    confidence=0.78,
                    inferred_target=f"{project} (python)",
                    reasoning="Python project detected",
                    slots={"project_path": project_path},
                    risk="medium",
                    assumption=f"I'm treating 'run it' as running the Python project: {project}.",
                )

        return _clarify("What should I run? I need a script name or command.", "run")

    def _resolve_ship(self, snap: dict) -> dict[str, Any]:
        project = snap.get("focused_project", "").strip()
        project_path = snap.get("focused_project_path", "").strip()
        git_changes = snap.get("git_has_changes", False)
        git_branch = snap.get("git_branch", "").strip()
        active_window = snap.get("active_window_title", "")

        has_deployment_context = any(k in active_window.lower() for k in
                                      ("vercel", "netlify", "heroku", "railway", "fly.io", "deploy"))

        if has_deployment_context:
            return _make_result(
                intent="inspect_deployment",
                confidence=0.83,
                inferred_target=active_window[:60],
                reasoning="Browser shows deployment dashboard",
                slots={"action": "summarize_screen"},
                risk="high",
                assumption=f"I'm treating 'ship it' as a deployment action on: {active_window[:50]}.",
            )

        if project and project_path and git_branch:
            target = f"{project} on branch {git_branch}"
            return _make_result(
                intent="ship_project",
                confidence=0.77,
                inferred_target=target,
                reasoning=f"Active project has git branch {git_branch}",
                slots={"project": project, "project_path": project_path, "branch": git_branch},
                risk="high",
                assumption=f"I'm treating 'ship it' as deploying {target}. This will push changes.",
            )

        return _make_result(
            intent="ship_unknown",
            confidence=0.50,
            inferred_target="",
            reasoning="No clear deployment context found",
            slots={},
            risk="high",
            clarifying_question="What should I ship? I don't see a clear deployment target.",
        )

    def _resolve_delete(self, snap: dict) -> dict[str, Any]:
        return _make_result(
            intent="delete_target",
            confidence=0.99,
            inferred_target="unknown — blocked for safety",
            reasoning="Delete commands always require explicit confirmation with a specific target",
            slots={},
            risk="dangerous",
            assumption="Deletion requires a specific target and explicit confirmation.",
        )

    def _resolve_clean(self, snap: dict) -> dict[str, Any]:
        project_path = snap.get("focused_project_path", "").strip()
        project = snap.get("focused_project", "").strip()

        if project_path:
            return _make_result(
                intent="clean_project",
                confidence=0.82,
                inferred_target=project or project_path,
                reasoning="Cleaning active project",
                slots={"action": "run_shell", "command": "find . -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null; echo done", "project_path": project_path},
                risk="medium",
                assumption=f"I'm treating 'clean this up' as cleaning cache files in {project or project_path}.",
            )
        return _clarify("What should I clean? I need a project or directory to target.", "clean")

    def _resolve_handle(self, snap: dict) -> dict[str, Any]:
        blockers = snap.get("blockers", [])
        errors = snap.get("recent_errors", [])
        next_action = snap.get("next_action", "").strip()

        if blockers:
            return _make_result(
                intent="address_blocker",
                confidence=0.80,
                inferred_target=blockers[0][:80],
                reasoning="Active blocker in mission state",
                slots={"blocker": blockers[0]},
                risk="medium",
                assumption=f"I'm treating 'handle it' as addressing the current blocker: '{blockers[0][:60]}'.",
            )
        if errors:
            err = errors[-1].get("description", "")[:80]
            return _make_result(
                intent="fix_recent_error",
                confidence=0.82,
                inferred_target=err,
                reasoning="Recent error found in activity log",
                slots={"error": err},
                risk="medium",
                assumption=f"I'm treating 'handle it' as the recent error: '{err[:60]}'.",
            )
        if next_action:
            return _make_result(
                intent="execute_next_action",
                confidence=0.82,
                inferred_target=next_action,
                reasoning="Handling the current next action",
                slots={"action": next_action},
                risk="medium",
                assumption=f"I'm treating 'handle it' as the next action: '{next_action[:60]}'.",
            )
        return _clarify("Handle what? I don't see a clear issue, blocker, or pending action.", "handle")

    def _resolve_prep(self, snap: dict) -> dict[str, Any]:
        project_path = snap.get("focused_project_path", "").strip()
        project = snap.get("focused_project", "").strip()
        next_action = snap.get("next_action", "").strip()

        if next_action:
            return _make_result(
                intent="prepare_next_action",
                confidence=0.92,
                inferred_target=next_action,
                reasoning="Preparing for the mission's next action",
                slots={"next_action": next_action},
                risk="safe",
                assumption=f"I'm prepping for the next action: '{next_action[:60]}'.",
            )
        if project_path:
            return _make_result(
                intent="prep_project",
                confidence=0.92,
                inferred_target=project or project_path,
                reasoning="Prepping the active project",
                slots={"project_path": project_path},
                risk="safe",
                assumption=f"I'm prepping {project or project_path} for work.",
            )
        return _clarify("Prep what? I need a specific project or task to prepare.", "prep")

    # ------------------------------------------------------------------
    # LLM-assisted resolution tier (thorough mode only)
    # ------------------------------------------------------------------

    def _llm_resolve(self, text: str, snap: dict) -> dict[str, Any] | None:
        """Call GPT-4o with world snapshot to resolve ambiguous commands."""
        try:
            from prometheus.infra.llm_router import get_planning_llm

            llm = get_planning_llm()
            if llm is None:
                return None

            safe_snap = _safe_snap_for_prompt(snap)
            system = (
                "You are a contextual intent resolver for Prometheus, a local desktop AI assistant. "
                "Given a vague command and world context, infer the specific intent.\n\n"
                "Return ONLY valid JSON matching this exact schema:\n"
                '{"intent": str, "confidence": float, "inferred_target": str, '
                '"reasoning_summary": str, "slots": {}, "risk": "safe"|"medium"|"high"|"dangerous", '
                '"should_execute": bool, "requires_confirmation": bool, '
                '"requires_clarification": bool, "clarifying_question": str|null, '
                '"user_facing_assumption": str}\n\n'
                "Risk levels: safe=reads/status, medium=writes/runs, high=deploy/push, dangerous=delete/destroy\n"
                "If intent is genuinely ambiguous, set requires_clarification=true and provide clarifying_question."
            )
            prompt = f"Command: {text}\n\nWorld context:\n{json.dumps(safe_snap, indent=2)[:2000]}"
            raw = llm.complete(prompt, system=system)
            return _parse_llm_resolution(raw)
        except Exception as exc:
            log_event("contextual_intent_llm_error", {"error": str(exc)[:120]})
            return None


def _safe_snap_for_prompt(snap: dict) -> dict:
    """Strip sensitive data from world snapshot before sending to LLM."""
    return {
        "current_mission": snap.get("current_mission", "")[:200],
        "active_goal": snap.get("active_goal", "")[:200],
        "next_action": snap.get("next_action", "")[:200],
        "subtasks": [t.get("description", "")[:80] for t in snap.get("subtasks", [])[:5]],
        "blockers": snap.get("blockers", [])[:3],
        "recent_activity": snap.get("recent_activity", [])[:5],
        "recent_errors": [
            {"kind": e.get("kind", ""), "description": e.get("description", "")[:80]}
            for e in snap.get("recent_errors", [])[:3]
        ],
        "active_window_title": snap.get("active_window_title", "")[:100],
        "active_app": snap.get("active_app", ""),
        "focused_project": snap.get("focused_project", ""),
        "focused_project_path": snap.get("focused_project_path", "")[:100],
        "git_branch": snap.get("git_branch", ""),
        "git_has_changes": snap.get("git_has_changes", False),
        "git_status_short": snap.get("git_status_short", "")[:200],
        "running_dev_servers": snap.get("running_dev_servers", [])[:3],
        "terminal_cwd": snap.get("terminal_cwd", ""),
    }


def _parse_llm_resolution(raw: str) -> dict[str, Any] | None:
    import re as _re
    try:
        m = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, _re.DOTALL)
        json_str = m.group(1) if m else raw.strip()
        start = json_str.find("{")
        end = json_str.rfind("}") + 1
        if start >= 0 and end > start:
            json_str = json_str[start:end]
        data = json.loads(json_str)
        # Validate required fields
        for field in ("intent", "confidence", "risk"):
            if field not in data:
                return None
        return data
    except Exception:
        return None


# ── Convenience function ──────────────────────────────────────────────────────

def resolve_command(
    command: str,
    snapshot: dict[str, Any] | None = None,
    *,
    mode: str = "fast",
) -> dict[str, Any] | None:
    """
    Convenience wrapper. If snapshot is None, builds one automatically.
    Returns None if the command is not vague or cannot be resolved.
    """
    if snapshot is None:
        try:
            from prometheus.context.world_model import build_world_snapshot
            snapshot = build_world_snapshot()
        except Exception:
            snapshot = {}
    return ContextualIntentResolver().resolve(command, snapshot, mode=mode)

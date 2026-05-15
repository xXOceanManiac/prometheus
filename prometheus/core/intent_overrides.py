"""
intent_overrides.py — Deterministic intent resolution extracted from realtime_client.py.

Standalone functions with no dependency on RealtimePrometheusClient state.
Called by RealtimePrometheusClient._direct_intent_override().
"""
from __future__ import annotations

from typing import Any

# ── App open aliases ──────────────────────────────────────────────────────────

_DIRECT_APPS: dict[str, str] = {
    "firefox": "firefox",
    "chrome": "chrome",
    "browser": "chrome",
    "google chrome": "chrome",
    "terminal": "terminal",
    "konsole": "terminal",
    "spotify": "spotify",
    "discord": "discord",
    "obsidian": "obsidian",
}

_APP_OPEN_VARIANTS: list[tuple[list[str], str]] = [
    (["vs code", "vscode", "visual studio code", "visual studio"], "code"),
    (["files", "file manager", "dolphin", "my files", "file explorer"], "files"),
    (["spotify"], "spotify"),
]

# ── Phrase registries ─────────────────────────────────────────────────────────

_MISSION_PHRASES = (
    "what are we working on",
    "what am i working on",
    "what's the current mission",
    "whats the current mission",
    "what is the current mission",
    "what's the current objective",
    "whats the current objective",
    "what is the current objective",
    "current mission",
    "mission status",
    "show mission status",
    "what's my mission",
    "whats my mission",
    "what's next",
    "whats next",
    "what is next",
    "what's blocked",
    "whats blocked",
    "what is blocked",
    "any blockers",
    "show blockers",
)

_TIME_PHRASES = (
    "what time is it",
    "what's the time",
    "whats the time",
    "current time",
    "tell me the time",
    "time please",
)

_SCREENSHOT_PHRASES = (
    "take a screenshot",
    "grab a screenshot",
    "screenshot",
    "capture the screen",
    "capture screenshot",
)

_WRAPUP_PHRASES = (
    "wrap up",
    "wrap it up",
    "end session",
    "that's it for today",
    "thats it for today",
    "i'm done",
    "im done",
    "call it a day",
    "end of day",
    "log off",
    "wrap up the session",
)

_SYSTEM_STATUS_PHRASES = (
    "remind me what i was working on",
    "what was i doing",
    "pull up my last session",
    "what have i been building",
    "what's running in the background",
    "whats running in the background",
    "any background tasks",
    "what are you working on",
    "is anything running",
)

_DIAGNOSTICS_PHRASES = (
    "run a diagnostic",
    "run diagnostics",
    "check your systems",
    "are you healthy",
    "self check",
    "self-check",
    "system status",
    "what's your status",
    "whats your status",
    "how are you doing",
    "check everything",
    "how much have i spent",
    "what's the cost so far",
    "whats the cost so far",
    "how much is this costing",
    "run health check",
    "health check",
)

_CONTEXT_AWARENESS_PHRASES = (
    "what are you working with",
    "what do you know",
    "what context do you have",
    "what are you aware of",
    "what do you have loaded",
)

_PRIORITIES_PHRASES = (
    "what should i focus on",
    "what's the priority",
    "whats the priority",
    "what are my priorities",
    "what should i work on",
    "what are we working on today",
)

_SEARCH_CODEBASE_PHRASES = (
    "search the codebase",
    "search codebase",
    "search the code for",
    "find in the codebase",
    "grep the code",
    "search the project for",
)

_SEARCH_CODEBASE_STRIP_PHRASES = (
    "search the codebase for",
    "search codebase for",
    "search the code for",
    "find in the codebase",
    "grep the code for",
    "search the project for",
    "search the codebase",
    "search codebase",
)

_GIT_PHRASES = (
    "check git",
    "what changed",
    "git status",
    "what files changed",
    "what's changed",
    "whats changed",
    "show me the diff",
)

_GIT_DIFF_PHRASES = (
    "diff",
    "what changed",
    "whats changed",
    "what's changed",
    "show me the diff",
)

_CODING_TASK_PHRASES = (
    "fix this bug",
    "fix the bug",
    "code this",
    "implement this",
    "build this",
    "write the code",
    "start a coding task",
    "run the coding agent",
    "code it up",
    "make the change",
    "build me a",
    "build me an",
    "create a website",
    "create a web",
    "create an app",
    "create a script",
    "make me a website",
    "make me a script",
    "spin me up",
    "spin up a",
    "write me a script",
    "write me a program",
    "write me a website",
    "write a script",
    "write a website",
    "write a program",
    "write an app",
    "create a program",
    "create a tool",
    "make a website",
    "make a script",
    "make an app",
    "put together a simple site",
    "put together a site",
    "code me a script",
    "code me a program",
    "write me some code",
    "write some code for me",
    "there's a bug in",
    "theres a bug in",
    "debug this for me",
    "debug this",
    "help me debug",
)

_CODING_STATUS_PHRASES = (
    "how's the coding task",
    "coding task status",
    "how's the code going",
    "is the agent done",
    "what's the coding agent doing",
    "did the agent finish",
    "get coding status",
)

_BUILD_START_PHRASES = (
    "start a build",
    "run the orchestrator",
    "build this feature",
    "orchestrate",
    "start the build pipeline",
    "run architect coder tester",
    "full build",
    "start build",
)

_BUILD_STATUS_PHRASES = (
    "how's the build",
    "build status",
    "is the build done",
    "did the build finish",
    "orchestrator status",
    "get build status",
    "how many tests are passing",
)

_SUMMARIZE_SCREEN_PHRASES = (
    "what's on my screen",
    "what is on my screen",
    "what am i looking at",
    "summarize the tab",
    "summarize the screen",
    "current tab",
    "screen right now",
)

_SCREEN_CONTEXT_PHRASES = (
    "what am i working on",
    "what are you tracking",
    "what project am i on",
    "what do i have open",
    "what's open",
    "what is open",
    "what windows do i have",
)

_XBOX_PHRASES = (
    "what am i watching",
    "what is on xbox",
    "what is playing on xbox",
    "what's on xbox",
    "what is on tv",
    "what's on tv",
    "what are you playing",
)

_SMART_ACTION_KEYWORDS = (
    "lights",
    "light ",
    "xbox",
    "netflix",
    "youtube",
    "spotify",
    "movie mode",
    "night mode",
    "work mode",
    "party mode",
)

_WEB_SEARCH_KEYWORDS = (
    "search for",
    "look up",
    "movies playing near me",
    "near me",
    "latest ",
    "today",
    "search the web",
)

_WORKSPACE_KEYWORDS = (
    "let's get to work",
    "lets get to work",
    "get to work",
    "open my workspace",
    "switch to",
    "work on ",
    "open project",
    "resume ",
    "continue ",
    "pick up where we left off",
)

_BACKGROUND_KEYWORDS = (
    "in the background",
    "when you get a chance",
    "background task",
    "run in the background",
    "do it in the background",
    "handle it in the background",
)

_VAULT_PHRASES = (
    "what do you remember about",
    "what do you know about",
    "do you remember",
    "check my vault",
    "look in my vault",
    "search my vault",
    "query my vault",
    "search the vault",
    "what's in my vault",
    "whats in my vault",
    "find in my vault",
    "from my vault",
    "in my vault",
    "vault knows",
    "look in my notes",
    "check my notes",
    "what did i write about",
    "search my memory",
    "what do i know about",
    "find my notes",
    "find my note",
)

_KNOWN_TARGETS = (
    "jarvis",
    "prometheus",
    "microschool",
    "tileworld",
    "lumen",
    "truth",
    "daemon",
)

_RESUME_PHRASES = (
    "continue previous work",
    "continue working on",
    "resume work on",
    "resume working on",
    "resume previous work",
    "restore workspace",
    "restore project",
    "open project",
    "open the project",
    "open my project",
)

# ── Calendar read phrases ─────────────────────────────────────────────────────

_CALENDAR_TODAY_PHRASES = (
    "what's on my calendar today",
    "whats on my calendar today",
    "what do i have today",
    "my schedule today",
    "what's on today",
    "whats on today",
    "show my calendar today",
    "today's calendar",
    "todays calendar",
    "calendar today",
)

_CALENDAR_TOMORROW_PHRASES = (
    "what do i have tomorrow",
    "what's on my calendar tomorrow",
    "whats on my calendar tomorrow",
    "tomorrow's schedule",
    "tomorrows schedule",
    "what's on tomorrow",
    "whats on tomorrow",
    "calendar tomorrow",
    "show me tomorrow",
)

_CALENDAR_NEXT_EVENT_PHRASES = (
    "what's my next event",
    "whats my next event",
    "next meeting",
    "next event",
    "what's coming up next",
    "whats coming up next",
    "what's coming up",
    "whats coming up",
    "when's my next meeting",
    "when is my next meeting",
    "what's my next meeting",
)

_CALENDAR_SUMMARIZE_PHRASES = (
    "summarize my day",
    "how does my day look",
    "how's my day looking",
    "hows my day looking",
    "what's my day like",
    "whats my day like",
    "what does my day look like",
    "daily summary",
    "day summary",
)

_CALENDAR_FREE_BLOCK_PHRASES = (
    "do i have a free hour",
    "do i have free time today",
    "when am i free",
    "when am i free today",
    "find a free block",
    "any free time today",
    "free time today",
    "free blocks today",
    "when's a good time today",
    "whens a good time today",
    "first hard commitment",
    "when is my first meeting",
    "when's my first meeting",
)

_SHOW_LOGS_PHRASES = (
    "show logs",
    "show me the logs",
    "show the logs",
    "check the logs",
    "check logs",
    "view logs",
    "view the logs",
    "recent logs",
    "latest logs",
    "what's in the logs",
    "whats in the logs",
    "any errors in the logs",
    "what are the logs saying",
    "show recent logs",
    "pull up the logs",
    "read the logs",
    "show me recent activity",
    "what happened recently",
    "show activity",
    "activity log",
)

# Calendar create — NL scheduling phrases (triggers parse_and_propose)
_CALENDAR_CREATE_PHRASES = (
    "schedule ",      # broad: "schedule a meeting", "schedule workout", "schedule church..."
    "block off ",
    "book a ",
    "book an ",
    "create a meeting",
    "create an event",
    "create a session",
    "create a block",
    "set up a meeting",
    "set up a call",
    "set up a sync",
    "add a meeting",
    "add a session",
    "add a workout",
    "add a standup",
    "add a sync",
    "add a block",
    "add a focus",
    "add a call",
    "add a lunch",
    "add a 1:1",
    "add an event",
    "add an appointment",
    "put a meeting",
    "put a session",
    "put a workout",
    "put a standup",
    "put an event",
    "put it on my calendar",
    "put that on my calendar",
    "add it to my calendar",
    "add to my calendar",
    "put on my calendar",
    "make a meeting",
    "make a call",
    "make a session",
    "make a sync",
    "make an appointment",
    "log a ",
    "plan a ",
)

# Verbs that, combined with "on my calendar" / "to my calendar", mean calendar create
_CALENDAR_CREATE_VERBS = ("put ", "add ", "schedule ", "book ", "create ", "log ", "plan ")

# Calendar confirm — only routed when a pending confirmation exists
_CALENDAR_CONFIRM_PHRASES = (
    "yes",
    "confirm",
    "do it",
    "go ahead",
    "sounds good",
    "looks good",
    "add it",
    "yeah",
    "yep",
    "yes please",
    "let's do it",
    "lets do it",
    "go for it",
    "approved",
    "execute it",
    "run it",
    "that works",
    "that's good",
    "thats good",
    "put it on",
    "add that",
    "do that",
)

# Calendar cancel — only routed when a pending confirmation exists
_CALENDAR_CANCEL_PHRASES = (
    "no",
    "cancel",
    "never mind",
    "forget it",
    "don't add it",
    "don't add that",
    "nevermind",
    "nope",
    "no thanks",
    "skip it",
    "cancel that",
    "cancel it",
    "not now",
    "discard",
    "abort",
)


# ── Standalone intent resolvers ───────────────────────────────────────────────

def resolve_project_resume(transcript: str, text: str) -> dict[str, Any] | None:
    """Pure function version of _project_resume_override() — no self required."""
    projectish = (
        "project" in text
        or "workspace" in text
        or any(token in text for token in _KNOWN_TARGETS)
    )

    if projectish and any(phrase in text for phrase in _RESUME_PHRASES):
        return {
            "type": "direct_tool",
            "payload": {
                "action": "resume_last_context",
                "query": transcript,
                "request_text": transcript,
            },
        }

    if projectish and text.startswith("open "):
        return {
            "type": "direct_tool",
            "payload": {
                "action": "resume_last_context",
                "query": transcript,
                "request_text": transcript,
            },
        }

    return None


def resolve_direct_intent(transcript: str) -> dict[str, Any] | None:
    """
    Pure function version of _direct_intent_override() — no self required.
    Returns a routing dict or None if no deterministic override applies.
    """
    text = " ".join(str(transcript).strip().lower().split())

    if not text:
        return None

    if any(p in text for p in _MISSION_PHRASES):
        return {
            "type": "direct_tool",
            "payload": {"action": "get_mission_status", "request_text": transcript},
        }

    if any(p in text for p in _TIME_PHRASES):
        return {
            "type": "direct_tool",
            "payload": {"action": "tell_time", "request_text": transcript},
        }

    if any(p in text for p in _SCREENSHOT_PHRASES):
        return {
            "type": "direct_tool",
            "payload": {"action": "screenshot", "request_text": transcript},
        }

    for alias, app_key in _DIRECT_APPS.items():
        if f"open {alias}" in text or text == f"launch {alias}" or text.startswith(f"open {alias} "):
            return {
                "type": "direct_tool",
                "payload": {"action": "open_app", "app": app_key, "request_text": transcript},
            }

    if any(p in text for p in _WRAPUP_PHRASES):
        return {
            "type": "direct_tool",
            "payload": {"action": "session_wrapup", "request_text": transcript},
        }

    if any(p in text for p in _SYSTEM_STATUS_PHRASES):
        return {
            "type": "direct_tool",
            "payload": {"action": "system_status", "request_text": transcript},
        }

    if any(p in text for p in _DIAGNOSTICS_PHRASES):
        return {
            "type": "direct_tool",
            "payload": {"action": "run_diagnostics", "request_text": transcript},
        }

    if any(p in text for p in _CONTEXT_AWARENESS_PHRASES):
        return {
            "type": "direct_tool",
            "payload": {"action": "system_status", "request_text": transcript},
        }

    if any(p in text for p in _PRIORITIES_PHRASES):
        return {
            "type": "direct_tool",
            "payload": {"action": "get_priorities", "request_text": transcript},
        }

    if any(p in text for p in _SEARCH_CODEBASE_PHRASES):
        query = text
        for phrase in _SEARCH_CODEBASE_STRIP_PHRASES:
            if phrase in text:
                query = text.split(phrase, 1)[-1].strip()
                break
        return {
            "type": "direct_tool",
            "payload": {
                "action": "search_codebase",
                "query": query,
                "request_text": transcript,
            },
        }

    if any(p in text for p in _GIT_PHRASES):
        action = (
            "git_diff"
            if any(p in text for p in _GIT_DIFF_PHRASES)
            else "git_status"
        )
        return {
            "type": "direct_tool",
            "payload": {"action": action, "request_text": transcript},
        }

    if any(p in text for p in _CODING_TASK_PHRASES):
        return {
            "type": "direct_tool",
            "payload": {
                "action": "start_coding_task",
                "goal": transcript,
                "request_text": transcript,
            },
        }

    if any(p in text for p in _CODING_STATUS_PHRASES):
        return {
            "type": "direct_tool",
            "payload": {"action": "get_coding_status", "request_text": transcript},
        }

    if any(p in text for p in _BUILD_START_PHRASES):
        return {
            "type": "direct_tool",
            "payload": {
                "action": "start_build",
                "goal": transcript,
                "request_text": transcript,
            },
        }

    if any(p in text for p in _BUILD_STATUS_PHRASES):
        return {
            "type": "direct_tool",
            "payload": {"action": "get_build_status", "request_text": transcript},
        }

    if any(p in text for p in _SUMMARIZE_SCREEN_PHRASES):
        return {
            "type": "direct_tool",
            "payload": {"action": "summarize_screen", "request_text": transcript},
        }

    if any(p in text for p in _SCREEN_CONTEXT_PHRASES):
        return {
            "type": "direct_tool",
            "payload": {"action": "screen_context", "request_text": transcript},
        }

    if any(p in text for p in _XBOX_PHRASES):
        return {
            "type": "direct_tool",
            "payload": {"action": "screen_context", "request_text": transcript},
        }

    for variants, canonical in _APP_OPEN_VARIANTS:
        if any(
            f"open {v}" in text or text.startswith(f"launch {v}")
            for v in variants
        ):
            return {
                "type": "direct_tool",
                "payload": {
                    "action": "open_app",
                    "app": canonical,
                    "request_text": transcript,
                },
            }

    if any(k in text for k in _SMART_ACTION_KEYWORDS):
        return {
            "type": "direct_tool",
            "payload": {"action": "smart_action", "request_text": transcript},
        }

    # Calendar confirm/cancel — checked BEFORE create so "yes" with pending
    # confirmation doesn't fall through to LLM or create a duplicate proposal.
    # Only active when a pending calendar confirmation exists in the filesystem.
    if any(p == text or text.startswith(p) for p in _CALENDAR_CONFIRM_PHRASES):
        try:
            from prometheus.agents.calendar_create_flow import has_pending_calendar_confirmation
            if has_pending_calendar_confirmation():
                return {
                    "type": "direct_tool",
                    "payload": {"action": "calendar_confirm_create", "request_text": transcript},
                }
        except Exception:
            pass

    if any(p == text or text.startswith(p) for p in _CALENDAR_CANCEL_PHRASES):
        try:
            from prometheus.agents.calendar_create_flow import has_pending_calendar_confirmation
            if has_pending_calendar_confirmation():
                return {
                    "type": "direct_tool",
                    "payload": {"action": "calendar_cancel_create", "request_text": transcript},
                }
        except Exception:
            pass

    # Compound calendar create: "put church meeting on my calendar Sunday at 10"
    # Catches any "<verb> X on/to my calendar" pattern not covered by phrase list.
    if any(
        marker in text
        for marker in ("on my calendar", "to my calendar", "on the calendar")
    ) and any(text.startswith(v) for v in _CALENDAR_CREATE_VERBS):
        return {
            "type": "direct_tool",
            "payload": {
                "action": "calendar_create_proposal",
                "user_request": transcript,
                "request_text": transcript,
            },
        }

    # Calendar create — NL scheduling requests
    if any(p in text for p in _CALENDAR_CREATE_PHRASES):
        return {
            "type": "direct_tool",
            "payload": {
                "action": "calendar_create_proposal",
                "user_request": transcript,
                "request_text": transcript,
            },
        }

    # Calendar reads — checked before web_search so "today"/"tomorrow" keywords
    # don't accidentally route calendar questions to web_search.
    if any(p in text for p in _CALENDAR_TODAY_PHRASES):
        return {
            "type": "direct_tool",
            "payload": {"action": "calendar_get_today", "request_text": transcript},
        }

    if any(p in text for p in _CALENDAR_TOMORROW_PHRASES):
        return {
            "type": "direct_tool",
            "payload": {"action": "calendar_get_tomorrow", "request_text": transcript},
        }

    if any(p in text for p in _CALENDAR_NEXT_EVENT_PHRASES):
        return {
            "type": "direct_tool",
            "payload": {"action": "calendar_next_event", "request_text": transcript},
        }

    if any(p in text for p in _CALENDAR_SUMMARIZE_PHRASES):
        return {
            "type": "direct_tool",
            "payload": {"action": "calendar_summarize_day", "request_text": transcript},
        }

    if any(p in text for p in _CALENDAR_FREE_BLOCK_PHRASES):
        from datetime import date as _date
        today = _date.today().isoformat()
        return {
            "type": "direct_tool",
            "payload": {
                "action": "calendar_find_free_blocks",
                "date": today,
                "minimum_minutes": 60,
                "request_text": transcript,
            },
        }

    if any(p in text for p in _SHOW_LOGS_PHRASES):
        return {
            "type": "direct_tool",
            "payload": {"action": "show_logs", "request_text": transcript},
        }

    if any(k in text for k in _WEB_SEARCH_KEYWORDS):
        return {
            "type": "direct_tool",
            "payload": {
                "action": "web_search",
                "query": transcript,
                "request_text": transcript,
            },
        }

    if any(k in text for k in _WORKSPACE_KEYWORDS):
        return {
            "type": "direct_tool",
            "payload": {"action": "smart_action", "request_text": transcript},
        }

    if any(k in text for k in _BACKGROUND_KEYWORDS):
        return {
            "type": "direct_tool",
            "payload": {
                "action": "background_task",
                "description": transcript,
                "request_text": transcript,
            },
        }

    if any(p in text for p in _VAULT_PHRASES):
        return {"type": "vault_recall", "query": transcript}

    return None

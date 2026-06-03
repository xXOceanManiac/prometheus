"""
proactive_loop.py — Ambient awareness loop for Prometheus.

Runs every 90 seconds and uses an LLM to decide whether anything is worth
surfacing to the user. Skips if the client is busy. Never crashes.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from utils import log_event
from working_memory import WorkingMemory


def _time_of_day_label() -> str:
    hour = int(time.strftime("%H"))
    if 5 <= hour < 12:
        return "morning"
    elif 12 <= hour < 17:
        return "afternoon"
    elif 17 <= hour < 21:
        return "evening"
    else:
        return "night"


class ProactiveLoop:
    """
    Ambient awareness loop — polls every 90 seconds.
    Uses an LLM to decide if there is anything genuinely worth surfacing.
    """

    _INTERVAL_SECONDS = 90.0
    _COOLDOWN_SECONDS = 600.0  # 10 minutes per category

    def __init__(self, client: Any, workspace_manager: Any) -> None:
        self._client = client
        self._workspace = workspace_manager
        self._stopped = False
        self._last_voice_at: float = time.time()
        # category → last_surfaced_timestamp
        self._last_surfaced: dict[str, float] = {}

    def stop(self) -> None:
        """Signal the loop to exit at the next opportunity."""
        self._stopped = True

    def note_voice_activity(self) -> None:
        """Record that the user spoke. Called from main._begin_turn()."""
        self._last_voice_at = time.time()

    async def run(self) -> None:
        """Main loop — runs until stop() is called."""
        while not self._stopped:
            try:
                await asyncio.sleep(self._INTERVAL_SECONDS)
                if self._stopped:
                    break
                await self._cycle()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log_event("proactive_loop_error", {"error": str(exc)[:200]})

    # ------------------------------------------------------------------
    # Per-cycle logic
    # ------------------------------------------------------------------

    async def _cycle(self) -> None:
        """Execute one proactive awareness cycle."""
        client = self._client
        connected = getattr(client, "connected", False) if client else False
        busy = getattr(client, "busy", False)
        listening = getattr(client, "awaiting_user_audio", False)
        seconds_since_voice = time.time() - self._last_voice_at

        log_event("proactive_loop_cycle", {
            "connected": connected,
            "busy": busy,
            "listening": listening,
            "seconds_since_voice": int(seconds_since_voice),
        })

        if not client or not connected:
            return
        if busy:
            return
        if listening:
            return
        if seconds_since_voice < 30:
            return

        context = await asyncio.get_running_loop().run_in_executor(
            None, lambda: self._build_context(seconds_since_voice)
        )

        decision = await asyncio.get_running_loop().run_in_executor(
            None, lambda: self._llm_decide(context)
        )

        if not decision:
            log_event("proactive_loop_no_decision", {"reason": "llm_unavailable_or_no_output"})
            return

        should_surface = bool(decision.get("should_surface", False))
        if not should_surface:
            log_event("proactive_loop_skip", {"reason": decision.get("reason", "")[:100]})
            return

        message = str(decision.get("message") or "").strip()
        reason = str(decision.get("reason") or "general").strip()

        if not message:
            return

        # Cooldown check by category
        category = self._extract_category(reason)
        last_ts = self._last_surfaced.get(category, 0.0)
        if (time.time() - last_ts) < self._COOLDOWN_SECONDS:
            log_event("proactive_loop_cooldown", {"category": category, "reason": reason[:80]})
            return

        # Final check — still idle?
        if getattr(client, "busy", False) or getattr(client, "awaiting_user_audio", False):
            return

        self._last_surfaced[category] = time.time()
        await self._surface(message)

    def _build_context(self, seconds_since_voice: float) -> dict:
        """Read all state sources and assemble context dict."""
        vs: dict = {}
        try:
            vs_path = Path.home() / ".jarvis" / "visual_state.json"
            if vs_path.exists():
                vs = json.loads(vs_path.read_text(encoding="utf-8"))
        except Exception:
            pass

        bt: list[dict] = []
        try:
            bt_path = Path.home() / ".jarvis" / "background_tasks.json"
            if bt_path.exists():
                raw = json.loads(bt_path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    bt = [
                        t for t in raw
                        if isinstance(t, dict) and t.get("status") == "completed"
                    ][-3:]
        except Exception:
            pass

        wm: dict = {}
        try:
            wm = WorkingMemory().read()
        except Exception:
            pass

        # Recent tool actions from working memory
        recent_tools: list[str] = []
        last_tool = str(wm.get("last_tool_action") or "").strip()
        if last_tool:
            recent_tools.append(last_tool)

        # Orchestration build result
        build_result: dict = {}
        try:
            raw_build = wm.get("last_orchestration_result")
            if isinstance(raw_build, dict) and raw_build.get("status") == "complete":
                build_result = raw_build
        except Exception:
            pass

        # Active window title
        active_window = vs.get("active_window") or {}
        if isinstance(active_window, dict):
            win_title = str(active_window.get("title") or "").strip()
        else:
            win_title = str(active_window)

        # Open windows list
        open_windows = vs.get("open_windows") or []
        if isinstance(open_windows, list):
            open_apps = [str(w) for w in open_windows if str(w).strip()]
        else:
            open_apps = []

        return {
            "active_project": str(
                vs.get("active_project")
                or vs.get("active_project_name")
                or wm.get("active_workspace")
                or ""
            ).strip(),
            "active_window": win_title,
            "time_of_day": _time_of_day_label(),
            "completed_background_tasks": bt,
            "seconds_since_last_voice": int(seconds_since_voice),
            "open_apps": open_apps[:10],
            "xbox_state": vs.get("xbox_state"),
            "recent_tool_actions": recent_tools[:3],
            "last_build_result": build_result,
        }

    def _llm_decide(self, context: dict) -> dict | None:
        """
        Ask LLM whether to surface something.
        Returns parsed JSON dict or None on any failure.
        """
        try:
            from llm_router import get_planning_llm
            import json as _json

            llm = get_planning_llm()
            if llm is None:
                return None

            prompt = (
                "You are Prometheus's awareness engine. Given the current context, "
                "decide if there is something genuinely worth surfacing to Tate right now. "
                "Be conservative — only surface something if it is clearly useful and timely.\n\n"
                f"Context: {_json.dumps(context)}\n\n"
                "Return JSON only (no markdown):\n"
                "{\"should_surface\": true/false, "
                "\"message\": \"short spoken message if should_surface is true\", "
                "\"reason\": \"internal reason\"}\n\n"
                "Rules:\n"
                "- Only return should_surface=true if something genuinely useful would be said\n"
                "- Never surface generic observations\n"
                "- Never surface anything in the same category as something surfaced within the last 10 minutes\n"
                "- Only surface if seconds_since_last_voice >= 30\n"
                "- Background task completed: worth surfacing if output_path or result_summary is new\n"
                "- last_build_result.status == 'complete' and seconds_since_last_voice > 60: surface build outcome once\n"
                "- Build succeeded: say 'Build complete. N tests passing.'\n"
                "- Build needs_human: say 'Build hit the debug limit. Your review is needed.'\n"
                "- Same file open 2+ hours: worth surfacing\n"
                "- Evening with no wrap-up: worth surfacing once\n"
                "- Relevant vault connection to current window: worth surfacing once per session\n"
                "- Status updates Tate didn't ask for: never worth surfacing"
            )

            raw = llm.complete(prompt, system="You are a conservative ambient assistant decision engine.")
            if not raw:
                return None

            # Parse JSON from response
            import re
            raw = raw.strip()
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if m:
                return _json.loads(m.group(0))
            return None

        except Exception as exc:
            log_event("proactive_loop_llm_error", {"error": str(exc)[:200]})
            return None

    def _extract_category(self, reason: str) -> str:
        """Extract a coarse category from the LLM reason string."""
        reason_lower = reason.lower()
        if "background" in reason_lower or "task" in reason_lower:
            return "background_task"
        if "evening" in reason_lower or "wrap" in reason_lower:
            return "evening_wrapup"
        if "file" in reason_lower or "window" in reason_lower:
            return "window_context"
        if "vault" in reason_lower or "memory" in reason_lower:
            return "vault_connection"
        if "xbox" in reason_lower or "media" in reason_lower:
            return "media"
        return reason[:40] if reason else "general"

    async def _surface(self, message: str) -> None:
        """Send a proactive message via the Realtime client."""
        try:
            client = self._client
            if not client or not client.connected:
                return

            await client.send({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "system",
                    "content": [
                        {
                            "type": "input_text",
                            "text": f"[PROACTIVE] {message}",
                        }
                    ],
                },
            })
            await client.send({
                "type": "response.create",
                "response": {
                    "instructions": f"Say exactly: {message}",
                },
            })
            log_event("proactive_surfaced", {"message": message[:120]})

        except Exception as exc:
            log_event("proactive_surface_error", {"error": str(exc)[:200]})

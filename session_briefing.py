"""
session_briefing.py — Startup briefing and session history loader for Prometheus.

Fires a contextual spoken briefing 3 seconds after session start.
Cancellable if the user speaks first. Never raises.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from config import CONFIG
from utils import log_event
from working_memory import WorkingMemory

if TYPE_CHECKING:
    from realtime_client import RealtimePrometheusClient


class SessionBriefing:
    """
    Fires a contextual startup briefing 3 seconds after session start.
    Cancel with .cancel() when the user speaks — briefing is skipped silently.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self._cancelled = False

    def cancel(self) -> None:
        """Cancel a pending briefing. Safe to call multiple times."""
        self._cancelled = True

    # ------------------------------------------------------------------
    # Session loader
    # ------------------------------------------------------------------

    @staticmethod
    def load_recent_sessions(n: int = 3) -> list[dict]:
        """
        Load the last n session markdown files from the vault Sessions directory.

        Returns list of dicts: {title, date, active_project, text (first 400 chars of body)}.
        Returns [] if vault_path not configured or directory missing. Never raises.
        """
        try:
            vault_path_str = str(CONFIG.get("vault_path", "")).strip()
            if not vault_path_str:
                return []

            vault_path = Path(vault_path_str).expanduser()
            sessions_root = vault_path / "vault" / "Sessions"
            if not sessions_root.is_dir():
                return []

            # Find most recent year directory
            year_dirs = sorted(
                [d for d in sessions_root.iterdir() if d.is_dir() and d.name.isdigit()],
                reverse=True,
            )
            if not year_dirs:
                return []

            session_files = sorted(year_dirs[0].glob("*.md"), reverse=True)[:n]
            results: list[dict] = []

            for f in session_files:
                try:
                    text = f.read_text(encoding="utf-8", errors="ignore")
                    frontmatter: dict[str, str] = {}
                    body = text

                    if text.startswith("---"):
                        end = text.find("---", 3)
                        if end > 0:
                            fm_text = text[3:end]
                            body = text[end + 3:].strip()
                            for line in fm_text.splitlines():
                                if ":" in line:
                                    key, _, val = line.partition(":")
                                    frontmatter[key.strip().lower()] = val.strip().strip('"').strip("'")

                    title = frontmatter.get("title") or f.stem
                    date = frontmatter.get("date") or ""
                    active_project = frontmatter.get("active_project") or ""
                    body_excerpt = body[:400].strip()

                    results.append({
                        "title": title,
                        "date": date,
                        "active_project": active_project,
                        "text": body_excerpt,
                    })
                except Exception:
                    continue

            return results

        except Exception:
            return []

    # ------------------------------------------------------------------
    # Briefing fire
    # ------------------------------------------------------------------

    async def fire_delayed(self, delay: float = 3.0) -> None:
        """
        Wait delay seconds, then fire a startup briefing via the Realtime client.
        Cancellable at every await point. Never raises.
        """
        try:
            await asyncio.sleep(delay)
            if self._cancelled:
                return

            client = self._client
            if not client or not getattr(client, "connected", False):
                return

            # Wait up to 5 more seconds if client is busy
            waited = 0.0
            while getattr(client, "busy", False) or getattr(client, "awaiting_user_audio", False):
                if self._cancelled:
                    return
                await asyncio.sleep(0.5)
                waited += 0.5
                if waited >= 5.0:
                    return

            if self._cancelled:
                return

            # Gather context
            recent_sessions = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self.load_recent_sessions(3)
            )
            if self._cancelled:
                return

            background_tasks: list[dict] = []
            try:
                bt_path = Path.home() / ".jarvis" / "background_tasks.json"
                if bt_path.exists():
                    bt_data = json.loads(bt_path.read_text(encoding="utf-8"))
                    if isinstance(bt_data, list):
                        background_tasks = [
                            t for t in bt_data if isinstance(t, dict)
                            and t.get("status") in ("completed", "pending", "running")
                        ][-3:]
            except Exception:
                pass

            wm_data = WorkingMemory().read()
            next_session_context = str(wm_data.get("next_session_context") or "").strip()

            vs_data: dict = {}
            try:
                vs_path = Path.home() / ".jarvis" / "visual_state.json"
                if vs_path.exists():
                    vs_data = json.loads(vs_path.read_text(encoding="utf-8"))
            except Exception:
                pass

            active_project = str(
                vs_data.get("active_project")
                or vs_data.get("active_project_name")
                or wm_data.get("active_workspace")
                or ""
            ).strip()

            briefing_context = {
                "recent_sessions": recent_sessions[:2],
                "background_tasks": background_tasks,
                "next_session_context": next_session_context,
                "active_project": active_project,
                "time_of_day": _time_of_day_label(),
                "hour": int(time.strftime("%H")),
            }

            if self._cancelled:
                return

            # Generate briefing text
            briefing_text = await asyncio.get_running_loop().run_in_executor(
                None, lambda: _generate_briefing(briefing_context)
            )

            if self._cancelled:
                return

            if not client.connected:
                return

            # Send via Realtime API
            await client.send({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "system",
                    "content": [
                        {
                            "type": "input_text",
                            "text": f"[SESSION_BRIEFING] {briefing_text}",
                        }
                    ],
                },
            })

            if self._cancelled:
                return

            await client.send({
                "type": "response.create",
                "response": {
                    "instructions": f"Speak this briefing exactly as given. One breath. No additions: {briefing_text}",
                },
            })

            log_event("briefing_generated", {
                "length": len(briefing_text),
                "snippet": briefing_text[:120],
                "has_prev_context": bool(next_session_context),
            })

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log_event("briefing_error", {"error": str(exc)[:200]})


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

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


def _generate_briefing(context: dict) -> str:
    """
    Generate briefing text via LLM, with template fallback.
    Never raises — always returns a non-empty string.
    """
    try:
        from llm_router import get_llm

        llm = get_llm("planning")
        if llm is not None:
            prompt = (
                f"You are generating a spoken startup briefing for Prometheus (voice assistant).\n"
                f"Tate is starting a session. Context:\n{json.dumps(context, indent=2)}\n\n"
                f"Rules:\n"
                f"- Maximum 4 sentences\n"
                f"- Be specific: use actual project names, actual completed tasks, actual issues\n"
                f'- End with: "Want to continue where we left off, or something else?"\n'
                f"- If no session history: say \"Ready to work. What are we building?\" (1 sentence)\n"
                f"- If next_session_context exists, lead with that\n"
                f"- Never start with \"Good morning\" or similar generic greetings\n"
                f"- Mention current time of day only if it's morning (before noon)\n"
            )
            result = llm.complete(prompt, system="You write concise spoken startup briefings.")
            if result and result.strip():
                return result.strip()
    except Exception:
        pass

    # Template fallback
    return _template_briefing(context)


def _template_briefing(context: dict) -> str:
    """Construct a briefing from templates — no LLM dependency."""
    next_ctx = str(context.get("next_session_context") or "").strip()
    sessions = context.get("recent_sessions") or []
    active_project = str(context.get("active_project") or "").strip()

    if next_ctx:
        return f"Last session: {next_ctx}. Continue or something else?"

    if sessions:
        first = sessions[0] if isinstance(sessions[0], dict) else {}
        project = str(first.get("active_project") or first.get("title") or active_project or "the last project")
        if project and project != "unknown":
            return f"Last time you worked on {project}. Continue or something else?"

    if active_project and active_project != "unknown":
        return f"Active project is {active_project}. Ready when you are."

    return "Ready to work. What are we building?"

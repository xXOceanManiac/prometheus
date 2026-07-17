from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from prometheus.infra.config import CONFIG
from prometheus.memory.episodic_memory import EpisodicMemory
from prometheus.memory.memory_core import now_iso
from prometheus.infra.utils import log_event
from prometheus.memory.working_memory import WorkingMemory


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

                results.append({
                    "title": frontmatter.get("title") or f.stem,
                    "date": frontmatter.get("date") or "",
                    "active_project": frontmatter.get("active_project") or "",
                    "text": body[:400].strip(),
                })
            except Exception:
                continue

        return results

    except Exception:
        return []


class SessionSummarizer:
    """
    Writes a structured markdown session summary to the Obsidian vault at shutdown.
    All methods fail silently — never block shutdown.
    """

    def summarize_and_write(self) -> bool:
        """
        Read WorkingMemory + EpisodicMemory, generate a session summary, and write
        it to CONFIG['vault_path']/vault/Sessions/YYYY/YYYY-MM-DD_HH-MM_<project>.md.

        Returns True on success, False on any failure.  Never raises.
        """
        try:
            vault_path_str = str(CONFIG.get("vault_path", "")).strip()
            if not vault_path_str:
                log_event("session_summarizer_skip", {"reason": "vault_path not configured"})
                return False

            vault_path = Path(vault_path_str).expanduser()
            if not vault_path.is_dir():
                log_event(
                    "session_summarizer_skip",
                    {"reason": f"vault_path not found: {vault_path}"},
                )
                return False

            wm = WorkingMemory().read()
            episodes = EpisodicMemory().tail(limit=50)

            project = str(
                wm.get("active_workspace")
                or wm.get("active_context_name")
                or "unknown"
            )
            vault_context = wm.get("vault_context") or []
            vault_titles = [
                str(r.get("title", ""))
                for r in vault_context
                if isinstance(r, dict) and r.get("title")
            ]

            tools_used: list[str] = []
            last_tool = str(wm.get("last_tool_action") or "")
            if last_tool:
                tools_used.append(last_tool)
            for ep in episodes:
                kind = ep.get("kind", "")
                if "tool" in kind or "action" in kind:
                    s = ep.get("summary", "")
                    if s and s not in tools_used:
                        tools_used.append(s)

            now = time.localtime()
            date_str = time.strftime("%Y-%m-%d", now)
            time_str = time.strftime("%H-%M", now)
            year_str = time.strftime("%Y", now)

            summary_body = self._generate_summary(wm, episodes, project)

            vault_titles_yaml = (
                "\n".join(f'  - "{t}"' for t in vault_titles[:10])
                if vault_titles
                else "  []"
            )
            tools_yaml = (
                "\n".join(f'  - "{t}"' for t in tools_used[:20])
                if tools_used
                else "  []"
            )

            doc = (
                f"---\n"
                f"date: {date_str}\n"
                f"time: {time_str.replace('-', ':')}\n"
                f"active_project: {project}\n"
                f"vault_context_used:\n{vault_titles_yaml}\n"
                f"tools_executed:\n{tools_yaml}\n"
                f"---\n\n"
                f"# Session — {date_str} {time_str.replace('-', ':')} — {project}\n\n"
                f"{summary_body}\n"
            )

            safe_project = "".join(
                c if (c.isalnum() or c in "-_.") else "_" for c in project
            )
            filename = f"{date_str}_{time_str}_{safe_project}.md"
            out_dir = vault_path / "vault" / "Sessions" / year_str
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / filename

            out_path.write_text(doc, encoding="utf-8")
            log_event(
                "session_summary_written",
                {"path": str(out_path), "project": project},
            )
            return True

        except Exception as exc:
            log_event("session_summarizer_error", {"error": str(exc)})
            return False

    def _generate_summary(
        self,
        wm: dict[str, Any],
        episodes: list[dict[str, Any]],
        project: str,
    ) -> str:
        try:
            from prometheus.infra.llm_router import get_planning_llm

            llm = get_planning_llm()
            if llm is None:
                return self._template_summary(wm, episodes)

            recent = episodes[-20:]
            ep_lines = "\n".join(
                f"- [{e.get('kind', '')}] {e.get('summary', '')}"
                for e in recent
                if e.get("summary")
            )
            last_request = str(wm.get("last_user_request") or "")
            last_response = str(wm.get("last_response_text") or "")

            prompt = (
                f"Active project: {project}\n"
                f"Last user request: {last_request}\n"
                f"Last assistant response: {last_response}\n\n"
                f"Recent activity:\n{ep_lines or '(none recorded)'}\n\n"
                "Write a concise session summary (3-5 sentences). "
                "Focus on what was accomplished, what was attempted, and what state was left in. "
                "Be factual and brief. No filler."
            )
            return llm.complete(
                prompt, system="You write precise technical session summaries."
            )
        except Exception:
            return self._template_summary(wm, episodes)

    def _template_summary(
        self,
        wm: dict[str, Any],
        episodes: list[dict[str, Any]],
    ) -> str:
        last_request = str(wm.get("last_user_request") or "(none)")
        last_response = str(wm.get("last_response_text") or "(none)")
        last_tool = str(wm.get("last_tool_action") or "(none)")

        recent = [
            f"- [{e.get('kind', '')}] {e.get('summary', '')}"
            for e in episodes[-10:]
            if e.get("summary")
        ]
        ep_block = "\n".join(recent) if recent else "*(no activity recorded)*"

        return (
            f"## Summary\n\n"
            f"**Last request:** {last_request}\n\n"
            f"**Last response:** {last_response}\n\n"
            f"**Last tool used:** {last_tool}\n\n"
            f"## Recent Activity\n\n"
            f"{ep_block}\n"
        )

    def trigger_wrapup(self, client: Any = None) -> bool:
        """
        Full session wrap-up: summarize session, write to vault, update next_session_context.
        Optionally speak a summary via client (RealtimePrometheusClient).
        Returns True on success, False on failure. Never raises.
        """
        try:
            ok = self.summarize_and_write()

            wm_obj = WorkingMemory()
            wm = wm_obj.read()
            episodes = EpisodicMemory().tail(limit=50)
            project = str(
                wm.get("active_workspace") or wm.get("active_context_name") or "the current project"
            )

            spoken_summary = self._build_spoken_summary(wm, episodes, project)

            # Write next_session_context
            last_req = str(wm.get("last_user_request") or wm.get("last_tool_action") or "recent work")
            next_ctx = f"{project}: {last_req[:100]}"
            wm_obj.write({"next_session_context": next_ctx})

            # Update profile cache session count if present
            try:
                _PROFILE_CACHE_PATH = Path.home() / ".jarvis" / "profile_cache.json"
                if _PROFILE_CACHE_PATH.exists():
                    cache = json.loads(_PROFILE_CACHE_PATH.read_text(encoding="utf-8"))
                    profile = cache.get("profile", {})
                    session_count = int(profile.get("session_count", 0)) + 1
                    profile["session_count"] = session_count
                    cache["profile"] = profile
                    import os as _os
                    tmp = _PROFILE_CACHE_PATH.with_suffix(".tmp")
                    tmp.write_text(json.dumps(cache, indent=2), encoding="utf-8")
                    _os.replace(tmp, _PROFILE_CACHE_PATH)
            except Exception:
                pass

            # Speak via client if available and idle
            if (
                client is not None
                and getattr(client, "connected", False)
                and not getattr(client, "busy", False)
            ):
                try:
                    import asyncio
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        asyncio.ensure_future(self._speak_wrapup(client, spoken_summary))
                except Exception:
                    pass

            log_event("session_wrapup_complete", {"ok": ok, "project": project})
            return ok

        except Exception as exc:
            log_event("session_wrapup_error", {"error": str(exc)})
            return False

    def _build_spoken_summary(
        self,
        wm: dict[str, Any],
        episodes: list[dict[str, Any]],
        project: str,
    ) -> str:
        """Build a max 2-sentence spoken summary of the session."""
        try:
            last_req = str(wm.get("last_user_request") or "").strip()
            last_tool = str(wm.get("last_tool_action") or "").strip()

            recent_ep_summaries = [
                str(e.get("summary", ""))
                for e in episodes[-5:]
                if e.get("summary") and "tool_request" not in str(e.get("kind", ""))
            ]
            activity = (
                recent_ep_summaries[-1] if recent_ep_summaries else (last_req or last_tool or "")
            )
            activity = activity[:120].strip()

            if project and project != "the current project":
                line1 = f"Today you worked on {project}."
            else:
                line1 = "Session complete."

            if activity:
                line2 = f"{activity}."
                return f"{line1} {line2}"
            return line1

        except Exception:
            return f"Session on {project} complete."

    async def _speak_wrapup(self, client: Any, spoken_summary: str) -> None:
        """Send spoken wrap-up via Realtime client. Never raises."""
        try:
            full_msg = f"{spoken_summary} Summary written to vault."
            await client.send({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "system",
                    "content": [{"type": "input_text", "text": f"[WRAPUP] {full_msg}"}],
                },
            })
            await client.send({
                "type": "response.create",
                "response": {
                    "modalities": ["audio", "text"],
                    "instructions": f"Say this exactly: '{full_msg}'",
                },
            })
        except Exception as exc:
            log_event("session_wrapup_speak_error", {"error": str(exc)})

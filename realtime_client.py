from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from pathlib import Path
from typing import Any

import numpy as np
import orjson
import websockets
from websockets.exceptions import ConnectionClosed

from audio import Speaker, pcm16_16k_to_base64_24k
from config import CONFIG
from tools import ToolRegistry
from utils import log_event, notify
from prometheus.core.intent_overrides import resolve_direct_intent
from prometheus.core.session_context import build_instructions, build_live_state_block
from prometheus.core.tool_followups import FOLLOWUP_ACTIONS
from prometheus.execution.response_synthesizer import synthesize_tool_response, is_calendar_action, is_synthesized_action


SYSTEM_PROMPT = """
You are Prometheus — a composed, intelligent local desktop assistant.
You have access to real-time workspace context, personal memory from the user's vault, and direct machine control.

Rules you always follow:
- When a tool result is available, base your response on it. Never ignore tool results.
- When asked what is open or running, use the screen_context tool to check — never guess from memory.
- When asked what you remember, use the vault memory context already injected into your instructions — answer naturally without mentioning the vault.
- Keep responses short and direct. No preamble. No apologies.
- For simple actions say only what happened: "Done." "Opening now." "VS Code is already open."
- For background tasks, tell the user where the output was written when complete.
- Never say you cannot access local information — you have workspace context available.
- If you are unsure, ask one short clarifying question.
- Never invent Home Assistant script names.
- For lights, Xbox, projector, TV, or smart-home requests, call desktop_action and let the tool layer choose the correct jarvis_* script.
- For search-style requests or current events, call desktop_action with web_search.
- For project or workspace switching, call desktop_action with smart_action.
- Do not pretend something succeeded if the tool says it failed.

Voice:
- Quiet confidence. Masculine. Intelligent.
- Minimal upward inflection. No filler words. No enthusiasm spikes.
- Speak results, not process. "Done." not "I have successfully completed the task of..."
""".strip()


class RealtimePrometheusClient:
    def __init__(self, speaker: Speaker, tools: ToolRegistry) -> None:
        self.api_key = CONFIG.get("openai_api_key", "")
        self.model = CONFIG.get("realtime_model", "gpt-realtime")
        self.voice = CONFIG.get("voice", "alloy")
        self.speaker = speaker
        self.tools = tools

        self.ws: websockets.ClientConnection | None = None
        self.connected = False
        self.awaiting_user_audio = False
        self._receiver_task: asyncio.Task | None = None

        # Reconnect backoff — schedule: 5s, 15s, 60s; hard-stop after max attempts
        self._should_reconnect = True
        self._RECONNECT_SCHEDULE: list[int] = [5, 15, 60]
        self._MAX_RECONNECT_ATTEMPTS = 5
        self._reconnect_attempt = 0
        self._reconnect_task: asyncio.Task | None = None
        # Dedup connection error logging — suppress repeats within 60s
        self._last_error_msg: str = ""
        self._last_error_dedup_ts: float = 0.0

        self.current_text = ""
        self.busy = False
        self.waiting_for_tool_followup = False
        self.last_cycle_end_at = 0.0
        self._override_handled = False
        self._drop_audio_until = 0.0
        # Guard against duplicate response.create while one is already in flight.
        # Reset on response.done / response.cancelled / response.failed / errors.
        self._response_active = False

        # Voice latency tracking — reset on each PTT press / begin_user_turn
        self._turn_start_ts: float = 0.0  # monotonic time when user started speaking
        # Bytes of audio sent since the last input_audio_buffer.committed event.
        # Reset to 0 when server_vad auto-commits (or on turn start).
        # Used in end_audio() to skip a redundant commit when the buffer is already empty.
        self._audio_bytes_since_commit: int = 0

        # Vault / workspace context injected before or during a session
        self._vault_context: str = ""
        self._workspace_context: str = ""

        # Dynamic system prompt — can be updated before connect()
        self._system_prompt: str = SYSTEM_PROMPT

        # Register voice error callback so tools.py can speak errors
        try:
            import tools as _tools
            _tools.set_voice_error_callback(self._handle_voice_error_sync)
        except Exception:
            pass

    def _handle_voice_error_sync(self, action: str, error: str) -> None:
        """Called from tools.py on tool failure. Queues an error speech via asyncio."""
        if not self.connected:
            return
        msg = f"I hit an error running {action}: {error[:80]}"
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.call_soon_threadsafe(
                    lambda: asyncio.ensure_future(self._speak_text(msg))
                )
        except Exception:
            pass

    async def _speak_text(self, text: str) -> None:
        """Send a text-only response through the Realtime session."""
        if not self.connected or not self.ws:
            return
        try:
            await self.send({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "system",
                    "content": [{"type": "input_text", "text": text}],
                },
            })
            await self._guarded_response_create(
                {
                    "modalities": ["audio", "text"],
                    "instructions": (
                        f"Respond as Prometheus. Deliver this message naturally and concisely, "
                        f"staying in character: {text}"
                    ),
                },
                context="_speak_text",
            )
        except Exception:
            pass

    def set_system_prompt(self, prompt: str) -> None:
        """Set the base system prompt. Call before connect() for full effect."""
        if prompt and prompt.strip():
            self._system_prompt = prompt.strip()
            log_event("system_prompt_updated", {"length": len(self._system_prompt)})

    # ── Vault / workspace context ─────────────────────────────────────────────

    def _log_vault_warning(self, msg: str) -> None:
        try:
            warn_file = Path.home() / ".jarvis" / "vault_warnings.json"
            existing: list = []
            if warn_file.exists():
                try:
                    existing = json.loads(warn_file.read_text(encoding="utf-8"))
                    if not isinstance(existing, list):
                        existing = []
                except Exception:
                    pass
            existing.append({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "warning": msg})
            tmp = warn_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(existing[-50:], indent=2), encoding="utf-8")
            os.replace(tmp, warn_file)
        except Exception:
            pass

    def inject_vault_context(self, chunks: list[dict]) -> None:
        """
        Build and store a formatted vault context block from retrieved chunks.
        Keeps the injected block under ~2000 tokens (≈8000 chars).
        Call before connect() for initial session, or anytime mid-session
        followed by _update_session_instructions() to push a fresh session.update.
        """
        try:
            MAX_CHUNKS = 5
            CHUNK_CHARS = 300
            lines: list[str] = [
                "--- PERSONAL MEMORY CONTEXT ---",
                "The following is retrieved from the user's personal knowledge vault.",
                "Use this to answer questions about their history, projects, and preferences.",
                "Do not mention that you are reading from a vault — answer naturally as if you know this.",
                "",
            ]
            for chunk in (chunks or [])[:MAX_CHUNKS]:
                title = str(chunk.get("title") or "")
                year  = str(chunk.get("year") or "")
                text  = str(chunk.get("text") or "")[:CHUNK_CHARS]
                header = f"[TITLE: {title}"
                if year:
                    header += f" | YEAR: {year}"
                header += "]"
                lines.append(header)
                lines.append(text)
                lines.append("")
            lines.append("--- END MEMORY CONTEXT ---")
            self._vault_context = "\n".join(lines)
        except Exception as exc:
            self._vault_context = ""
            self._log_vault_warning(f"inject_vault_context failed: {exc}")

    def inject_workspace_context(self, workspace: dict) -> None:
        """Store workspace state block to include in session instructions."""
        try:
            project     = str(workspace.get("project_name") or workspace.get("active_project_name") or "")
            path        = str(workspace.get("project_path") or workspace.get("active_project_path") or "")
            win_info    = workspace.get("active_window") or {}
            win_title   = str(win_info.get("title") or "") if isinstance(win_info, dict) else ""
            xbox_state  = workspace.get("xbox_state")
            xbox_app    = str(workspace.get("xbox_app") or "")
            xbox_media  = str(workspace.get("xbox_media_title") or "")
            lines = [
                "--- CURRENT WORKSPACE ---",
                f"Active project: {project or 'unknown'}",
            ]
            if win_title:
                lines.append(f"Active window: {win_title}")
            if path:
                lines.append(f"Project path: {path}")
            if xbox_state is not None:
                lines.append(f"Xbox state: {xbox_state or 'off'}")
                if xbox_app:
                    lines.append(f"Xbox app: {xbox_app}")
                if xbox_media:
                    lines.append(f"Xbox media: {xbox_media}")
            lines.append("--- END WORKSPACE ---")
            self._workspace_context = "\n".join(lines)
        except Exception as exc:
            self._workspace_context = ""
            self._log_vault_warning(f"inject_workspace_context failed: {exc}")

    def _build_instructions(self) -> str:
        """Combine system prompt with vault/workspace/working-memory context."""
        return build_instructions(
            self._system_prompt,
            self._workspace_context,
            self._vault_context,
        )

    def _build_live_state_block(self) -> str:
        """Build a compact live world state block for injection before each LLM response."""
        return build_live_state_block()

    async def _update_session_instructions(self) -> None:
        """Push updated instructions to an already-connected session via session.update."""
        if not self.connected or not self.ws:
            return
        try:
            await self.send({
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "instructions": self._build_instructions(),
                },
            })
            log_event("vault_context_injected", {
                "vault_chars": len(self._vault_context),
                "workspace_chars": len(self._workspace_context),
            })
        except Exception as exc:
            self._log_vault_warning(f"_update_session_instructions failed: {exc}")

    async def _handle_vault_recall(self, transcript: str) -> None:
        """On-demand vault search triggered by 'what do you remember about...' phrases."""
        query = transcript.strip()
        _strip_prefixes = (
            "what do you remember about ",
            "what do you know about ",
            "do you remember ",
            "check my vault and find out ",
            "check my vault and find ",
            "check my vault for ",
            "check my vault ",
            "search my vault for ",
            "search my vault ",
            "look in my vault for ",
            "look in my vault ",
            "query my vault for ",
            "query my vault ",
            "search the vault for ",
            "search the vault ",
            "find in my vault ",
            "from my vault ",
        )
        for phrase in _strip_prefixes:
            lower = query.lower()
            if lower.startswith(phrase):
                query = query[len(phrase):]
                break
        try:
            from memory_core import query_vault
            loop = asyncio.get_running_loop()
            results = await loop.run_in_executor(None, lambda: query_vault(query, limit=5))
            if results:
                self.inject_vault_context(results)
                await self._update_session_instructions()
                log_event("vault_recall_injected", {"query": query[:80], "count": len(results)})
        except Exception as exc:
            self._log_vault_warning(f"_handle_vault_recall failed: {exc}")
            log_event("vault_recall_error", {"error": str(exc)})

        await self._guarded_response_create(
            {
                "modalities": ["audio", "text"],
                "instructions": (
                    "Answer the user's question using the personal memory context "
                    "that was just injected into the session instructions. "
                    "Answer naturally — do not say you are reading from a vault. "
                    "If you cannot find the information, say so briefly."
                ),
            },
            context="_handle_vault_recall",
        )

    def _project_resume_override(
        self, transcript: str, text: str
    ) -> dict[str, Any] | None:
        from prometheus.core.intent_overrides import resolve_project_resume
        return resolve_project_resume(transcript, text)

    def _direct_intent_override(self, transcript: str) -> dict[str, Any] | None:
        """Deterministic intent routing — delegates to prometheus.core.intent_overrides."""
        return resolve_direct_intent(transcript)
    async def _contextual_override(self, transcript: str) -> bool:
        """
        Contextual intent resolver for vague commands ("fix that", "continue", etc.).
        Rule-based only on the voice path (< 50ms, no LLM).
        Returns True if the command was handled, False to fall through to Realtime API.
        """
        try:
            from contextual_intent import ContextualIntentResolver, _is_vague
            from world_model import build_world_snapshot

            if not _is_vague(transcript):
                return False

            snap = build_world_snapshot()
            resolver = ContextualIntentResolver()
            result = resolver.resolve(transcript, snap, mode="fast")

            if result is None:
                return False

            self.awaiting_user_audio = False
            self.busy = True

            assumption = result.get("user_facing_assumption", "")
            intent = result.get("intent", "")
            slots = result.get("slots", {})
            risk = result.get("risk", "safe")

            log_event("contextual_intent_override", {
                "command": transcript[:80],
                "intent": intent,
                "confidence": result.get("confidence", 0),
                "risk": risk,
            })

            if result.get("requires_clarification"):
                question = result.get("clarifying_question") or "Can you be more specific?"
                await self._speak_text(question)
                self.busy = False
                return True

            if result.get("requires_confirmation") or risk in ("high", "dangerous"):
                msg = (assumption or f"I'm planning to: {intent}.") + " Confirm?"
                await self._speak_text(msg)
                # Set pending confirmation — user must say "yes" to proceed
                self.tools._pending_action = {
                    "intent": intent,
                    "slots": slots,
                    "assumption": assumption,
                }
                self.busy = False
                return True

            if result.get("should_execute") and slots.get("action"):
                if assumption:
                    await self._speak_text(assumption)
                payload = {"action": slots["action"], **{k: v for k, v in slots.items() if k != "action"}}
                await self._run_direct_tool(payload)
                return True

            if result.get("should_execute") and intent in ("get_mission_status", "run_diagnostics", "summarize_screen"):
                if assumption:
                    await self._speak_text(assumption)
                await self._run_direct_tool({"action": intent})
                return True

        except Exception as exc:
            log_event("contextual_override_error", {"error": str(exc)[:200]})

        return False

    async def _speak_text(self, text: str) -> None:
        """Speak a short text response via the Realtime API."""
        await self._guarded_response_create(
            {
                "modalities": ["audio", "text"],
                "instructions": (
                    f"Respond as Prometheus. Deliver this message naturally and concisely, "
                    f"staying in character: {text}"
                ),
            },
            context="_speak_text",
        )

    async def _run_direct_tool(self, payload: dict[str, Any]) -> None:
        print("Direct tool override:", payload)
        ts_to_first_tool_ms = (
            round((time.monotonic() - self._turn_start_ts) * 1000)
            if self._turn_start_ts > 0
            else None
        )
        log_event("direct_tool_override", {
            "payload": payload,
            "ts_to_first_tool_ms": ts_to_first_tool_ms,
        })

        result = self.tools.execute(payload)
        self._override_handled = True

        try:
            from working_memory import WorkingMemory
            WorkingMemory().set_tool_result(
                action=str(payload.get("action", "")),
                ok=result.ok,
                message=result.message,
                data=result.data or {},
            )
            asyncio.ensure_future(self._update_session_instructions())
        except Exception:
            pass

        followup_actions = FOLLOWUP_ACTIONS

        action = str(payload.get("action", "")).strip().lower()

        # Log web search result content before generating spoken response
        if action == "web_search":
            search_summary = str((result.data or {}).get("summary", "")).strip()
            log_event("web_search_result_direct", {
                "query": str(payload.get("query", ""))[:120],
                "summary_length": len(search_summary),
                "summary_empty": not bool(search_summary),
                "ok": result.ok,
            })

        needs_followup = (
            action in followup_actions
            or not result.ok
            or "Awaiting confirmation" in result.message
            or "confirm" in result.message.lower()
            or "error" in result.message.lower()
        )

        if needs_followup:
            await self.send(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "system",
                        "content": [
                            {
                                "type": "input_text",
                                "text": json.dumps(
                                    {
                                        "tool_result": {
                                            "ok": result.ok,
                                            "message": result.message,
                                            "data": result.data or {},
                                        }
                                    }
                                ),
                            }
                        ],
                    },
                }
            )

            if action == "web_search":
                search_summary = str((result.data or {}).get("summary", "")).strip()
                if search_summary:
                    response_instructions = (
                        f"Web search result: {search_summary[:800]} "
                        "Speak this information naturally and concisely. "
                        "Do not say you searched the web. Do not add filler."
                    )
                else:
                    response_instructions = (
                        "The web search returned no useful results. "
                        "Say exactly: 'I searched but couldn't find a clear answer for that.'"
                    )
            elif action == "screen_context":
                response_instructions = (
                    "Report the current workspace state from the tool result data. "
                    "Mention the active project, active window, and xbox state if relevant. "
                    "Be brief and factual."
                )
            elif action == "search_codebase":
                count = (result.data or {}).get("count", 0)
                output = str((result.data or {}).get("output", ""))[:600]
                if count > 0:
                    response_instructions = (
                        f"Found {count} matches in the codebase. "
                        f"Results: {output[:400]}. Report the key matches concisely."
                    )
                else:
                    response_instructions = "Say: 'No matches found for that search.'"
            elif action in ("git_status", "git_diff"):
                data = result.data or {}
                if action == "git_status":
                    status_text = str(data.get("status", ""))
                    if data.get("clean"):
                        response_instructions = "Say: 'No uncommitted changes.'"
                    else:
                        response_instructions = (
                            f"Git status: {status_text[:300]}. Report which files have changed."
                        )
                else:
                    diff = str(data.get("diff", ""))[:500]
                    if diff:
                        response_instructions = (
                            f"Git diff: {diff}. Summarize what changed in one or two sentences."
                        )
                    else:
                        response_instructions = "Say: 'No staged or unstaged changes in the diff.'"
            elif action == "session_wrapup":
                response_instructions = f"The session wrap-up has been triggered. {result.message}"
            elif action == "system_status":
                data_str = json.dumps(result.data or {}, indent=1)[:600]
                response_instructions = (
                    f"Describe what you currently have loaded — workspace context, vault context, "
                    f"active project, and current state. Data: {data_str}"
                )
            elif action == "get_priorities":
                priorities = (result.data or {}).get("priorities", [])
                response_instructions = (
                    f"State Tate's top priorities right now based on: {priorities}. Be specific and direct."
                )
            elif action in ("run_python", "run_shell"):
                output = str((result.data or {}).get("output", ""))[:400]
                if result.ok:
                    response_instructions = (
                        f"Command executed. Output: {output}. Report the result concisely."
                    )
                else:
                    response_instructions = (
                        f"Command failed. Error: {result.message}. Report the failure."
                    )
            elif action == "start_coding_task":
                d = result.data or {}
                goal = d.get("goal", "")[:60]
                criteria = d.get("criteria", "")
                response_instructions = (
                    f"Coding task started in background: '{goal}'. "
                    f"Success criteria: {criteria}. "
                    "Say: 'Coding task started. I'll let you know when it's done.'"
                )
            elif action == "get_coding_status":
                d = result.data or {}
                if d.get("status") == "no task running":
                    response_instructions = "Say: 'No coding task has been run yet.'"
                elif d.get("success"):
                    att = d.get("attempts", 1)
                    diff = d.get("diff", "")[:200]
                    response_instructions = (
                        f"Coding task succeeded in {att} attempt(s). Changes: {diff}. "
                        "Report the success and key changes concisely."
                    )
                else:
                    rolled = d.get("rolled_back", False)
                    response_instructions = (
                        f"Coding task failed after {d.get('attempts',0)} attempt(s). "
                        + ("Changes were rolled back. " if rolled else "")
                        + "Report the failure briefly."
                    )
            elif action == "start_build":
                d = result.data or {}
                goal = d.get("goal", "")[:60]
                response_instructions = (
                    f"Orchestrated build started for: '{goal}'. "
                    "Say: 'Build started. Architect, Coder, and Tester are running in the background. "
                    "I'll let you know when it's done.'"
                )
            elif action == "get_build_status":
                d = result.data or {}
                status = d.get("status", "")
                if status == "no build running":
                    response_instructions = "Say: 'No orchestrated build has been run yet.'"
                elif status == "running":
                    response_instructions = (
                        f"Build is still running for: '{d.get('goal','')[:50]}'. "
                        "Say: 'The build is still in progress. I'll let you know when it completes.'"
                    )
                elif d.get("success"):
                    tr = d.get("test_results", {})
                    phases = d.get("phases_completed", [])
                    response_instructions = (
                        f"Build succeeded. {tr.get('passed', 0)} tests passing. "
                        f"Phases: {', '.join(phases[:5])}. Report the success concisely."
                    )
                elif d.get("needs_human"):
                    tr = d.get("test_results", {})
                    response_instructions = (
                        f"Build hit the debug limit and needs human review. "
                        f"{tr.get('failed', 0)} tests still failing. "
                        "Say: 'The build hit its debug limit. I need your help to resolve the remaining failures.'"
                    )
                else:
                    response_instructions = (
                        f"Build failed. Goal: '{d.get('goal','')[:50]}'. Report the failure briefly."
                    )
            elif action == "run_diagnostics":
                summary = str((result.data or {}).get("spoken_summary", "Diagnostics complete."))
                response_instructions = (
                    f"Read the diagnostic summary: {summary}. "
                    "Report it clearly. Do not add preamble."
                )
            elif action == "read_file":
                output = str((result.data or {}).get("content", ""))[:600]
                response_instructions = (
                    f"File contents: {output}. Summarize what's relevant to the current mission concisely."
                )
            elif action == "list_files":
                items = (result.data or {}).get("items", [])
                names = [f"{i['name']}{'/' if i.get('is_dir') else ''}" for i in items[:30]]
                output = ", ".join(names)[:400]
                response_instructions = f"Directory contents: {output}. Report what's there concisely."
            elif action == "get_mission_status":
                output = result.message[:500]
                response_instructions = f"Status: {output}. Report the key points concisely."
            elif action == "query_vault":
                output = result.message[:500]
                response_instructions = (
                    f"Action 'query_vault' completed. {output}. Report the result concisely."
                )
            elif is_synthesized_action(action):
                response_instructions = synthesize_tool_response(action, result)
            else:
                response_instructions = (
                    "Briefly report the result in polished British butler style. "
                    "Be precise and do not claim an app or project was already open unless the tool result explicitly shows that."
                )

            await self._guarded_response_create(
                {"modalities": ["audio", "text"], "instructions": response_instructions},
                context="_run_direct_tool_followup",
            )
        else:
            self.busy = False
            self.last_cycle_end_at = time.time()

    async def connect(self) -> None:
        if not self.api_key:
            raise RuntimeError("Missing OpenAI API key.")

        url = f"wss://api.openai.com/v1/realtime?model={self.model}"
        # GA Realtime API — no OpenAI-Beta header; Authorization only
        headers = {
            "Authorization": f"Bearer {self.api_key}",
        }

        # Log connection attempt — header names only, never values or API key
        log_event("realtime_connect_attempt", {
            "url": url,
            "model": self.model,
            "header_names": list(headers.keys()),
            "has_beta_header": "OpenAI-Beta" in headers,
            "reconnect_attempt": self._reconnect_attempt,
        })

        self.ws = await websockets.connect(
            url,
            additional_headers=headers,
            max_size=20_000_000,
        )
        self.connected = True
        self._receiver_task = asyncio.create_task(self._receiver())

        _instructions = self._build_instructions()
        log_event("session_instructions_debug", {
            "total_length": len(_instructions),
            "has_vault": "PERSONAL MEMORY CONTEXT" in _instructions,
            "has_workspace": "CURRENT WORKSPACE" in _instructions,
            "vault_titles": [
                l.strip() for l in _instructions.splitlines()
                if l.strip().startswith("[TITLE:")
            ],
        })

        # Log outgoing session payload (redacted — no instructions text, no API key)
        log_event("realtime_session_payload_debug", {
            "model": self.model,
            "voice": self.voice,
            "modalities": ["text", "audio"],
            "instructions_length": len(_instructions),
        })

        # Audit headers — only Authorization is permitted
        _forbidden_headers = [h for h in headers if h != "Authorization"]
        if _forbidden_headers:
            log_event("realtime_header_blocked", {"forbidden": _forbidden_headers})
            notify(f"Realtime blocked: unexpected headers {_forbidden_headers}")
            self._should_reconnect = False
            return

        _session_update = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": _instructions,
            },
        }

        # Log session keys before sending — never log secrets
        _sess = _session_update["session"]
        log_event("realtime_session_update_keys", {
            "session_keys": list(_sess.keys()),
        })

        # Hard audit: block send if any forbidden beta field appears in the payload
        _forbidden_payload_strings = [
            "OpenAI-Beta",
            "realtime=v1",
            "input_audio_format",
            "output_audio_format",
            "additionalProperties",
            "whisper-1",
            "input_audio_transcription_model",
        ]
        _payload_text = json.dumps(_session_update)
        _hits = [s for s in _forbidden_payload_strings if s in _payload_text]
        if _hits:
            log_event("realtime_payload_blocked", {"forbidden": _hits})
            notify(f"Realtime payload blocked: forbidden fields {_hits}")
            self._should_reconnect = False
            return

        await self.send(_session_update)

        asyncio.create_task(self._chat_polling_loop())
        # Successful connection — reset reconnect counter
        self._reconnect_attempt = 0

        log_event("realtime_connected", {"model": self.model, "voice": self.voice})
        print(f"Realtime connected ({self.model})")

    async def _chat_polling_loop(self) -> None:
        """
        Poll working_memory["chat_input"] every 500ms.

        Tool actions (direct intent overrides): execute and format as readable text.
        Vault recall: run search and return formatted results.
        Everything else: send to chat_completion() (Claude/Ollama text model).
        Never touches the Realtime API — chat is text-only.
        """
        from working_memory import WorkingMemory
        wm = WorkingMemory()
        last_ts = ""
        loop = asyncio.get_running_loop()

        while self.connected:
            try:
                await asyncio.sleep(0.5)
                data = wm.read()
                chat_input = data.get("chat_input")
                if not isinstance(chat_input, dict):
                    continue
                ts = str(chat_input.get("ts", ""))
                if not ts or ts == last_ts:
                    continue
                last_ts = ts
                text = str(chat_input.get("text", "")).strip()
                if not text:
                    continue

                history: list[dict] = []
                try:
                    history = list(data.get("chat_history") or [])
                except Exception:
                    pass

                response_text = ""
                path = "llm"
                override = self._direct_intent_override(text)

                if override and override.get("type") == "direct_tool":
                    path = "tool"
                    payload = override["payload"]
                    result = await loop.run_in_executor(
                        None, lambda p=payload: self.tools.execute(p, chat_format=True)
                    )
                    response_text = result.message

                elif override and override.get("type") == "vault_recall":
                    path = "vault"
                    query = str(override.get("query", text))
                    try:
                        from memory_core import query_vault
                        results = await loop.run_in_executor(
                            None, lambda q=query: query_vault(q, limit=5)
                        )
                        if results:
                            lines = [f"Found {len(results)} vault result(s) for '{query[:40]}':"]
                            for r in results[:5]:
                                title = str(r.get("title", ""))[:60]
                                snippet = str(r.get("text", ""))[:120].replace("\n", " ")
                                lines.append(f"\n  [{title}]\n  {snippet}...")
                            response_text = "\n".join(lines)
                        else:
                            response_text = f"No vault entries found for '{query[:60]}'."
                    except Exception as exc:
                        response_text = f"Vault search failed: {str(exc)[:80]}"

                else:
                    context = {
                        "active_project": str(data.get("active_workspace", "")),
                        "last_tool_result": data.get("last_tool_result", {}),
                        "ollama_available": bool(data.get("ollama_available", True)),
                    }
                    from llm_router import chat_completion
                    response_text = await loop.run_in_executor(
                        None, lambda t=text, c=context, h=history: chat_completion(t, c, h)
                    )

                resp_ts = time.strftime("%Y-%m-%dT%H:%M:%S")

                updated_history = list(history)
                updated_history.append({"role": "user", "content": text, "ts": ts})
                updated_history.append({"role": "assistant", "content": response_text, "ts": resp_ts})
                updated_history = updated_history[-20:]

                wm.write({
                    "chat_response": {"text": response_text, "ts": resp_ts},
                    "chat_history": updated_history,
                })
                log_event("chat_input_routed", {
                    "text": text[:80],
                    "response": response_text[:80],
                    "path": path,
                })
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log_event("chat_polling_error", {"error": str(exc)[:200]})

    async def close(self) -> None:
        self._should_reconnect = False  # intentional close — do not reconnect
        self.connected = False
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        if self._receiver_task:
            self._receiver_task.cancel()
        if self.ws:
            await self.ws.close()
        log_event("realtime_closed", {})

    async def interrupt(self) -> None:
        """Cancel the current assistant response and drop incoming audio for 2s."""
        self._drop_audio_until = time.time() + 2.0
        self.busy = False
        self.waiting_for_tool_followup = False
        self._override_handled = False
        if self.connected and self.ws:
            try:
                await self.send({"type": "response.cancel"})
                log_event("interrupt_sent", {})
            except Exception as exc:
                log_event("interrupt_send_error", {"error": str(exc)})

    async def send(self, data: dict[str, Any]) -> None:
        if not self.ws:
            raise RuntimeError("Realtime websocket is not connected.")
        await self.ws.send(orjson.dumps(data).decode())

    async def _guarded_response_create(self, response_payload: dict[str, Any], context: str = "") -> bool:
        """Send response.create only if no response is currently active.

        Returns True if the request was sent, False if it was blocked (duplicate).
        Strips 'modalities' — unknown parameter in the GA Realtime API; session defaults apply.
        """
        if self._response_active:
            log_event("response_guard_blocked", {"context": context[:80]})
            print(f"[DBG] response.create BLOCKED (already active) ctx={context}")
            return False
        cleaned = {k: v for k, v in response_payload.items() if k != "modalities"}
        self._response_active = True
        print(f"[DBG] sending response.create ctx={context} keys={list(cleaned.keys())}")
        await self.send({"type": "response.create", "response": cleaned})
        print(f"[DBG] response.create sent")
        return True

    async def begin_user_turn(self) -> None:
        self.awaiting_user_audio = True
        self._turn_start_ts = time.monotonic()
        self._audio_bytes_since_commit = 0
        log_event("user_turn_started", {})

    async def send_audio(self, chunk: bytes) -> None:
        if not self.awaiting_user_audio:
            return

        self._audio_bytes_since_commit += len(chunk)
        arr = np.frombuffer(chunk, dtype=np.int16)
        b64 = pcm16_16k_to_base64_24k(arr)

        await self.send(
            {
                "type": "input_audio_buffer.append",
                "audio": b64,
            }
        )

    async def end_audio(self) -> None:
        if not self.awaiting_user_audio:
            return

        self.awaiting_user_audio = False
        self.current_text = ""
        self.busy = True
        self.waiting_for_tool_followup = False
        self._override_handled = False
        self._drop_audio_until = 0.0

        _MIN_COMMIT_BYTES = 3200
        if self._audio_bytes_since_commit < _MIN_COMMIT_BYTES:
            print(f"[DBG] end_audio: skip commit ({self._audio_bytes_since_commit} bytes, server_vad already committed)")
            log_event("end_audio_vad_committed", {"bytes": self._audio_bytes_since_commit})
            return

        log_event("user_turn_committed", {})
        print("[DBG] end_audio: sending input_audio_buffer.commit")
        await self.send({"type": "input_audio_buffer.commit"})
        print("[DBG] end_audio: commit sent — sending response.create")
        await self._guarded_response_create({}, context="end_audio_ptt")
        print("Committing audio and requesting response")

    async def _handle_tool_call(self, event: dict[str, Any]) -> None:
        try:
            args = json.loads(event.get("arguments", "{}"))
        except Exception:
            args = {}

        print("Tool call:", args)
        log_event("tool_call_received", {"args": args})

        result = self.tools.execute(args)

        try:
            from working_memory import WorkingMemory
            WorkingMemory().set_tool_result(
                action=str(args.get("action", "")),
                ok=result.ok,
                message=result.message,
                data=result.data or {},
            )
            asyncio.ensure_future(self._update_session_instructions())
        except Exception:
            pass

        await self.send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": event.get("call_id"),
                    "output": json.dumps(
                        {
                            "ok": result.ok,
                            "message": result.message,
                            "data": result.data or {},
                        }
                    ),
                },
            }
        )

        followup_actions = FOLLOWUP_ACTIONS

        tool_action = str(args.get("action", "")).strip().lower()

        # Log web search result content before generating spoken response
        if tool_action == "web_search":
            search_summary = str((result.data or {}).get("summary", "")).strip()
            log_event("web_search_result", {
                "query": str(args.get("query", ""))[:120],
                "summary_length": len(search_summary),
                "summary_empty": not bool(search_summary),
                "ok": result.ok,
            })

        needs_followup = (
            tool_action in followup_actions
            or not result.ok
            or "Awaiting confirmation" in result.message
            or "confirm" in result.message.lower()
            or "error" in result.message.lower()
        )

        if needs_followup:
            self.waiting_for_tool_followup = True
            if tool_action == "web_search":
                search_summary = str((result.data or {}).get("summary", "")).strip()
                if search_summary:
                    response_instructions = (
                        f"Web search result: {search_summary[:800]} "
                        "Speak this information naturally and concisely. "
                        "Do not say you searched the web. Do not add filler."
                    )
                else:
                    response_instructions = (
                        "The web search returned no useful results. "
                        "Say exactly: 'I searched but couldn't find a clear answer for that.'"
                    )
            elif tool_action == "screen_context":
                response_instructions = (
                    "Report the current workspace state from the tool result data. "
                    "Mention the active project, active window, and xbox state if relevant. "
                    "Be brief and factual."
                )
            elif tool_action == "search_codebase":
                count = (result.data or {}).get("count", 0)
                output = str((result.data or {}).get("output", ""))[:600]
                if count > 0:
                    response_instructions = (
                        f"Found {count} matches in the codebase. "
                        f"Results: {output[:400]}. Report the key matches concisely."
                    )
                else:
                    response_instructions = "Say: 'No matches found for that search.'"
            elif tool_action in ("git_status", "git_diff"):
                data = result.data or {}
                if tool_action == "git_status":
                    status_text = str(data.get("status", ""))
                    if data.get("clean"):
                        response_instructions = "Say: 'No uncommitted changes.'"
                    else:
                        response_instructions = (
                            f"Git status: {status_text[:300]}. Report which files have changed."
                        )
                else:
                    diff = str(data.get("diff", ""))[:500]
                    if diff:
                        response_instructions = (
                            f"Git diff: {diff}. Summarize what changed in one or two sentences."
                        )
                    else:
                        response_instructions = "Say: 'No staged or unstaged changes in the diff.'"
            elif tool_action == "session_wrapup":
                response_instructions = f"The session wrap-up has been triggered. {result.message}"
            elif tool_action == "system_status":
                data_str = json.dumps(result.data or {}, indent=1)[:600]
                response_instructions = (
                    f"Describe what you currently have loaded — workspace context, vault context, "
                    f"active project, and current state. Data: {data_str}"
                )
            elif tool_action == "get_priorities":
                priorities = (result.data or {}).get("priorities", [])
                response_instructions = (
                    f"State Tate's top priorities right now based on: {priorities}. Be specific and direct."
                )
            elif tool_action in ("run_python", "run_shell"):
                output = str((result.data or {}).get("output", ""))[:400]
                if result.ok:
                    response_instructions = (
                        f"Command executed. Output: {output}. Report the result concisely."
                    )
                else:
                    response_instructions = (
                        f"Command failed. Error: {result.message}. Report the failure."
                    )
            elif tool_action == "start_coding_task":
                d = result.data or {}
                goal = d.get("goal", "")[:60]
                criteria = d.get("criteria", "")
                response_instructions = (
                    f"Coding task started in background: '{goal}'. "
                    f"Success criteria: {criteria}. "
                    "Say: 'Coding task started. I'll let you know when it's done.'"
                )
            elif tool_action == "get_coding_status":
                d = result.data or {}
                if d.get("status") == "no task running":
                    response_instructions = "Say: 'No coding task has been run yet.'"
                elif d.get("success"):
                    att = d.get("attempts", 1)
                    diff = d.get("diff", "")[:200]
                    response_instructions = (
                        f"Coding task succeeded in {att} attempt(s). Changes: {diff}. "
                        "Report the success and key changes concisely."
                    )
                else:
                    rolled = d.get("rolled_back", False)
                    response_instructions = (
                        f"Coding task failed after {d.get('attempts',0)} attempt(s). "
                        + ("Changes were rolled back. " if rolled else "")
                        + "Report the failure briefly."
                    )
            elif tool_action == "start_build":
                d = result.data or {}
                goal = d.get("goal", "")[:60]
                response_instructions = (
                    f"Orchestrated build started for: '{goal}'. "
                    "Say: 'Build started. Architect, Coder, and Tester are running in the background. "
                    "I'll let you know when it's done.'"
                )
            elif tool_action == "get_build_status":
                d = result.data or {}
                status = d.get("status", "")
                if status == "no build running":
                    response_instructions = "Say: 'No orchestrated build has been run yet.'"
                elif status == "running":
                    response_instructions = (
                        f"Build is still running for: '{d.get('goal','')[:50]}'. "
                        "Say: 'The build is still in progress. I'll let you know when it completes.'"
                    )
                elif d.get("success"):
                    tr = d.get("test_results", {})
                    phases = d.get("phases_completed", [])
                    response_instructions = (
                        f"Build succeeded. Goal: '{d.get('goal','')[:50]}'. "
                        f"{tr.get('passed', 0)} tests passing. Phases: {', '.join(phases[:5])}. "
                        "Report the success concisely."
                    )
                elif d.get("needs_human"):
                    tr = d.get("test_results", {})
                    response_instructions = (
                        f"Build hit the debug limit and needs human review. "
                        f"Goal: '{d.get('goal','')[:50]}'. "
                        f"{tr.get('failed', 0)} tests still failing. "
                        "Say: 'The build hit its debug limit. I need your help to resolve the remaining failures.'"
                    )
                else:
                    response_instructions = (
                        f"Build failed. Goal: '{d.get('goal','')[:50]}'. "
                        "Report the failure briefly."
                    )
            elif tool_action == "run_diagnostics":
                summary = str((result.data or {}).get("spoken_summary", "Diagnostics complete."))
                response_instructions = (
                    f"Read the diagnostic summary: {summary}. "
                    "Report it clearly. Do not add preamble."
                )
            elif tool_action == "read_file":
                output = str((result.data or {}).get("content", ""))[:600]
                response_instructions = (
                    f"File contents: {output}. Summarize what's relevant to the current mission concisely."
                )
            elif tool_action == "list_files":
                items = (result.data or {}).get("items", [])
                names = [f"{i['name']}{'/' if i.get('is_dir') else ''}" for i in items[:30]]
                output = ", ".join(names)[:400]
                response_instructions = f"Directory contents: {output}. Report what's there concisely."
            elif tool_action == "get_mission_status":
                output = result.message[:500]
                response_instructions = f"Status: {output}. Report the key points concisely."
            elif tool_action == "query_vault":
                output = result.message[:500]
                response_instructions = (
                    f"Action 'query_vault' completed. {output}. Report the result concisely."
                )
            elif is_synthesized_action(tool_action):
                response_instructions = synthesize_tool_response(tool_action, result)
            else:
                response_instructions = (
                    "Briefly report the result in polished British butler style. "
                    "Do not add filler. "
                    "Do not claim something is already open unless the tool result explicitly says so."
                )
            await self._guarded_response_create(
                {"modalities": ["audio", "text"], "instructions": response_instructions},
                context="_handle_tool_call_followup",
            )
        else:
            self.waiting_for_tool_followup = False
            self.busy = False
            self.last_cycle_end_at = time.time()

    def _log_connection_error_deduped(self, msg: str, event_name: str) -> None:
        """Log a connection error once; suppress repeats within 60 seconds."""
        now = time.time()
        if msg != self._last_error_msg or (now - self._last_error_dedup_ts) > 60.0:
            notify(f"Realtime connection closed: {msg}")
            log_event(event_name, {"error": msg})
            self._last_error_msg = msg
            self._last_error_dedup_ts = now
        else:
            log_event(event_name + "_suppressed", {"suppressed": True})

    async def _reconnect_with_backoff(self) -> None:
        """Attempt to re-establish the Realtime WebSocket with scheduled backoff.

        Schedule: 5s → 15s → 60s. Stops after _MAX_RECONNECT_ATTEMPTS failures.
        Prometheus continues running (text/tool path) even when realtime is down.
        """
        if not self._should_reconnect:
            return
        if self._reconnect_attempt >= self._MAX_RECONNECT_ATTEMPTS:
            log_event("realtime_reconnect_exhausted", {
                "attempts": self._reconnect_attempt,
                "max": self._MAX_RECONNECT_ATTEMPTS,
            })
            notify(
                "Realtime voice offline after max reconnect attempts. "
                "Restart Prometheus to retry."
            )
            self._should_reconnect = False
            return
        delay = self._RECONNECT_SCHEDULE[
            min(self._reconnect_attempt, len(self._RECONNECT_SCHEDULE) - 1)
        ]
        self._reconnect_attempt += 1
        log_event("realtime_reconnect_scheduled", {
            "attempt": self._reconnect_attempt,
            "of_max": self._MAX_RECONNECT_ATTEMPTS,
            "delay_s": delay,
        })
        print(
            f"Realtime reconnect attempt {self._reconnect_attempt}/"
            f"{self._MAX_RECONNECT_ATTEMPTS} in {delay}s"
        )
        await asyncio.sleep(delay)
        if not self._should_reconnect:
            return
        try:
            await self.connect()
            log_event("realtime_reconnected", {
                "attempt": self._reconnect_attempt,
                "after_delay_s": delay,
            })
        except Exception as exc:
            log_event("realtime_reconnect_failed", {
                "attempt": self._reconnect_attempt,
                "error": str(exc)[:200],
            })
            if self._should_reconnect:
                self._reconnect_task = asyncio.create_task(self._reconnect_with_backoff())

    async def _receiver(self) -> None:
        assert self.ws is not None

        try:
            while self.connected:
                raw = await self.ws.recv()
                event = json.loads(raw)
                event_type = event.get("type", "")
                log_event("realtime_event", {"type": event_type})
                print(f"[DBG] recv: {event_type}")

                if event_type == "error":
                    err_obj = event.get("error") or {}
                    err_code = str(err_obj.get("code") or "unknown")
                    err_msg = str(err_obj.get("message") or str(event))[:200]
                    err_param = str(err_obj.get("param") or "")
                    err_event_id = str(err_obj.get("event_id") or "")
                    log_event("realtime_api_error", {"code": err_code, "message": err_msg})
                    print(f"[DBG] REALTIME ERROR code={err_code!r} msg={err_msg!r} param={err_param!r} event_id={err_event_id!r}")
                    # Non-fatal errors that must not stop an active response:
                    # - buffer errors: PTT double-commit after server_vad already committed
                    # - already_active: server_vad and client both created a response
                    _NON_FATAL_ERRORS = {
                        "input_audio_buffer_commit_empty",
                        "input_audio_buffer_flush_empty",
                        "conversation_already_has_active_response",
                    }
                    if err_code in _NON_FATAL_ERRORS:
                        continue
                    self.busy = False
                    self._response_active = False
                    self.speaker.finish_realtime()
                    self.last_cycle_end_at = time.time()
                    continue

                if event_type == "input_audio_buffer.committed":
                    self._audio_bytes_since_commit = 0
                    continue

                if (
                    event_type
                    == "conversation.item.input_audio_transcription.completed"
                ):
                    transcript = event.get("transcript", "")
                    if transcript:
                        notify(f"Heard: {transcript}")
                        ts_transcription_ms = (
                            round((time.monotonic() - self._turn_start_ts) * 1000)
                            if self._turn_start_ts > 0
                            else None
                        )
                        log_event("transcript", {
                            "transcript": transcript[:300],
                            "ts_to_transcription_ms": ts_transcription_ms,
                        })

                        override = self._direct_intent_override(transcript)
                        if override and override.get("type") == "direct_tool":
                            self.awaiting_user_audio = False
                            self.busy = True
                            await self._run_direct_tool(override["payload"])
                            continue
                        if override and override.get("type") == "vault_recall":
                            self.awaiting_user_audio = False
                            self.busy = True
                            await self._handle_vault_recall(override["query"])
                            continue

                        # Contextual intent resolution — handles vague commands
                        # like "fix that", "continue", "what's wrong", etc.
                        # Rule-based only (mode="fast"); no LLM on the voice path.
                        ctx_handled = await self._contextual_override(transcript)
                        if ctx_handled:
                            continue

                    if not self._override_handled:
                        state_block = self._build_live_state_block()
                        if state_block:
                            await self.send({
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "message",
                                    "role": "system",
                                    "content": [{"type": "input_text", "text": state_block}],
                                },
                            })
                        await self._guarded_response_create(
                            {"modalities": ["audio", "text"]},
                            context="transcript_no_override",
                        )
                    continue

                if event_type in {
                    "response.text.delta",
                    "response.output_text.delta",
                    "response.output_audio_transcript.delta",
                }:
                    delta = event.get("delta", "")
                    if delta:
                        self.current_text += delta
                    continue

                if event_type in {"response.audio.delta", "response.output_audio.delta"}:
                    if time.time() < self._drop_audio_until:
                        print(f"[DBG] audio delta DROPPED (drop_until active)")
                        continue
                    audio_b64 = event.get("delta", "")
                    if audio_b64:
                        pcm = base64.b64decode(audio_b64)
                        print(f"[DBG] audio chunk {len(pcm)} bytes -> speaker")
                        try:
                            await asyncio.to_thread(self.speaker.play_pcm_chunk, pcm)
                        except Exception as _exc:
                            print(f"[DBG] speaker.play_pcm_chunk FAILED: {_exc!r}")
                    continue

                if event_type in {"response.audio.done", "response.output_audio.done"}:
                    print(f"[DBG] audio done -> speaker.finish_realtime")
                    await asyncio.to_thread(self.speaker.finish_realtime)
                    continue

                if event_type == "response.function_call_arguments.done":
                    await self._handle_tool_call(event)
                    continue

                if event_type == "response.done":
                    print(f"[DBG] response.done received")
                    self.waiting_for_tool_followup = False
                    self.busy = False
                    self._response_active = False
                    self._drop_audio_until = 0.0
                    self.last_cycle_end_at = time.time()
                    continue

                if event_type in {"response.cancelled", "response.failed"}:
                    self._response_active = False
                    self.busy = False
                    self.last_cycle_end_at = time.time()
                    continue

        except asyncio.CancelledError:
            self._response_active = False
        except ConnectionClosed as e:
            self.connected = False
            self.busy = False
            self._response_active = False
            self.speaker.finish_realtime()
            self.last_cycle_end_at = time.time()
            self._log_connection_error_deduped(str(e), "realtime_connection_closed")
            if self._should_reconnect:
                self._reconnect_task = asyncio.create_task(self._reconnect_with_backoff())
        except Exception as e:
            self.connected = False
            self.busy = False
            self._response_active = False
            self.speaker.finish_realtime()
            self.last_cycle_end_at = time.time()
            self._log_connection_error_deduped(str(e), "realtime_receiver_error")
            print("Receiver exception:", repr(e))
            if self._should_reconnect:
                self._reconnect_task = asyncio.create_task(self._reconnect_with_backoff())

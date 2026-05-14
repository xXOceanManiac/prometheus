from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

from audio import MicRecorder, Speaker
from config import CONFIG
from ptt import PushToTalkController
from realtime_client import RealtimePrometheusClient
from tools import ToolRegistry
from utils import log_event, notify
from visuals import VisualStateController
from wakeword import WakeWordDetector
from background_worker import BackgroundWorkerPool
from memory_core import query_vault
from session_summarizer import SessionSummarizer
from working_memory import WorkingMemory
from workspace.workspace_manager import WorkspaceManager
from prometheus_identity import build_system_prompt
from prometheus_profile import PrometheusProfile
from session_briefing import SessionBriefing
from proactive_loop import ProactiveLoop
from event_bus import get_bus, EventType
from sensor_manager import SensorManager


_PID_FILE = Path.home() / ".jarvis" / "prometheus.pid"


class PrometheusCore:
    def __init__(self) -> None:
        self.sample_rate_in = int(CONFIG.get("sample_rate_in", 16000))
        self.sample_rate_out = int(CONFIG.get("sample_rate_out", 24000))
        self.speaker_blocksize = int(CONFIG.get("speaker_blocksize", 2048))
        self.max_turn_seconds = float(CONFIG.get("max_turn_seconds", 12.0))
        self.mic_device = CONFIG.get("mic_device")

        self.wake_word_min_listen_seconds = float(
            CONFIG.get("wake_word_min_listen_seconds", 0.90)
        )
        self.wake_word_end_silence_seconds = float(
            CONFIG.get("wake_word_end_silence_seconds", 1.10)
        )
        self.wake_word_energy_threshold = float(
            CONFIG.get("wake_word_energy_threshold", 550.0)
        )

        self.visuals = VisualStateController()
        self.worker_pool = BackgroundWorkerPool(max_workers=4)
        self.session_summarizer = SessionSummarizer()
        self.workspace = WorkspaceManager(
            poll_interval=float(CONFIG.get("workspace_poll_interval", 5.0)),
            on_project_change=self._on_workspace_project_change,
            on_workspace_change=self._on_workspace_state_change,
        )
        self.speaker = Speaker(
            samplerate=self.sample_rate_out,
            blocksize=self.speaker_blocksize,
            state_callback=self.visuals.set_state,
        )
        self.mic = MicRecorder(samplerate=self.sample_rate_in, device=self.mic_device)
        self.tools = ToolRegistry()
        self.client = RealtimePrometheusClient(self.speaker, self.tools)
        self.wakeword = WakeWordDetector()

        self._pid_file: Path | None = None
        self.running = True
        self.user_turn_active = False
        self.user_turn_source = ""
        self.user_turn_started_at = 0.0
        self.last_voice_activity_at = 0.0
        self.loop: asyncio.AbstractEventLoop | None = None
        self.ptt = PushToTalkController(
            on_activated=self._on_ptt_activated, on_released=self._on_ptt_released
        )
        self.profile: Any = None
        self.briefing: SessionBriefing | None = None
        self.proactive_loop: ProactiveLoop | None = None
        self._sensor_manager: SensorManager | None = None

    def _acquire_pid_lock(self) -> None:
        try:
            _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
            if _PID_FILE.exists():
                try:
                    existing_pid = int(_PID_FILE.read_text().strip())
                    os.kill(existing_pid, 0)
                    print(f"Prometheus already running (PID {existing_pid}). Exiting.")
                    sys.exit(0)
                except (ProcessLookupError, ValueError, OSError):
                    pass  # stale pid file — safe to overwrite
            _PID_FILE.write_text(str(os.getpid()))
            self._pid_file = _PID_FILE
        except SystemExit:
            raise
        except Exception as exc:
            log_event("pid_lock_error", {"error": str(exc)})

    def _release_pid_lock(self) -> None:
        try:
            if self._pid_file and self._pid_file.exists():
                self._pid_file.unlink(missing_ok=True)
        except Exception:
            pass

    async def startup(self) -> None:
        self._acquire_pid_lock()
        self.loop = asyncio.get_running_loop()

        # Detect workspace and load vault BEFORE connect so context is in the
        # initial session.update (the one sent inside client.connect()).
        project = await self.loop.run_in_executor(None, self.workspace.detect_once)
        self.workspace.start()

        project_name = project.get("project_name", "")
        active_window = project.get("active_window") or {}
        win_title = (
            str(active_window.get("title", ""))
            if isinstance(active_window, dict)
            else ""
        )
        query_terms = [t for t in [project_name, win_title] if t]
        vault_query = " ".join(query_terms[:3]) or project_name
        vault_results: list = []
        if vault_query:
            try:
                vault_results = await self.loop.run_in_executor(
                    None, lambda: query_vault(vault_query, limit=5)
                )
            except Exception as exc:
                log_event("vault_load_error", {"error": str(exc)})

        # Build personal profile (cached daily)
        try:
            self.profile = await self.loop.run_in_executor(
                None, lambda: PrometheusProfile().load()
            )
        except Exception as exc:
            log_event("profile_load_error", {"error": str(exc)})

        # Load recent sessions for system prompt
        recent_sessions: list = []
        try:
            recent_sessions = await self.loop.run_in_executor(
                None, lambda: SessionBriefing.load_recent_sessions(n=3)
            )
        except Exception as exc:
            log_event("recent_sessions_load_error", {"error": str(exc)})

        # Build dynamic system prompt from all live context
        try:
            system_prompt = build_system_prompt(
                workspace=project,
                vault_context=vault_results,
                recent_sessions=recent_sessions,
                working_memory=WorkingMemory().read(),
                profile=self.profile.to_dict() if self.profile else {},
            )
            self.client.set_system_prompt(system_prompt)
        except Exception as exc:
            log_event("system_prompt_build_error", {"error": str(exc)})

        if vault_results:
            self.client.inject_vault_context(vault_results)
            WorkingMemory().write({"vault_context": vault_results})
            log_event("vault_context_loaded", {
                "project": project_name,
                "query": vault_query,
                "count": len(vault_results),
            })

        self.client.inject_workspace_context(project)

        # Connect now — session.update uses the pre-loaded context.
        await self.client.connect()

        # Start event bus and ambient sensor layer; subscribe to window changes
        try:
            await get_bus().start()
            self._sensor_manager = SensorManager()
            await self._sensor_manager.start()
            get_bus().subscribe(EventType.WINDOW_CHANGED, self._on_window_changed)
            log_event("sensor_manager_started", {})
        except Exception as exc:
            log_event("sensor_manager_start_error", {"error": str(exc)})

        # Start proactive loop and session briefing
        self.proactive_loop = ProactiveLoop(self.client, self.workspace)
        asyncio.create_task(self.proactive_loop.run())
        self.briefing = SessionBriefing(self.client)
        asyncio.create_task(self.briefing.fire_delayed(delay=3.0))

        self.mic.start()
        self.ptt.start()
        self.worker_pool.start(
            loop=self.loop,
            on_complete=self._on_background_task_complete,
        )
        self.tools.worker_pool = self.worker_pool
        asyncio.create_task(self._heartbeat_loop())

        self._set_idle_visual_state()
        status = "wake word armed" if self.wakeword.is_ready else "PTT ready"
        notify(f"Prometheus started. {status}")
        log_event(
            "prometheus_started",
            {
                "wake_word_ready": self.wakeword.is_ready,
                "wake_word_error": self.wakeword.error,
            },
        )

    async def shutdown(self) -> None:
        self.running = False
        if self.proactive_loop:
            self.proactive_loop.stop()
        self.ptt.stop()
        self.mic.stop()
        self.speaker.stop()
        self.wakeword.close()
        self.workspace.stop()
        if self._sensor_manager:
            try:
                await self._sensor_manager.stop()
            except Exception:
                pass
        try:
            await get_bus().stop()
        except Exception:
            pass
        await self.client.close()
        self.visuals.set_state("idle")

        # Write session summary to vault before exiting — never block shutdown
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, self.session_summarizer.summarize_and_write),
                timeout=15.0,
            )
        except Exception as exc:
            log_event("session_summarizer_shutdown_error", {"error": str(exc)})

        self.worker_pool.shutdown(wait=False, cancel_futures=True)
        self._release_pid_lock()
        log_event("prometheus_stopped", {})

    async def _heartbeat_loop(self) -> None:
        _hb = Path.home() / ".jarvis" / "heartbeat.json"
        while self.running:
            try:
                tmp = _hb.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps({
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "pid": os.getpid(),
                    }),
                    encoding="utf-8",
                )
                os.replace(tmp, _hb)
            except Exception as exc:
                log_event("heartbeat_error", {"error": str(exc)})
            await asyncio.sleep(5.0)

    def _on_background_task_complete(self, result: dict) -> None:
        description = str(result.get("description", "task"))[:50]
        ok = result.get("ok", False)
        status = "complete" if ok else "failed"
        notify(f"Background {status}: {description}")
        log_event("background_task_complete_notified", {
            "ok": ok,
            "description": description,
            "cycles": result.get("cycles", 0),
        })
        if self.loop and self.loop.is_running():
            result_copy = dict(result)
            self.loop.call_soon_threadsafe(
                lambda: asyncio.create_task(
                    self._announce_background_task_complete(result_copy)
                )
            )

    async def _announce_background_task_complete(self, result: dict) -> None:
        """Speak a verbal announcement of the completed background task."""
        if not self.client.connected:
            return
        if self.client.busy or self.user_turn_active:
            log_event("background_task_announce_skipped", {
                "busy": self.client.busy,
                "user_turn": self.user_turn_active,
            })
            return
        description = str(result.get("description", "task"))[:60]
        ok = result.get("ok", False)
        result_summary = str(result.get("result_summary") or result.get("message") or "")[:200]
        output_path = str(result.get("output_path") or "")
        if ok:
            msg = f"Background task complete: {description}."
            if output_path:
                msg += f" Output written to {output_path}."
            elif result_summary:
                msg += f" {result_summary}"
        else:
            msg = f"Background task failed: {description}. {result_summary[:100]}"
        try:
            await self.client.send({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "system",
                    "content": [{"type": "input_text", "text": f"[BACKGROUND_COMPLETE] {msg}"}],
                },
            })
            await self.client.send({
                "type": "response.create",
                "response": {
                    "modalities": ["audio", "text"],
                    "instructions": f"Announce this background task result in one sentence: {msg}",
                },
            })
            log_event("background_task_announced", {"ok": ok, "description": description})
        except Exception as exc:
            log_event("background_task_announce_error", {"error": str(exc)})

    def _on_workspace_project_change(self, project_name: str, project_path: str) -> None:
        """Called from the workspace thread when the active project changes."""
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(
                lambda: asyncio.create_task(
                    self._refresh_vault_for_project(project_name, project_path)
                )
            )

    async def _refresh_vault_for_project(self, project_name: str, project_path: str) -> None:
        """Re-query vault and push fresh context to the Realtime session."""
        if not project_name:
            return
        try:
            results = await self.loop.run_in_executor(
                None, lambda: query_vault(project_name, limit=5)
            )
            if results:
                self.client.inject_vault_context(results)
                WorkingMemory().write({"vault_context": results})
                log_event("vault_context_refreshed", {"project": project_name, "count": len(results)})
            # Read full visual state to include xbox and window info
            try:
                import json as _json
                vs = _json.loads((Path.home() / ".jarvis" / "visual_state.json").read_text(encoding="utf-8"))
            except Exception:
                vs = {}
            workspace = {
                "project_name": project_name,
                "active_project_path": project_path,
                "active_window": vs.get("active_window"),
                "xbox_state": vs.get("xbox_state"),
                "xbox_app": vs.get("xbox_app"),
                "xbox_media_title": vs.get("xbox_media_title"),
            }
            self.client.inject_workspace_context(workspace)
            await self.client._update_session_instructions()
        except Exception as exc:
            log_event("vault_refresh_error", {"error": str(exc)})

    def _on_workspace_state_change(self, workspace_state: dict) -> None:
        """Called from workspace thread when xbox state changes significantly."""
        if self.loop and self.loop.is_running():
            state_copy = dict(workspace_state)
            self.loop.call_soon_threadsafe(
                lambda: asyncio.create_task(
                    self._update_workspace_context_async(state_copy)
                )
            )

    async def _update_workspace_context_async(self, workspace_state: dict) -> None:
        try:
            self.client.inject_workspace_context(workspace_state)
            await self.client._update_session_instructions()
            log_event("workspace_context_updated", {
                "xbox_state": workspace_state.get("xbox_state"),
                "xbox_app": workspace_state.get("xbox_app"),
            })
        except Exception as exc:
            log_event("workspace_context_update_error", {"error": str(exc)})

    def _on_window_changed(self, event: Any) -> None:
        """Called from the event bus when active window changes. Refreshes session context."""
        if not self.loop or not self.loop.is_running():
            return
        try:
            payload = event.payload or {}
            current = self.workspace.current_project()
            workspace = {
                "project_name": current.get("project_name", ""),
                "active_project_path": current.get("project_path", ""),
                "active_window": {"title": payload.get("window_title", "")},
                "xbox_state": current.get("xbox_state"),
                "xbox_app": current.get("xbox_app"),
                "xbox_media_title": current.get("xbox_media_title"),
            }
            self.client.inject_workspace_context(workspace)
            self.loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self.client._update_session_instructions())
            )
        except Exception as exc:
            log_event("window_changed_handler_error", {"error": str(exc)})

    def _set_idle_visual_state(self) -> None:
        if not self.speaker.is_speaking and not self.client.busy:
            self.visuals.set_state("armed")
        else:
            self.visuals.set_state("idle")

    def _chunk_rms(self, chunk: np.ndarray) -> float:
        if chunk is None or chunk.size == 0:
            return 0.0
        arr = chunk.astype(np.float32)
        return float(np.sqrt(np.mean(arr * arr)))

    def _on_ptt_activated(self) -> None:
        log_event("ptt_activated", {
            "is_speaking": self.speaker.is_speaking,
            "client_busy": self.client.busy,
            "user_turn_active": self.user_turn_active,
        })
        if self.loop:
            self.loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._begin_turn("ptt"))
            )

    def _on_ptt_released(self) -> None:
        if self.loop:
            self.loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._commit_turn("ptt_release"))
            )

    async def _interrupt_assistant(self) -> None:
        log_event("interrupt_triggered", {
            "is_speaking": self.speaker.is_speaking,
            "client_busy": self.client.busy,
        })
        self.speaker.force_stop()
        await asyncio.sleep(0.05)
        try:
            await self.client.interrupt()
        except Exception as e:
            log_event("interrupt_error", {"error": str(e)})
        self.mic.drain()
        self.visuals.set_state("listening")
        log_event("barge_in", {})

    async def _begin_turn(self, source: str) -> None:
        if self.user_turn_active:
            return

        # Note voice activity for proactive loop and cancel startup briefing
        if self.proactive_loop:
            self.proactive_loop.note_voice_activity()
        if self.briefing:
            self.briefing.cancel()

        if self.speaker.is_speaking or self.client.busy:
            await self._interrupt_assistant()

        self.mic.drain()
        await self.client.begin_user_turn()
        self.user_turn_active = True
        self.user_turn_source = source
        self.user_turn_started_at = time.time()
        self.last_voice_activity_at = self.user_turn_started_at
        self.visuals.set_state("listening")
        if source == "wakeword":
            notify("Wake word detected")
            log_event("wakeword_turn_started", {})
        else:
            notify("Listening")
            log_event("ptt_turn_started", {})

    async def _commit_turn(self, reason: str) -> None:
        if not self.user_turn_active:
            return
        source = self.user_turn_source
        self.user_turn_active = False
        self.user_turn_source = ""
        self.visuals.set_state("processing")
        await self.client.end_audio()
        self.mic.drain()
        log_event("turn_committed", {"reason": reason, "source": source})

    async def _handle_active_turn(self, chunk: np.ndarray) -> None:
        if not self.user_turn_active:
            return
        await self.client.send_audio(chunk.tobytes())
        now = time.time()
        if self._chunk_rms(chunk) >= self.wake_word_energy_threshold:
            self.last_voice_activity_at = now

        if (now - self.user_turn_started_at) >= self.max_turn_seconds:
            await self._commit_turn("max_turn_seconds")
            return

        if self.user_turn_source == "wakeword":
            elapsed = now - self.user_turn_started_at
            silent_for = now - self.last_voice_activity_at
            if (
                elapsed >= self.wake_word_min_listen_seconds
                and silent_for >= self.wake_word_end_silence_seconds
            ):
                await self._commit_turn("wakeword_silence")

    async def _handle_idle_chunk(self, chunk: np.ndarray) -> None:
        if self.user_turn_active:
            return
        if self.wakeword.is_ready and self.wakeword.process(chunk):
            await self._begin_turn("wakeword")

    async def run(self) -> None:
        await self.startup()
        try:
            while self.running:
                chunk = self.mic.read_chunk(timeout=0.05)
                if chunk is None:
                    await asyncio.sleep(0.01)
                    continue
                if self.user_turn_active:
                    await self._handle_active_turn(chunk)
                else:
                    await self._handle_idle_chunk(chunk)
                    if (
                        not self.speaker.is_speaking
                        and not self.client.busy
                        and not self.user_turn_active
                    ):
                        self._set_idle_visual_state()
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()


async def amain() -> None:
    from prometheus.infra.paths import ensure_runtime_dirs
    ensure_runtime_dirs()
    await PrometheusCore().run()


if __name__ == "__main__":
    asyncio.run(amain())

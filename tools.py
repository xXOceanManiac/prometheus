from __future__ import annotations
import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import requests
from config import CONFIG, VISUAL_STATE_PATH
from memory import MemoryStore
from episodic_memory import EpisodicMemory
from semantic_memory import SemanticMemory
from procedural_memory import ProceduralMemory
from working_memory import WorkingMemory
from dream_manager import DreamManager
from behavior_learning import BehaviorLearningEngine
from utils import command_exists, ensure_dir, kill_existing, log_event, run_cmd
from workspace_policy import resolve_workspace_path, ensure_workspace_root


# Module-level voice error callback — set by realtime client at startup
_voice_error_callback = None


def set_voice_error_callback(cb) -> None:
    global _voice_error_callback
    _voice_error_callback = cb


def notify_voice_error(action: str, error: str) -> None:
    """Send a spoken error notification through the Realtime API if connected."""
    if _voice_error_callback is None:
        return
    try:
        _voice_error_callback(str(action)[:60], str(error)[:80])
    except Exception:
        pass


@dataclass
class ToolResult:
    ok: bool
    message: str
    data: dict[str, Any] | None = None


HARDCODED_HA_SCRIPTS: dict[str, str] = {
    # Lights
    "lights on": "jarvis_lights_power_on",
    "lights off": "jarvis_lights_power_off",
    "dim lights": "jarvis_lights_brightness_down",
    "brighten lights": "jarvis_lights_brightness_up",
    "movie mode": "jarvis_lights_scene_movie",
    "work mode": "jarvis_lights_scene_work",
    "night mode": "jarvis_lights_scene_night",
    "disco mode": "jarvis_lights_scene_disco",
    "red lights": "jarvis_lights_scene_red",
    "blue lights": "jarvis_lights_scene_blue",
    "green lights": "jarvis_lights_scene_green",
    "purple lights": "jarvis_lights_scene_purple",
    # Xbox
    "turn on xbox": "jarvis_xbox_power_on",
    "turn off xbox": "jarvis_xbox_power_off",
    "open youtube": "jarvis_xbox_app_youtube",
    "open netflix": "jarvis_xbox_app_netflix",
    "open spotify": "jarvis_xbox_app_spotify",
    "pause": "jarvis_xbox_media_pause",
    "resume": "jarvis_xbox_media_resume",
    "volume up": "jarvis_xbox_volume_up",
    "volume down": "jarvis_xbox_volume_down",
    # Routines
    "watch youtube": "jarvis_routine_watch_youtube",
    "watch netflix": "jarvis_routine_watch_netflix",
    "play spotify": "jarvis_routine_play_spotify",
    "good night": "jarvis_routine_good_night",
}
EXACT_HA_SCRIPT_NAMES = set(HARDCODED_HA_SCRIPTS.values())

# Ordered fallback binary candidates for apps that may not be in the user config
_BUILTIN_APP_CMDS: dict[str, list[str]] = {
    "code":                ["code", "code-insiders", "flatpak run com.visualstudio.code"],
    "vscode":              ["code", "code-insiders", "flatpak run com.visualstudio.code"],
    "visual studio code":  ["code", "code-insiders"],
    "vs code":             ["code", "code-insiders"],
    "visual studio":       ["code", "code-insiders"],
    "dolphin":             ["dolphin", "nautilus", "xdg-open"],
    "files":               ["dolphin", "nautilus", "nemo", "thunar"],
    "file manager":        ["dolphin", "nautilus"],
    "my files":            ["dolphin", "nautilus"],
    "file explorer":       ["dolphin", "nautilus"],
    "spotify":             ["spotify", "flatpak run com.spotify.Client"],
    "firefox":             ["firefox"],
    "chrome":              ["google-chrome", "chromium"],
    "chromium":            ["chromium", "google-chrome"],
    "terminal":            ["konsole", "gnome-terminal", "xterm"],
}

# Process names for pgrep running-check
_APP_PROCESS_NAMES: dict[str, str] = {
    "spotify":  "spotify",
    "firefox":  "firefox",
    "code":     "code",
    "vscode":   "code",
    "dolphin":  "dolphin",
    "chromium": "chromium",
    "chrome":   "chrome",
}


def run_ha_script(script_name: str) -> ToolResult:
    import os
    import requests

    token = os.getenv("HOME_ASSISTANT_API_KEY", "").strip()
    if not token:
        return ToolResult(False, "HOME_ASSISTANT_API_KEY is not set.")

    base_url = (
        os.getenv("HOME_ASSISTANT_URL", "http://localhost:8123").strip().rstrip("/")
    )
    if not base_url:
        return ToolResult(False, "HOME_ASSISTANT_URL is not set.")

    url = f"{base_url}/api/services/script/turn_on"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    data = {"entity_id": f"script.{script_name}"}

    try:
        response = requests.post(url, headers=headers, json=data, timeout=5)
        if response.status_code >= 400:
            return ToolResult(
                False,
                f"Home Assistant error {response.status_code}: {response.text[:300]}",
            )
        return ToolResult(True, f"Executed Home Assistant script: {script_name}")
    except Exception as e:
        return ToolResult(False, f"Failed to run Home Assistant script: {e}")


ACTION_ENUM = [
    "open_app",
    "close_app",
    "open_url_key",
    "open_url_keys",
    "open_url_raw",
    "web_search",
    "open_code_folder",
    "open_terminal_here",
    "smart_action",
    "summarize_screen",
    "save_context",
    "resume_last_context",
    "run_routine",
    "save_routine",
    "backfill_memory",
    "run_dream_pass",
    "run_ha_script",
    "list_windows",
    "get_active_window",
    "desktop_state",
    "screen_context",
    "list_files",
    "read_file",
    "write_file",
    "mode_lock_in",
    "volume_change",
    "volume_set",
    "mute_toggle",
    "screenshot",
    "tell_time",
    "projector_on",
    "projector_off",
    "sleep",
    "restart",
    "shutdown",
    "confirm_pending",
    "cancel_pending",
    "background_task",
    # Code / git tools
    "run_python",
    "run_shell",
    "show_logs",
    "search_codebase",
    "git_status",
    "git_diff",
    "git_commit",
    # Session / awareness tools
    "session_wrapup",
    "system_status",
    "get_priorities",
    # Autonomous coding tools
    "start_coding_task",
    "get_coding_status",
    # Orchestrated build tools
    "start_build",
    "get_build_status",
    # Vault / personal memory query
    "query_vault",
    # System diagnostics
    "run_diagnostics",
    # Mission state
    "get_mission_status",
    "set_mission",
    "add_subtask",
    "complete_subtask",
    # Calendar reads (read-only, no writes)
    "calendar_list_upcoming",
    "calendar_get_today",
    "calendar_get_tomorrow",
    "calendar_get_date",
    "calendar_next_event",
    "calendar_summarize_day",
    "calendar_find_free_blocks",
]


def _step_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ACTION_ENUM},
            "app": {"type": "string"},
            "apps": {"type": "array", "items": {"type": "string"}},
            "url_key": {"type": "string"},
            "url_keys": {"type": "array", "items": {"type": "string"}},
            "url": {"type": "string"},
            "urls": {"type": "array", "items": {"type": "string"}},
            "query": {"type": "string"},
            "delta": {"type": "integer"},
            "value": {"type": "integer"},
            "project_path": {"type": "string"},
            "context_name": {"type": "string"},
            "routine_name": {"type": "string"},
            "script_name": {"type": "string"},
            "notes": {"type": "string"},
            "layout": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "description": {"type": "string"},
            "include_screenshot": {"type": "boolean"},
            "path": {"type": "string"},
            "content": {"type": "string"},
            "command": {"type": "string"},
            "file": {"type": "string"},
            "message": {"type": "string"},
            "confirmed": {"type": "boolean"},
            "goal": {"type": "string"},
            "context": {"type": "string"},
            "date": {"type": "string"},
            "max_results": {"type": "integer"},
            "minimum_minutes": {"type": "integer"},
            "days": {"type": "integer"},
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ACTION_ENUM},
                        "app": {"type": "string"},
                        "apps": {"type": "array", "items": {"type": "string"}},
                        "url_key": {"type": "string"},
                        "url_keys": {"type": "array", "items": {"type": "string"}},
                        "url": {"type": "string"},
                        "urls": {"type": "array", "items": {"type": "string"}},
                        "query": {"type": "string"},
                        "delta": {"type": "integer"},
                        "value": {"type": "integer"},
                        "project_path": {"type": "string"},
                        "context_name": {"type": "string"},
                        "routine_name": {"type": "string"},
                        "script_name": {"type": "string"},
                        "notes": {"type": "string"},
                        "layout": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "description": {"type": "string"},
                        "include_screenshot": {"type": "boolean"},
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }


def run_diagnostics() -> dict:
    """
    Run a full system health check and return status for all subsystems.
    Written to working_memory["last_diagnostic"] and voiced as a summary.
    """
    import shutil as _shutil
    import datetime as _dt

    result: dict = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    # voice
    try:
        from working_memory import WorkingMemory
        wm = WorkingMemory().read()
        result["voice"] = {
            "connected": bool(wm.get("voice_connected", True)),
            "last_response_ts": str(wm.get("last_voice_response_ts", "")),
        }
    except Exception as e:
        result["voice"] = {"connected": False, "error": str(e)[:100]}

    # ollama
    try:
        import requests as _req
        t0 = time.time()
        r = _req.get("http://localhost:11434/api/tags", timeout=5)
        latency_ms = round((time.time() - t0) * 1000)
        if r.status_code == 200:
            models = [m.get("name", "") for m in r.json().get("models", [])]
            result["ollama"] = {"available": True, "models": models, "latency_ms": latency_ms}
        else:
            result["ollama"] = {"available": False, "models": [], "latency_ms": latency_ms}
    except Exception:
        result["ollama"] = {"available": False, "models": [], "latency_ms": -1}

    # claude_code
    try:
        from working_memory import WorkingMemory
        wm = WorkingMemory().read()
        result["claude_code"] = {
            "on_path": _shutil.which("claude") is not None,
            "last_result": str(wm.get("last_coding_result", ""))[:80],
        }
    except Exception as e:
        result["claude_code"] = {"on_path": _shutil.which("claude") is not None, "error": str(e)[:80]}

    # vault
    try:
        import os as _os
        import sqlite3 as _sqlite3
        from config import CONFIG
        _checked_at = _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        # Resolve vault path: config → env → candidate scan
        vault_path = str(CONFIG.get("vault_path", "") or "").strip()
        if not vault_path:
            vault_path = _os.environ.get("VAULT_PATH", "").strip()
        _candidates = [
            vault_path,
            str(Path.home() / "Desktop" / "Tates Brain"),
            str(Path.home() / "Desktop" / "Tates_Brain"),
            str(Path.home() / "Tates_Brain"),
            str(Path.home() / "Tates Brain"),
        ]
        resolved_path = ""
        db_path: Path | None = None
        for _cand in _candidates:
            if not _cand:
                continue
            _db = Path(_cand) / "data" / "memory.db"
            if _db.exists():
                resolved_path = _cand
                db_path = _db
                break
        db_exists = db_path is not None and db_path.exists()
        active = db_exists
        readable = False
        chunk_count = 0
        last_indexed = ""
        reason = ""
        if db_exists and db_path is not None:
            try:
                with _sqlite3.connect(str(db_path)) as conn:
                    row = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
                    chunk_count = int(row[0]) if row else 0
                readable = True
            except Exception as _ve:
                readable = False
                reason = str(_ve)[:80]
            try:
                mtime = db_path.stat().st_mtime
                last_indexed = _dt.datetime.fromtimestamp(mtime).strftime("%Y-%m-%dT%H:%M:%S")
            except Exception:
                pass
        else:
            reason = (
                f"Not found in candidates: {[c for c in _candidates if c]}"[:120]
            )
        result["vault"] = {
            "active": active,
            "db_exists": db_exists,
            "path": resolved_path or "(not found)",
            "exists": db_exists,
            "readable": readable,
            "chunk_count": chunk_count,
            "last_indexed": last_indexed,
            "reason": reason,
            "checked_at": _checked_at,
        }
    except Exception as e:
        result["vault"] = {
            "active": False,
            "db_exists": False,
            "path": "",
            "exists": False,
            "readable": False,
            "chunk_count": 0,
            "reason": str(e)[:80],
            "checked_at": "",
        }

    # background_workers
    try:
        tasks_file = Path.home() / ".jarvis" / "background_tasks.json"
        active_tasks = 0
        stuck_tasks = 0
        if tasks_file.exists():
            import json as _json
            data = _json.loads(tasks_file.read_text(encoding="utf-8"))
            tasks = data.get("tasks") or []
            now_ts = time.time()
            for t in tasks:
                if not isinstance(t, dict):
                    continue
                if t.get("status") == "running":
                    active_tasks += 1
                    started_at = str(t.get("started_at", ""))
                    if started_at:
                        try:
                            started_epoch = _dt.datetime.fromisoformat(started_at).timestamp()
                            if (now_ts - started_epoch) / 60.0 > 5.0:
                                stuck_tasks += 1
                        except Exception:
                            pass
        result["background_workers"] = {
            "active_tasks": active_tasks,
            "stuck_tasks": stuck_tasks,
        }
    except Exception as e:
        result["background_workers"] = {"active_tasks": 0, "stuck_tasks": 0, "error": str(e)[:80]}

    # git
    try:
        from git_safety import GitSafety
        gs = GitSafety()
        sha = gs.last_checkpoint_sha()
        git_r = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
            cwd=str(Path(__file__).parent),
        )
        uncommitted = len([l for l in git_r.stdout.splitlines() if l.strip()]) if git_r.returncode == 0 else 0
        result["git"] = {
            "last_checkpoint": sha or "none",
            "uncommitted_changes": uncommitted,
            "claude_code_on_path": _shutil.which("claude") is not None,
        }
    except Exception as e:
        result["git"] = {"last_checkpoint": "unknown", "uncommitted_changes": 0, "error": str(e)[:80]}

    # cost
    try:
        from cost_tracker import CostTracker
        ct = CostTracker()
        summary = ct.session_summary()
        pct = round(summary["daily_total"] / ct.daily_limit_usd * 100, 1) if ct.daily_limit_usd > 0 else 0.0
        result["cost"] = {
            "session_usd": summary["session_total"],
            "daily_usd": summary["daily_total"],
            "daily_limit_usd": ct.daily_limit_usd,
            "pct_used": pct,
        }
    except Exception as e:
        result["cost"] = {"session_usd": 0.0, "daily_usd": 0.0, "daily_limit_usd": 5.0, "pct_used": 0.0, "error": str(e)[:80]}

    # system
    try:
        import psutil as _psutil
        result["system"] = {
            "cpu_pct": _psutil.cpu_percent(interval=None),
            "ram_pct": _psutil.virtual_memory().percent,
            "disk_pct": _psutil.disk_usage("/").percent,
        }
    except Exception:
        result["system"] = {"cpu_pct": 0.0, "ram_pct": 0.0, "disk_pct": 0.0}

    # watchdog
    try:
        from working_memory import WorkingMemory
        import datetime as _dt2
        wm = WorkingMemory().read()
        ts = str(wm.get("watchdog_last_check_ts", ""))
        if ts:
            try:
                age_s = time.time() - _dt2.datetime.fromisoformat(ts).timestamp()
                w_status = "healthy" if age_s < 120 else "stale"
            except Exception:
                w_status = "pending"
        else:
            w_status = "pending"  # just started, first cycle hasn't fired yet
        result["watchdog"] = {"last_check_ts": ts, "status": w_status}
    except Exception as e:
        result["watchdog"] = {"last_check_ts": "", "status": "pending", "error": str(e)[:80]}

    # proactive_loop
    try:
        log_dir = Path.home() / ".jarvis" / "logs"
        cycles = 0
        last_fired = ""
        if log_dir.exists():
            import json as _json
            files = sorted(log_dir.glob("*.jsonl"))
            if files:
                lines = files[-1].read_text(encoding="utf-8", errors="ignore").splitlines()
                for raw in lines:
                    try:
                        rec = _json.loads(raw)
                        if rec.get("kind") == "proactive_loop_cycle":
                            cycles += 1
                            last_fired = str(rec.get("ts", ""))
                    except Exception:
                        pass
        result["proactive_loop"] = {
            "last_fired_ts": last_fired,
            "cycles_this_session": cycles,
        }
    except Exception as e:
        result["proactive_loop"] = {"last_fired_ts": "", "cycles_this_session": 0, "error": str(e)[:80]}

    # Build spoken summary
    healthy = 0
    warnings = 0
    critical = 0
    warning_msgs = []
    critical_msgs = []

    if result.get("voice", {}).get("connected", True):
        healthy += 1
    else:
        warnings += 1
        warning_msgs.append("voice disconnected")

    ollama = result.get("ollama", {})
    if ollama.get("available"):
        latency = ollama.get("latency_ms", 0)
        if latency > 3000:
            warnings += 1
            warning_msgs.append(f"Ollama latency is {latency}ms")
        else:
            healthy += 1
    else:
        warnings += 1
        warning_msgs.append("Ollama unavailable")

    if result.get("claude_code", {}).get("on_path"):
        healthy += 1
    else:
        critical += 1
        critical_msgs.append("Claude Code not found on PATH")

    if result.get("vault", {}).get("db_exists"):
        healthy += 1
    else:
        critical += 1
        critical_msgs.append("vault database missing")

    workers = result.get("background_workers", {})
    stuck = workers.get("stuck_tasks", 0)
    if stuck > 0:
        warnings += 1
        warning_msgs.append(f"{stuck} task(s) stuck over 5 minutes")
    else:
        healthy += 1

    healthy += 1  # git always counts

    cost = result.get("cost", {})
    pct = cost.get("pct_used", 0)
    if pct > 80:
        critical += 1
        critical_msgs.append(f"at {pct:.0f}% of daily cost limit")
    elif pct > 50:
        warnings += 1
        warning_msgs.append(f"at {pct:.0f}% of daily cost limit")
    else:
        healthy += 1

    watchdog_status = result.get("watchdog", {}).get("status", "pending")
    if watchdog_status == "stale":
        warnings += 1
        warning_msgs.append("watchdog hasn't checked in over 2 minutes")
    else:
        healthy += 1  # healthy or pending both count fine

    healthy += 1  # proactive loop

    healthy += 1  # system

    if critical > 0:
        spoken = f"{critical} critical issue{'s' if critical > 1 else ''}: " + ", ".join(critical_msgs)
        if warnings:
            spoken += f"; {warnings} warning{'s' if warnings > 1 else ''}: " + ", ".join(warning_msgs)
    elif warnings > 0:
        spoken = f"{warnings} warning{'s' if warnings > 1 else ''}: " + ", ".join(warning_msgs)
    else:
        spoken = f"All systems nominal — {healthy} systems healthy."

    result["spoken_summary"] = spoken

    # Write to working memory
    try:
        from working_memory import WorkingMemory
        WorkingMemory().write({"last_diagnostic": result})
    except Exception:
        pass

    log_event("diagnostics_run", {
        "healthy": healthy,
        "warnings": warnings,
        "critical": critical,
        "summary": spoken[:120],
    })

    return result


class ToolRegistry:
    def __init__(self) -> None:
        self.pending_action: dict[str, Any] | None = None
        self.pending_expires_at: float = 0.0
        self.confirmation_timeout_seconds = 12.0
        self.memory = MemoryStore()
        self.episodes = EpisodicMemory()
        self.semantic = SemanticMemory()
        self.procedural = ProceduralMemory()
        self.working = WorkingMemory()
        self.dream = DreamManager()
        self.behavior = BehaviorLearningEngine(
            memory=self.memory,
            semantic=self.semantic,
            procedural=self.procedural,
            working=self.working,
            episodes=self.episodes,
        )
        self.worker_pool: Any | None = None  # set by main.py after pool starts

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": "desktop_action",
                "description": (
                    "Execute local desktop actions. Supports single actions, action arrays, "
                    "persistent work contexts, named routines, Home Assistant scripts, "
                    "desktop awareness, file access, and memory operations."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ACTION_ENUM},
                        "actions": {
                            "type": "array",
                            "items": _step_schema(),
                            "minItems": 1,
                        },
                        "app": {"type": "string"},
                        "apps": {"type": "array", "items": {"type": "string"}},
                        "url_key": {"type": "string"},
                        "url_keys": {"type": "array", "items": {"type": "string"}},
                        "url": {"type": "string"},
                        "urls": {"type": "array", "items": {"type": "string"}},
                        "query": {"type": "string"},
                        "delta": {"type": "integer"},
                        "value": {"type": "integer"},
                        "project_path": {"type": "string"},
                        "context_name": {"type": "string"},
                        "routine_name": {"type": "string"},
                        "script_name": {"type": "string"},
                        "notes": {"type": "string"},
                        "layout": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "description": {"type": "string"},
                        "include_screenshot": {"type": "boolean"},
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                        "command": {"type": "string"},
                        "file": {"type": "string"},
                        "message": {"type": "string"},
                        "confirmed": {"type": "boolean"},
                        "date": {"type": "string"},
                        "max_results": {"type": "integer"},
                        "minimum_minutes": {"type": "integer"},
                        "days": {"type": "integer"},
                        "steps": {
                            "type": "array",
                            "items": _step_schema(),
                            "minItems": 1,
                        },
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            }
        ]

    def _episode(
        self,
        kind: str,
        summary: str,
        *,
        tags: list[str] | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        try:
            self.episodes.append(kind, summary, tags=tags or [], data=data or {})
        except Exception:
            pass

    def _remember_last_request(self, payload: dict[str, Any]) -> None:
        try:
            request_text = str(payload.get("request_text", "")).strip()
            if request_text:
                self.working.set_user_request(request_text)
        except Exception:
            pass

    def _remember_tool_result(
        self,
        payload: dict[str, Any],
        result: ToolResult,
    ) -> ToolResult:
        action = str(payload.get("action", "")).strip() or "multi_action"
        try:
            self.working.set_tool_result(
                action=action,
                ok=result.ok,
                message=result.message,
                data=result.data or {},
            )
        except Exception:
            pass
        return result

    def _set_pending(self, payload: dict[str, Any]) -> ToolResult:
        self.pending_action = payload
        self.pending_expires_at = time.time() + self.confirmation_timeout_seconds
        try:
            self.working.write(
                {
                    "pending_confirmation": str(payload.get("action", "")).strip(),
                }
            )
        except Exception:
            pass
        return ToolResult(True, f"Awaiting confirmation for {payload['action']}.")

    def _clear_pending(self) -> None:
        self.pending_action = None
        self.pending_expires_at = 0.0
        try:
            self.working.write({"pending_confirmation": ""})
        except Exception:
            pass

    def _resolve_pending(self, confirm: bool) -> ToolResult:
        if not self.pending_action:
            return ToolResult(False, "There is no pending action to confirm.")
        if time.time() > self.pending_expires_at:
            self._clear_pending()
            return ToolResult(False, "The pending confirmation expired.")
        if not confirm:
            self._clear_pending()
            return ToolResult(True, "Cancelled.")
        payload = self.pending_action
        self._clear_pending()
        return self.execute(payload)

    def _launch(self, cmd: str, cwd: str | None = None) -> None:
        try:
            subprocess.Popen(
                shlex.split(cmd),
                cwd=cwd or None,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            log_event("launch_error", {"cmd": cmd, "error": str(exc)})
            raise

    def _launch_with_fallback(self, app_key: str, cwd: str | None = None) -> ToolResult:
        """Try app from config first, then _BUILTIN_APP_CMDS candidates."""
        # Config takes priority
        cmd = CONFIG.get("apps", {}).get(app_key)
        if cmd:
            try:
                self._launch(cmd, cwd=cwd)
                return ToolResult(True, f"Opened {app_key}.")
            except Exception as exc:
                log_event("open_app_config_failed", {"app": app_key, "cmd": cmd, "error": str(exc)})

        # Try builtin candidates in order
        candidates = _BUILTIN_APP_CMDS.get(app_key) or _BUILTIN_APP_CMDS.get(app_key.replace("-", " ")) or []
        for candidate in candidates:
            binary = shlex.split(candidate)[0]
            if binary == "xdg-open":
                # xdg-open doesn't need which check
                try:
                    subprocess.Popen(
                        [binary, str(Path.home())],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    return ToolResult(True, f"Opened {app_key} via {binary}.")
                except Exception as exc:
                    log_event("open_app_fallback_failed", {"app": app_key, "binary": binary, "error": str(exc)})
                    continue
            if shutil.which(binary):
                try:
                    self._launch(candidate, cwd=cwd)
                    return ToolResult(True, f"Opened {app_key} via {binary}.")
                except Exception as exc:
                    log_event("open_app_fallback_failed", {"app": app_key, "binary": binary, "error": str(exc)})
                    continue

        return ToolResult(False, f"Could not find a way to open {app_key}. Checked config and built-in fallbacks.")

    def _open_app_key(self, app_key: str, cwd: str | None = None) -> ToolResult:
        cmd = CONFIG.get("apps", {}).get(app_key)
        if not cmd:
            return ToolResult(False, f"Unknown app: {app_key}")
        self._launch(cmd, cwd=cwd)
        return ToolResult(True, f"Opened {app_key}.")

    def _resolve_project_path(self, value: str) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        projects = CONFIG.get("projects", {})
        if raw in projects and isinstance(projects[raw], dict):
            return str(projects[raw].get("path", "")).strip()
        return str(Path(raw).expanduser())

    def _run_layout_hook(self, context: dict[str, Any]) -> None:
        script = Path(str(CONFIG.get("layout_script", "")).strip()).expanduser()
        if not script.exists() or not os.access(script, os.X_OK):
            return
        env = os.environ.copy()
        env["JARVIS_CONTEXT_JSON"] = json.dumps(context)
        subprocess.Popen(
            [str(script)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )

    def _capture_screenshot(self) -> tuple[bool, str]:
        shot_dir = ensure_dir(CONFIG.get("screenshot_dir", "~/Pictures/Screenshots"))
        filename = shot_dir / f"screenshot-{time.strftime('%Y%m%d-%H%M%S')}.png"
        if command_exists("gnome-screenshot"):
            run_cmd(["gnome-screenshot", "-f", str(filename)])
        elif command_exists("spectacle"):
            run_cmd(["spectacle", "-b", "-n", "-o", str(filename)])
        elif command_exists("grim"):
            run_cmd(["grim", str(filename)])
        else:
            return False, "No supported screenshot tool found."
        return True, str(filename)

    def _get_active_window(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "window_id": "",
            "pid": "",
            "title": "",
            "class": "",
            "workspace": None,
        }
        if command_exists("xdotool"):
            r = run_cmd(["xdotool", "getactivewindow"], capture=True)
            if r.returncode == 0:
                wid = r.stdout.strip()
                data["window_id"] = wid
                r_name = run_cmd(["xdotool", "getwindowname", wid], capture=True)
                if r_name.returncode == 0:
                    data["title"] = r_name.stdout.strip()
                r_pid = run_cmd(["xdotool", "getwindowpid", wid], capture=True)
                if r_pid.returncode == 0:
                    data["pid"] = r_pid.stdout.strip()
        if command_exists("wmctrl"):
            r = run_cmd(["wmctrl", "-lx"], capture=True)
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    parts = line.split(None, 4)
                    if len(parts) < 5:
                        continue
                    wid, workspace, wm_class, host, title = parts
                    if wid.lower() == str(data["window_id"]).lower():
                        data["workspace"] = workspace
                        data["class"] = wm_class
                        if not data["title"]:
                            data["title"] = title
                        break
        return data

    def _list_windows(self) -> list[dict[str, Any]]:
        windows: list[dict[str, Any]] = []
        if not command_exists("wmctrl"):
            return windows
        r = run_cmd(["wmctrl", "-lx"], capture=True)
        if r.returncode != 0:
            return windows
        active_id = ""
        if command_exists("xdotool"):
            r_active = run_cmd(["xdotool", "getactivewindow"], capture=True)
            if r_active.returncode == 0:
                active_id = r_active.stdout.strip().lower()
        for line in r.stdout.splitlines():
            parts = line.split(None, 4)
            if len(parts) < 5:
                continue
            wid, workspace, wm_class, host, title = parts
            pid = ""
            if command_exists("xdotool"):
                r_pid = run_cmd(["xdotool", "getwindowpid", wid], capture=True)
                if r_pid.returncode == 0:
                    pid = r_pid.stdout.strip()
            windows.append(
                {
                    "window_id": wid,
                    "pid": pid,
                    "workspace": workspace,
                    "class": wm_class,
                    "title": title,
                    "is_active": wid.lower() == active_id,
                }
            )
        return windows

    def _normalize_ha_script_name(self, raw_name: str) -> str:
        norm = raw_name.lower().replace("-", " ").replace("_", " ")
        norm = " ".join(norm.split())

        # if already an exact normalized HA script id, keep it
        if raw_name in EXACT_HA_SCRIPT_NAMES:
            return raw_name

        alias_map = {
            # Lights
            "lights on": "jarvis_lights_power_on",
            "turn on lights": "jarvis_lights_power_on",
            "turn on my lights": "jarvis_lights_power_on",
            "lights off": "jarvis_lights_power_off",
            "turn off lights": "jarvis_lights_power_off",
            "turn off my lights": "jarvis_lights_power_off",
            "dim lights": "jarvis_lights_brightness_down",
            "brighten lights": "jarvis_lights_brightness_up",
            "default lights": "jarvis_lights_scene_default",
            "natural lights": "jarvis_lights_scene_natural_75",
            "red lights": "jarvis_lights_scene_red",
            "blue lights": "jarvis_lights_scene_blue",
            "green lights": "jarvis_lights_scene_green",
            "purple lights": "jarvis_lights_scene_purple",
            "movie mode": "jarvis_lights_scene_movie",
            "work mode": "jarvis_lights_scene_work",
            "night mode": "jarvis_lights_scene_night",
            "disco mode": "jarvis_lights_scene_disco",
            # Xbox
            "turn on xbox": "jarvis_xbox_power_on",
            "turn off xbox": "jarvis_xbox_power_off",
            "youtube on xbox": "jarvis_xbox_app_youtube",
            "open youtube on xbox": "jarvis_xbox_app_youtube",
            "netflix on xbox": "jarvis_xbox_app_netflix",
            "open netflix on xbox": "jarvis_xbox_app_netflix",
            "spotify on xbox": "jarvis_xbox_app_spotify",
            "open spotify on xbox": "jarvis_xbox_app_spotify",
            "pause xbox": "jarvis_xbox_media_pause",
            "resume xbox": "jarvis_xbox_media_resume",
            # Routines
            "watch youtube": "jarvis_routine_watch_youtube",
            "watch netflix": "jarvis_routine_watch_netflix",
            "play spotify": "jarvis_routine_play_spotify",
            "good night": "jarvis_routine_good_night",
        }

        chosen = alias_map.get(norm)
        if chosen:
            return chosen

        # allow normalized exact matches by comparing space-normalized ids
        for exact_name in EXACT_HA_SCRIPT_NAMES:
            exact_norm = exact_name.lower().replace("_", " ").replace("-", " ")
            exact_norm = " ".join(exact_norm.split())
            if exact_norm == norm:
                return exact_name

        return raw_name.strip().lower().replace(" ", "_")

    def _match_ha_script_from_text(self, text: str) -> str:
        q = " ".join(
            str(text or "").strip().lower().replace("_", " ").replace("-", " ").split()
        )
        if not q:
            return ""

        phrase_order = [
            (
                [
                    "turn the lights on",
                    "turn my lights on",
                    "turn on the lights",
                    "turn on my lights",
                    "lights on",
                ],
                "jarvis_lights_power_on",
            ),
            (
                [
                    "turn the lights off",
                    "turn my lights off",
                    "turn off the lights",
                    "turn off my lights",
                    "lights off",
                ],
                "jarvis_lights_power_off",
            ),
            (
                ["dim the lights", "dim my lights", "dim lights", "lights dim"],
                "jarvis_lights_brightness_down",
            ),
            (
                [
                    "brighten the lights",
                    "brighten my lights",
                    "brighten lights",
                    "lights brighten",
                ],
                "jarvis_lights_brightness_up",
            ),
            (
                ["default lights", "lights default", "reset lights"],
                "jarvis_lights_scene_default",
            ),
            (
                ["natural lights", "lights natural", "natural 75"],
                "jarvis_lights_scene_natural_75",
            ),
            (
                ["red lights", "lights red", "turn lights red", "make the lights red"],
                "jarvis_lights_scene_red",
            ),
            (
                [
                    "blue lights",
                    "lights blue",
                    "turn lights blue",
                    "make the lights blue",
                ],
                "jarvis_lights_scene_blue",
            ),
            (
                [
                    "green lights",
                    "lights green",
                    "turn lights green",
                    "make the lights green",
                ],
                "jarvis_lights_scene_green",
            ),
            (
                [
                    "purple lights",
                    "lights purple",
                    "turn lights purple",
                    "make the lights purple",
                ],
                "jarvis_lights_scene_purple",
            ),
            (
                ["movie mode", "set movie mode", "movie lights", "watch a movie"],
                "jarvis_lights_scene_movie",
            ),
            (
                [
                    "work mode",
                    "set work mode",
                    "work lights",
                    "lets get to work lights",
                ],
                "jarvis_lights_scene_work",
            ),
            (
                ["night mode", "set night mode", "good night lights"],
                "jarvis_lights_scene_night",
            ),
            (
                ["disco mode", "turn on disco mode", "Activate disco mode"],
                "jarvis_lights_scene_party",
            ),
            (
                ["xbox on", "turn on xbox", "power on xbox"],
                "jarvis_xbox_power_on",
            ),
            (
                ["xbox off", "turn off xbox", "power off xbox"],
                "jarvis_xbox_power_off",
            ),
            (
                ["open youtube on xbox", "youtube on xbox"],
                "jarvis_xbox_app_youtube",
            ),
            (
                ["open netflix on xbox", "netflix on xbox"],
                "jarvis_xbox_app_netflix",
            ),
            (
                ["open spotify on xbox", "spotify on xbox", "play spotify on xbox"],
                "jarvis_xbox_app_spotify",
            ),
            (
                ["watch youtube", "watch youtube on xbox"],
                "jarvis_routine_watch_youtube",
            ),
            (
                ["watch netflix", "watch netflix on xbox"],
                "jarvis_routine_watch_netflix",
            ),
            (
                ["play spotify"],
                "jarvis_routine_play_spotify",
            ),
            (
                ["pause the xbox", "pause xbox", "xbox pause"],
                "jarvis_xbox_media_pause",
            ),
            (
                ["resume the xbox", "resume xbox", "xbox resume", "play the xbox"],
                "jarvis_xbox_media_resume",
            ),
            (
                ["xbox volume up", "volume up on xbox", "turn xbox volume up"],
                "jarvis_xbox_volume_up",
            ),
            (
                ["xbox volume down", "volume down on xbox", "turn xbox volume down"],
                "jarvis_xbox_volume_down",
            ),
            (
                ["good night"],
                "jarvis_routine_good_night",
            ),
        ]

        for phrases, script_name in phrase_order:
            if any(p in q for p in phrases):
                return script_name

        # fallback token logic
        if "light" in q or "lights" in q:
            if "off" in q:
                return "jarvis_lights_power_off"
            if "on" in q:
                return "jarvis_lights_power_on"
            if "dim" in q:
                return "jarvis_lights_brightness_down"
            if "bright" in q:
                return "jarvis_lights_brightness_up"
            if "red" in q:
                return "jarvis_lights_scene_red"
            if "blue" in q:
                return "jarvis_lights_scene_blue"
            if "green" in q:
                return "jarvis_lights_scene_green"
            if "purple" in q:
                return "jarvis_lights_scene_purple"
            if "movie" in q:
                return "jarvis_lights_scene_movie"
            if "work" in q:
                return "jarvis_lights_scene_work"
            if "night" in q:
                return "jarvis_lights_scene_night"
            if "disco" in q:
                return "jarvis_lights_scene_disco"

        if "xbox" in q:
            if "volume up" in q:
                return "jarvis_xbox_volume_up"
            if "volume down" in q:
                return "jarvis_xbox_volume_down"
            if "pause" in q:
                return "jarvis_xbox_media_pause"
            if "resume" in q or "play" in q:
                return "jarvis_xbox_media_resume"
            if "off" in q:
                return "jarvis_xbox_power_off"
            if "on" in q:
                return "jarvis_xbox_power_on"
            if "youtube" in q:
                return "jarvis_xbox_app_youtube"
            if "netflix" in q:
                return "jarvis_xbox_app_netflix"
            if "spotify" in q:
                return "jarvis_xbox_app_spotify"

        return ""

    def _extract_response_text(self, payload: dict[str, Any]) -> str:
        if isinstance(payload.get("output_text"), str) and payload.get("output_text"):
            return str(payload["output_text"]).strip()
        parts: list[str] = []
        for item in payload.get("output", []) or []:
            if item.get("type") != "message":
                continue
            for content in item.get("content", []) or []:
                txt = content.get("text") or content.get("output_text")
                if txt:
                    parts.append(str(txt))
        return "\n".join(parts).strip()

    def _openai_responses(
        self, body: dict[str, Any]
    ) -> tuple[bool, dict[str, Any] | str]:
        api_key = str(
            CONFIG.get("openai_api_key", "") or os.getenv("OPENAI_API_KEY", "")
        ).strip()
        if not api_key:
            return False, "OPENAI_API_KEY is not set."
        try:
            response = requests.post(
                "https://api.openai.com/v1/responses",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=45,
            )
            if response.status_code >= 400:
                return (
                    False,
                    f"OpenAI Responses error {response.status_code}: {response.text[:400]}",
                )
            return True, response.json()
        except Exception as e:
            return False, str(e)

    def _image_to_data_url(self, path: str) -> str:
        raw = Path(path).read_bytes()
        mime = "image/png"
        suffix = Path(path).suffix.lower()
        if suffix in {".jpg", ".jpeg"}:
            mime = "image/jpeg"
        elif suffix == ".webp":
            mime = "image/webp"
        return f"data:{mime};base64," + base64.b64encode(raw).decode("utf-8")

    def _summarize_screenshot_with_vision(
        self, image_path: str, prompt: str
    ) -> ToolResult:
        body = {
            "model": CONFIG.get("vision_model", "gpt-4.1-mini"),
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {
                            "type": "input_image",
                            "image_url": self._image_to_data_url(image_path),
                            "detail": "high",
                        },
                    ],
                }
            ],
        }
        ok, payload = self._openai_responses(body)
        if not ok:
            return ToolResult(False, str(payload))
        summary = self._extract_response_text(
            payload if isinstance(payload, dict) else {}
        )
        if not summary:
            return ToolResult(False, "Vision analysis returned no summary.")
        return ToolResult(
            True, "Summarized current screen.", {"summary": summary, "path": image_path}
        )

    def _web_search_summary(self, query: str) -> ToolResult:
        body = {
            "model": CONFIG.get("responses_model", "gpt-5.4-mini"),
            "tools": [{"type": "web_search"}],
            "input": f"Search the web for: {query}. Give a concise factual summary. If the query is local or time-sensitive, say so clearly and summarize the best available results.",
            "include": ["web_search_call.action.sources"],
        }
        ok, payload = self._openai_responses(body)
        if not ok:
            return ToolResult(False, str(payload))
        data = payload if isinstance(payload, dict) else {}
        summary = self._extract_response_text(data)
        sources = []
        for item in data.get("output", []) or []:
            if item.get("type") == "web_search_call":
                action = item.get("action") or {}
                for src in action.get("sources", []) or []:
                    url = src.get("url") or src.get("site") or src.get("title")
                    if url:
                        sources.append(url)
        webbrowser.open(f"https://www.google.com/search?q={query.replace(' ', '+')}")
        return ToolResult(
            True,
            f"Searched the web for {query}.",
            {"summary": summary, "sources": sources[:8], "query": query},
        )

    def _resolve_code_file_from_window(
        self, active: dict[str, Any], project_path: str
    ) -> str:
        title = str(active.get("title", "")).strip()
        if not title or not project_path or not Path(project_path).exists():
            return ""
        head = re.split(r"\s[-—]\s", title)[0].strip()
        if not head:
            return ""
        root = Path(project_path)
        exact = list(root.rglob(head))
        if exact:
            return str(exact[0])
        exact2 = list(root.rglob(head + ".*"))
        if exact2:
            return str(exact2[0])
        return ""

    def _execute_many(self, actions: list[dict[str, Any]]) -> ToolResult:
        results: list[ToolResult] = []
        for item in actions:
            single = dict(item)
            single.pop("actions", None)
            results.append(self._execute_one(single))
        ok = all(r.ok for r in results)
        message = " | ".join(r.message for r in results)
        if ok:
            project_path = next(
                (
                    str(a.get("project_path", "")).strip()
                    for a in actions
                    if str(a.get("project_path", "")).strip()
                ),
                "",
            )
            apps = [
                str(a.get("app", "")).strip().lower()
                for a in actions
                if str(a.get("app", "")).strip()
            ]
            url_keys: list[str] = []
            urls: list[str] = []
            for a in actions:
                if str(a.get("url_key", "")).strip():
                    url_keys.append(str(a["url_key"]).strip().lower())
                for key in a.get("url_keys", []):
                    if str(key).strip():
                        url_keys.append(str(key).strip().lower())
                if str(a.get("url", "")).strip():
                    urls.append(str(a["url"]).strip())
            url_keys = self.memory._unique_list(url_keys)
            urls = self.memory._unique_list(urls)
            meaningful_apps = [
                a for a in apps if a not in {"chrome", "google chrome", "browser"}
            ]
            meaningful_urls = [k for k in url_keys if k not in {"chatgpt", "google"}]
            if project_path or meaningful_apps or meaningful_urls or len(urls) >= 2:
                name = (
                    Path(project_path).name
                    if project_path
                    else f"Session {time.strftime('%Y-%m-%d %H:%M')}"
                )
                ctx = self.memory.remember_context(
                    name=name,
                    project_path=project_path,
                    apps=apps,
                    url_keys=url_keys,
                    urls=urls,
                    notes="Auto-saved successful workspace",
                    source="auto",
                )
                self.working.write(
                    {
                        "active_workspace": ctx["name"],
                        "active_context_name": ctx["name"],
                    }
                )
        return ToolResult(ok, message, {"results": [r.__dict__ for r in results]})

    def _format_result_for_chat(self, action: str, result: ToolResult) -> str:
        """Format a tool result as readable text for the chat tab (not spoken form)."""
        data = result.data or {}
        if not result.ok:
            return f"Error: {result.message}"

        if action == "git_status":
            status = str(data.get("status", "")).strip()
            if data.get("clean"):
                return "Git: clean — no uncommitted changes."
            lines = [l for l in status.splitlines() if l.strip()]
            formatted = "\n".join(f"  {l}" for l in lines[:25])
            suffix = f"\n  ...({len(lines) - 25} more)" if len(lines) > 25 else ""
            return f"Git status — {len(lines)} changed file(s):\n{formatted}{suffix}"

        if action == "git_diff":
            diff = str(data.get("diff", "")).strip()
            if not diff:
                return "Git diff: no staged or unstaged changes."
            lines = diff.splitlines()
            preview = "\n".join(lines[:30])
            suffix = f"\n...({len(lines) - 30} more lines)" if len(lines) > 30 else ""
            return f"Git diff:\n```\n{preview}{suffix}\n```"

        if action == "run_diagnostics":
            spoken = str(data.get("spoken_summary", result.message))
            rows = [spoken, ""]
            for key, label in [
                ("voice", "Voice"), ("ollama", "Ollama"), ("claude_code", "Claude Code"),
                ("vault", "Vault"), ("background_workers", "Workers"), ("git", "Git"),
                ("cost", "Cost"), ("system", "System"), ("watchdog", "Watchdog"),
                ("proactive_loop", "Proactive"),
            ]:
                d = data.get(key)
                if not isinstance(d, dict):
                    continue
                if key == "ollama":
                    val = f"{'✓' if d.get('available') else '✗'}  {d.get('latency_ms', '?')}ms  models: {', '.join(d.get('models', []))}"
                elif key == "vault":
                    val = f"{'✓' if d.get('db_exists') else '✗'}  {d.get('chunk_count', 0)} chunks  indexed: {str(d.get('last_indexed', '?'))[:10]}"
                elif key == "cost":
                    val = f"session ${d.get('session_usd', 0):.4f}  daily ${d.get('daily_usd', 0):.4f}  ({d.get('pct_used', 0):.0f}% of limit)"
                elif key == "system":
                    val = f"CPU {d.get('cpu_pct', 0):.0f}%  RAM {d.get('ram_pct', 0):.0f}%  Disk {d.get('disk_pct', 0):.0f}%"
                elif key == "background_workers":
                    val = f"active: {d.get('active_tasks', 0)}  stuck: {d.get('stuck_tasks', 0)}"
                elif key == "git":
                    val = f"checkpoint: {d.get('last_checkpoint', '?')}  uncommitted: {d.get('uncommitted_changes', 0)}"
                elif key == "watchdog":
                    val = d.get("status", "pending")
                elif key == "proactive_loop":
                    val = f"{d.get('cycles_this_session', 0)} cycles this session"
                elif key == "voice":
                    val = "connected" if d.get("connected", True) else "disconnected"
                elif key == "claude_code":
                    val = "on PATH" if d.get("on_path") else "not found"
                else:
                    val = str(d)[:60]
                rows.append(f"  {label}: {val}")
            return "\n".join(rows)

        if action == "search_codebase":
            count = data.get("count", 0)
            output = str(data.get("output", "")).strip()
            if count == 0:
                return "No matches found."
            return f"Found {count} match(es):\n{output[:1500]}"

        if action == "query_vault":
            results = data.get("results", [])
            if not results:
                return "No vault entries found."
            lines = [f"Found {len(results)} vault result(s):"]
            for r in results[:5]:
                title = str(r.get("title", ""))[:60]
                snippet = str(r.get("text", ""))[:120].replace("\n", " ")
                lines.append(f"\n  [{title}]\n  {snippet}...")
            return "\n".join(lines)

        if action in ("run_python", "run_shell"):
            output = str(data.get("output", "")).strip()
            return f"```\n{output[:1500]}\n```" if output else result.message

        if action == "system_status":
            import json as _json
            return f"System status:\n{_json.dumps(data, indent=2)[:1000]}"

        if action == "get_priorities":
            priorities = data.get("priorities", [])
            if not priorities:
                return "No priorities found in vault."
            lines = ["Priorities:"]
            for i, p in enumerate(priorities[:5], 1):
                lines.append(f"  {i}. {str(p)[:120]}")
            return "\n".join(lines)

        # Generic: prefer message, trim raw data
        return result.message

    def execute(self, payload: dict[str, Any], chat_format: bool = False) -> ToolResult:
        log_event("tool_execute", {"payload": payload})
        self._remember_last_request(payload)
        action_name = str(payload.get("action", "")).strip()
        self._episode(
            "tool_request",
            f"Requested tool action: {action_name or 'multi_action'}",
            tags=["tool"],
            data=payload,
        )
        actions = payload.get("actions")
        if isinstance(actions, list) and actions:
            result = self._execute_many(actions)
            r = self._remember_tool_result(payload, result)
            if chat_format:
                return ToolResult(r.ok, self._format_result_for_chat("multi_action", r), r.data)
            return r
        action = str(payload.get("action", "")).strip()
        if not action:
            return self._remember_tool_result(
                payload,
                ToolResult(False, "No action was provided."),
            )
        result = self._execute_one(payload)
        r = self._remember_tool_result(payload, result)
        if chat_format:
            return ToolResult(r.ok, self._format_result_for_chat(action, r), r.data)
        return r

    def _execute_one(self, payload: dict[str, Any]) -> ToolResult:
        action = str(payload.get("action", "")).strip()
        try:
            return self._execute_one_inner(payload)
        except Exception as exc:
            import traceback
            tb_last = traceback.format_exc().strip().splitlines()[-1]
            log_event("tool_error", {
                "action": action,
                "error": str(exc)[:200],
                "traceback_last": tb_last,
            })
            try:
                self.working.write({
                    "last_tool_error": {
                        "action": action,
                        "error": str(exc)[:200],
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    }
                })
            except Exception:
                pass
            notify_voice_error(action, str(exc))
            return ToolResult(False, f"Tool error: {str(exc)[:120]}")

    def _execute_calendar_read(self, action: str, payload: dict[str, Any]) -> ToolResult:
        """Dispatch read-only calendar actions. No mutation, no writes."""
        try:
            from prometheus.agents.calendar_read_tools import (
                calendar_list_upcoming,
                calendar_get_today,
                calendar_get_tomorrow,
                calendar_get_date,
                calendar_next_event,
                calendar_summarize_day,
                calendar_find_free_blocks,
            )
        except ImportError as exc:
            return ToolResult(False, f"Calendar read tools not available: {exc}")

        try:
            if action == "calendar_get_today":
                result = calendar_get_today()
            elif action == "calendar_get_tomorrow":
                result = calendar_get_tomorrow()
            elif action == "calendar_next_event":
                result = calendar_next_event()
            elif action == "calendar_list_upcoming":
                max_results = int(payload.get("max_results") or 10)
                days = int(payload.get("days") or 14)
                result = calendar_list_upcoming(max_results=max_results, days=days)
            elif action == "calendar_get_date":
                date_str = str(payload.get("date", "")).strip()
                if not date_str:
                    return ToolResult(False, "calendar_get_date requires a 'date' parameter (YYYY-MM-DD).")
                result = calendar_get_date(date_str)
            elif action == "calendar_summarize_day":
                date_str = str(payload.get("date", "")).strip() or None
                result = calendar_summarize_day(date_str)
            elif action == "calendar_find_free_blocks":
                date_str = str(payload.get("date", "")).strip()
                if not date_str:
                    return ToolResult(False, "calendar_find_free_blocks requires a 'date' parameter (YYYY-MM-DD).")
                min_min = int(payload.get("minimum_minutes") or 60)
                result = calendar_find_free_blocks(date_str, minimum_minutes=min_min)
            else:
                return ToolResult(False, f"Unknown calendar action: {action}")
        except Exception as exc:
            log_event("calendar_read_error", {"action": action, "error": str(exc)[:200]})
            return ToolResult(False, f"Calendar read error: {str(exc)[:120]}")

        ok = bool(result.get("ok", False))
        if not ok:
            return ToolResult(False, result.get("error", "Calendar read failed."), result)

        summary = result.get("summary") or result.get("error") or f"{action} completed."
        if action == "calendar_get_today":
            summary = f"{result.get('count', 0)} event(s) today ({result.get('date', '')})."
        elif action == "calendar_get_tomorrow":
            summary = f"{result.get('count', 0)} event(s) tomorrow ({result.get('date', '')})."
        elif action == "calendar_list_upcoming":
            summary = f"{result.get('count', 0)} upcoming event(s) over the next {result.get('days', 14)} days."
        elif action == "calendar_next_event":
            nxt = result.get("next_timed_event")
            if nxt:
                summary = f"Next event: {nxt.get('title', '')} at {str(nxt.get('start_time', ''))[:16]}."
            else:
                summary = "No upcoming timed events found."
        elif action == "calendar_find_free_blocks":
            n = result.get("free_block_count", 0)
            summary = f"{n} free block(s) of {result.get('minimum_minutes', 60)}+ min on {result.get('date', '')}."

        log_event("calendar_read_tool", {"action": action, "ok": ok, "summary": summary[:120]})
        self.working.write({"last_tool_action": action})
        return ToolResult(True, summary, result)

    def _execute_one_inner(self, payload: dict[str, Any]) -> ToolResult:
        action = str(payload.get("action", "")).strip()
        if action == "confirm_pending":
            return self._resolve_pending(True)
        if action == "cancel_pending":
            return self._resolve_pending(False)
        if action in {"sleep", "restart", "shutdown"}:
            return self._set_pending(payload)
        if action == "run_ha_script":
            raw_name = str(payload.get("script_name", "")).strip()
            if not raw_name:
                return ToolResult(False, "A script name is required.")
            chosen = self._normalize_ha_script_name(raw_name)
            result = run_ha_script(chosen)
            if result.ok:
                self._episode(
                    "tool_action",
                    f"Ran Home Assistant script: {chosen}",
                    tags=["home_assistant", "script"],
                    data={"action": action, "script_name": chosen},
                )
                self.working.write(
                    {
                        "last_tool_action": "run_ha_script",
                        "active_media_flow": (
                            chosen if "movie" in chosen or "netflix" in chosen else ""
                        ),
                        "current_mode": "movie" if "movie" in chosen else "",
                    }
                )
            return result
        if action == "smart_action":
            request_text = str(
                payload.get("request_text", "") or payload.get("query", "")
            ).strip()
            lowered = " ".join(request_text.lower().split())
            script = self._match_ha_script_from_text(lowered)
            if script:
                return self._execute_one(
                    {
                        "action": "run_ha_script",
                        "script_name": script,
                        "request_text": request_text,
                    }
                )
            if any(
                x in lowered
                for x in [
                    "what am i looking at",
                    "what's on my screen",
                    "summarize the tab",
                    "summarize the screen",
                    "what is on my screen",
                    "current tab",
                    "screen right now",
                ]
            ):
                return self._execute_one(
                    {"action": "summarize_screen", "request_text": request_text}
                )
            if any(
                x in lowered
                for x in [
                    "movies playing near me",
                    "search for",
                    "look up",
                    "search the web",
                    "latest ",
                    "today",
                    "near me",
                ]
            ):
                return self._execute_one(
                    {
                        "action": "web_search",
                        "query": request_text,
                        "request_text": request_text,
                    }
                )
            if any(
                x in lowered
                for x in [
                    "let's get to work",
                    "lets get to work",
                    "get to work",
                    "open my workspace",
                    "switch to",
                    "work on ",
                ]
            ):
                return self._execute_one(
                    {
                        "action": "resume_last_context",
                        "query": request_text,
                        "request_text": request_text,
                    }
                )
            return ToolResult(False, "No deterministic smart action matched.")
        if action == "summarize_screen":
            ok, shot = self._capture_screenshot()
            if not ok:
                return ToolResult(False, shot)
            desktop = {
                "active_window": self._get_active_window(),
                "windows": self._list_windows(),
            }
            active = desktop.get("active_window", {}) or {}
            request_text = str(
                payload.get("request_text", "")
                or "Summarize what is visible on this screen."
            ).strip()
            project_path = self._resolve_project_path(
                self.behavior.resolve_active_project(
                    desktop, request_text=request_text
                ).get("project_path", "")
            )
            code_file = self._resolve_code_file_from_window(active, project_path)
            code_snippet = ""
            if code_file and Path(code_file).exists():
                try:
                    code_snippet = Path(code_file).read_text(
                        encoding="utf-8", errors="ignore"
                    )[:5000]
                except Exception:
                    code_snippet = ""
            prompt = (
                f"Analyze this screenshot and describe exactly what is visible. "
                f"Identify the application, summarize the visible content, and explain what the user is likely looking at. "
                f"Do not guess if the screenshot is unclear; say what you can actually see. "
                f"Active window metadata: {json.dumps(active)}. "
            )
            if code_snippet:
                prompt += f"Likely active file path: {code_file}. File snippet for grounding: {code_snippet}"
            result = self._summarize_screenshot_with_vision(shot, prompt)
            if result.ok:
                data = result.data or {}
                data.update({"desktop_state": desktop, "code_file": code_file})
                result.data = data
            return result
        if action == "backfill_memory":
            count = self.memory.backfill_from_logs()
            self._episode(
                "tool_action",
                f"Backfilled memory from logs: {count}",
                tags=["memory"],
                data={"action": action, "count": count},
            )
            return ToolResult(
                True,
                f"Imported {count} historical contexts from logs.",
                {"count": count},
            )
        if action == "run_dream_pass":
            result = self.dream.run_once()
            self._episode(
                "tool_action",
                "Ran dream pass.",
                tags=["memory", "dream"],
                data={"action": action, **result},
            )
            return ToolResult(True, "Dream pass completed.", result)
        if action == "list_windows":
            windows = self._list_windows()
            self._episode(
                "tool_action",
                f"Listed {len(windows)} desktop windows.",
                tags=["desktop", "awareness"],
                data={"action": action, "count": len(windows)},
            )
            return ToolResult(
                True, f"Found {len(windows)} open windows.", {"windows": windows}
            )
        if action == "get_active_window":
            active = self._get_active_window()
            self.working.write(
                {"screen_focus": active, "last_tool_action": "get_active_window"}
            )
            self._episode(
                "tool_action",
                f"Checked active window: {active.get('title') or 'Unknown'}",
                tags=["desktop", "awareness"],
                data={"action": action, "active_window": active},
            )
            return ToolResult(
                True,
                f"Active window: {active.get('title') or 'Unknown'}",
                {"active_window": active},
            )
        if action == "desktop_state":
            windows = self._list_windows()
            active = self._get_active_window()
            data = {
                "active_window": active,
                "windows": windows,
            }
            if bool(payload.get("include_screenshot", False)):
                ok, shot = self._capture_screenshot()
                if ok:
                    data["screenshot_path"] = shot
                else:
                    data["screenshot_error"] = shot
            self.working.write(
                {"screen_focus": active, "last_tool_action": "desktop_state"}
            )
            self._episode(
                "tool_action",
                "Collected desktop state.",
                tags=["desktop", "awareness"],
                data={
                    "action": action,
                    "active_window": active,
                    "window_count": len(windows),
                },
            )
            return ToolResult(True, "Collected desktop state.", data)
        if action == "screen_context":
            # Read workspace state from visual_state.json — no screenshot needed
            try:
                vs = json.loads(VISUAL_STATE_PATH.read_text(encoding="utf-8"))
            except Exception:
                vs = {}
            active_project = str(vs.get("active_project_name") or "unknown")
            active_window = vs.get("active_window") or {}
            win_title = str(active_window.get("title") or "unknown") if isinstance(active_window, dict) else "unknown"
            xbox_state = vs.get("xbox_state")
            xbox_app = str(vs.get("xbox_app") or "")
            xbox_media = str(vs.get("xbox_media_title") or "")
            summary_parts = [
                f"Active project: {active_project}",
                f"Active window: {win_title}",
            ]
            if xbox_state:
                summary_parts.append(f"Xbox: {xbox_state}" + (f" — {xbox_app}" if xbox_app else ""))
            data = {
                "active_project": active_project,
                "active_window": active_window,
                "xbox_state": xbox_state,
                "xbox_app": xbox_app,
                "xbox_media_title": xbox_media,
                "summary": " | ".join(summary_parts),
            }
            log_event("screen_context_read", {"project": active_project, "window": win_title[:60]})
            return ToolResult(True, " | ".join(summary_parts), data)
        if action == "open_app":
            app_key = str(payload.get("app", "")).strip().lower()

            # Check if app is already running — require BOTH pgrep AND wmctrl confirmation
            # to avoid false-positive "already open" responses.
            proc_name = _APP_PROCESS_NAMES.get(app_key)
            pgrep_running = False
            wmctrl_found = False
            if proc_name:
                try:
                    pg = subprocess.run(
                        ["pgrep", "-i", proc_name],
                        capture_output=True, timeout=2.0,
                    )
                    pgrep_running = pg.returncode == 0
                except Exception as exc:
                    log_event("open_app_pgrep_error", {"app": app_key, "error": str(exc)})
                if pgrep_running and command_exists("wmctrl"):
                    try:
                        wm_out = subprocess.run(
                            ["wmctrl", "-l"],
                            capture_output=True, text=True, timeout=2.0,
                        )
                        wmctrl_found = any(
                            proc_name.lower() in line.lower() or app_key.lower() in line.lower()
                            for line in wm_out.stdout.splitlines()
                        )
                    except Exception:
                        pass

            if pgrep_running and wmctrl_found:
                # Both sources confirm app is running — bring it to focus
                log_event("open_app_already_running", {"app": app_key, "proc": proc_name})
                if command_exists("wmctrl"):
                    subprocess.Popen(
                        ["wmctrl", "-a", proc_name],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                self.working.write({"last_tool_action": "open_app"})
                return ToolResult(True, f"{app_key.capitalize()} is already open.")
            # Otherwise launch fresh — pgrep-only match or neither confirmed

            # Launch with config → builtin fallback chain
            result = self._launch_with_fallback(app_key)
            if result.ok:
                self._episode(
                    "tool_action",
                    f"Opened app: {app_key}",
                    tags=["desktop", "app"],
                    data={"action": action, "app": app_key},
                )
                self.working.write({"last_tool_action": "open_app"})
            else:
                log_event("open_app_failed", {"app": app_key, "message": result.message})
            return result
        if action == "close_app":
            app_key = str(payload.get("app", "")).strip().lower()
            cmd = CONFIG.get("apps", {}).get(app_key)
            if not cmd:
                return ToolResult(False, f"Unknown app: {app_key}")
            process_name = shlex.split(cmd)[0]
            kill_existing(process_name)
            self._episode(
                "tool_action",
                f"Closed app: {app_key}",
                tags=["desktop", "app"],
                data={"action": action, "app": app_key},
            )
            self.working.write({"last_tool_action": "close_app"})
            return ToolResult(True, f"Closed {app_key}.")
        if action == "open_url_key":
            url_key = str(payload.get("url_key", "")).strip().lower()
            url = CONFIG.get("urls", {}).get(url_key)
            if not url:
                return ToolResult(False, f"Unknown URL key: {url_key}")
            webbrowser.open(url)
            self._episode(
                "tool_action",
                f"Opened URL key: {url_key}",
                tags=["browser", "url"],
                data={"action": action, "url_key": url_key},
            )
            self.working.write({"last_tool_action": "open_url_key"})
            return ToolResult(True, f"Opened {url_key}.")
        if action == "open_url_keys":
            keys = [
                str(x).strip().lower()
                for x in payload.get("url_keys", [])
                if str(x).strip()
            ]
            if not keys:
                return ToolResult(False, "No URL keys were provided.")
            results: list[str] = []
            all_ok = True
            for key in keys:
                url = CONFIG.get("urls", {}).get(key)
                if not url:
                    all_ok = False
                    results.append(f"Unknown URL key: {key}")
                    continue
                webbrowser.open(url)
                results.append(f"Opened {key}.")
            if all_ok:
                self._episode(
                    "tool_action",
                    f"Opened URL keys: {', '.join(keys)}",
                    tags=["browser", "url"],
                    data={"action": action, "url_keys": keys},
                )
                self.working.write({"last_tool_action": "open_url_keys"})
            return ToolResult(all_ok, " | ".join(results), {"url_keys": keys})
        if action == "open_url_raw":
            url = str(payload.get("url", "")).strip()
            if not url:
                return ToolResult(False, "No URL provided.")
            webbrowser.open(url)
            self._episode(
                "tool_action",
                f"Opened raw URL: {url}",
                tags=["browser", "url"],
                data={"action": action, "url": url},
            )
            self.working.write({"last_tool_action": "open_url_raw"})
            return ToolResult(True, f"Opened {url}.")
        if action == "web_search":
            query = str(
                payload.get("query", "") or payload.get("request_text", "")
            ).strip()
            if not query:
                return ToolResult(False, "No search query provided.")
            self._episode(
                "tool_action",
                f"Web searched: {query}",
                tags=["browser", "search"],
                data={"action": action, "query": query},
            )
            self.working.write({"last_tool_action": "web_search"})
            return self._web_search_summary(query)
        if action == "open_code_folder":
            project_path = self._resolve_project_path(
                str(payload.get("project_path", "")).strip()
            )
            if not project_path:
                return ToolResult(False, "No project path was provided.")
            if not Path(project_path).exists():
                return ToolResult(False, f"Project path not found: {project_path}")
            code_cmd = CONFIG.get("apps", {}).get("code") or CONFIG.get("apps", {}).get(
                "vscode"
            )
            if not code_cmd:
                return ToolResult(False, "VS Code command is not configured.")
            parts = shlex.split(code_cmd) + [project_path]
            subprocess.Popen(
                parts, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            self._episode(
                "tool_action",
                f"Opened code folder: {project_path}",
                tags=["code", "project"],
                data={"action": action, "project_path": project_path},
            )
            self.working.write(
                {
                    "last_tool_action": "open_code_folder",
                    "active_workspace": Path(project_path).name,
                    "active_context_name": Path(project_path).name,
                }
            )
            return ToolResult(
                True,
                f"Opened code folder: {project_path}.",
                {"project_path": project_path},
            )
        if action == "open_terminal_here":
            project_path = self._resolve_project_path(
                str(payload.get("project_path", "")).strip()
            )
            if not project_path:
                return ToolResult(False, "No project path was provided.")
            if not Path(project_path).exists():
                return ToolResult(False, f"Project path not found: {project_path}")
            terminal_cmd = str(CONFIG.get("apps", {}).get("terminal", "")).strip()
            if not terminal_cmd:
                return ToolResult(False, "Terminal command is not configured.")
            terminal_bin = shlex.split(terminal_cmd)[0]
            if terminal_bin == "konsole":
                subprocess.Popen(
                    ["konsole", "--workdir", project_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            elif terminal_bin == "gnome-terminal":
                subprocess.Popen(
                    ["gnome-terminal", "--working-directory", project_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                subprocess.Popen(
                    shlex.split(terminal_cmd),
                    cwd=project_path,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            self._episode(
                "tool_action",
                f"Opened terminal in: {project_path}",
                tags=["terminal", "project"],
                data={"action": action, "project_path": project_path},
            )
            self.working.write({"last_tool_action": "open_terminal_here"})
            return ToolResult(
                True,
                f"Opened terminal in: {project_path}.",
                {"project_path": project_path},
            )
        if action == "save_context":
            context_name = str(payload.get("context_name", "")).strip()
            project_path = self._resolve_project_path(
                str(payload.get("project_path", "")).strip()
            )
            apps = [
                str(x).strip().lower()
                for x in payload.get("apps", [])
                if str(x).strip()
            ]
            url_keys = [
                str(x).strip().lower()
                for x in payload.get("url_keys", [])
                if str(x).strip()
            ]
            urls = [str(x).strip() for x in payload.get("urls", []) if str(x).strip()]
            notes = str(payload.get("notes", "")).strip()
            tags = [str(x).strip() for x in payload.get("tags", []) if str(x).strip()]
            layout = str(payload.get("layout", "")).strip()
            if not context_name:
                if project_path:
                    context_name = Path(project_path).name
                elif apps:
                    context_name = " ".join(apps[:2]).strip() or "Saved Workspace"
                elif url_keys:
                    context_name = " ".join(url_keys[:2]).strip() or "Saved Workspace"
                elif notes:
                    context_name = notes[:50]
                else:
                    context_name = f"Saved Workspace {time.strftime('%Y-%m-%d %H:%M')}"
            ctx = self.memory.remember_context(
                name=context_name,
                project_path=project_path,
                apps=apps,
                url_keys=url_keys,
                urls=urls,
                notes=notes,
                tags=tags,
                layout=layout,
                source="tool",
            )
            self._episode(
                "tool_action",
                f"Saved context: {ctx['name']}",
                tags=["memory", "context"],
                data={"action": action, "context": ctx},
            )
            self.working.write(
                {
                    "last_tool_action": "save_context",
                    "active_workspace": ctx["name"],
                    "active_context_name": ctx["name"],
                }
            )
            return ToolResult(True, f"Saved context: {ctx['name']}.", {"context": ctx})
        if action == "resume_last_context":
            context_name = str(payload.get("context_name", "")).strip()
            query = str(payload.get("query", "")).strip()
            if context_name:
                ctx = self.memory.get_context(context_name)
            elif query:
                ctx = self.memory.search_best_context(query)
            else:
                ctx = self.memory.get_last_context()
            if not ctx:
                return ToolResult(False, "No saved context was found.")
            current_name = str(
                self.working.read().get("active_context_name", "")
            ).strip()
            if (
                current_name
                and current_name.lower() != str(ctx.get("name", "")).strip().lower()
            ):
                try:
                    self._execute_one({"action": "close_app", "app": "code"})
                    self._execute_one({"action": "close_app", "app": "terminal"})
                except Exception:
                    pass
            result = self._execute_many(self.memory.build_actions_from_context(ctx))
            self.memory.touch_context(str(ctx.get("name", "")))
            self._run_layout_hook(ctx)
            self._episode(
                "tool_action",
                f"Resumed context: {ctx['name']}",
                tags=["memory", "context"],
                data={"action": action, "context": ctx},
            )
            self.working.write(
                {
                    "last_tool_action": "resume_last_context",
                    "active_workspace": ctx["name"],
                    "active_context_name": ctx["name"],
                }
            )
            return ToolResult(
                result.ok,
                f"Resumed context: {ctx['name']}. {result.message}",
                {"context": ctx, "results": (result.data or {}).get("results", [])},
            )
        if action == "save_routine":
            routine_name = str(payload.get("routine_name", "")).strip()
            steps = payload.get("steps", [])
            description = str(payload.get("description", "")).strip()
            tags = [str(x).strip() for x in payload.get("tags", []) if str(x).strip()]
            if not routine_name:
                return ToolResult(False, "A routine name is required.")
            if not isinstance(steps, list) or not steps:
                return ToolResult(False, "A non-empty steps list is required.")
            routine = self.memory.save_routine(
                name=routine_name,
                steps=steps,
                description=description,
                tags=tags,
            )
            self.procedural.save_routine(
                routine_name,
                description=description,
                triggers=[routine_name],
                steps=steps,
                tags=tags,
            )
            self._episode(
                "tool_action",
                f"Saved routine: {routine['name']}",
                tags=["memory", "routine"],
                data={"action": action, "routine": routine},
            )
            self.working.write({"last_tool_action": "save_routine"})
            return ToolResult(
                True, f"Saved routine: {routine['name']}.", {"routine": routine}
            )
        if action == "run_routine":
            routine_name = str(payload.get("routine_name", "")).strip()
            if not routine_name:
                return ToolResult(False, "A routine name is required.")
            routine = self.memory.get_routine(routine_name)
            source = "memory"
            if routine is None:
                proc_routine = self.procedural.get_routine(routine_name)
                if proc_routine:
                    routine = proc_routine
                    source = "procedural"
            if routine is None:
                routines_cfg = CONFIG.get("routines", {})
                q = routine_name.strip().lower().replace("_", " ").replace("-", " ")
                q = " ".join(q.split())
                exact_key = None
                partial_key = None
                for key in routines_cfg.keys():
                    key_norm = key.strip().lower().replace("_", " ").replace("-", " ")
                    key_norm = " ".join(key_norm.split())
                    if key_norm == q:
                        exact_key = key
                        break
                    if q in key_norm and partial_key is None:
                        partial_key = key
                chosen_key = exact_key or partial_key
                if chosen_key:
                    cfg_routine = routines_cfg.get(chosen_key)
                    if isinstance(cfg_routine, dict):
                        routine = cfg_routine
                        source = "config"
                        routine_name = chosen_key
            if routine is None:
                return ToolResult(False, f"Routine not found: {routine_name}")
            steps = routine.get("steps") or routine.get("actions") or []
            if not isinstance(steps, list) or not steps:
                return ToolResult(False, f"Routine has no steps: {routine_name}")
            hosting = self.semantic.get_fact("microschool_hosting")
            if (
                routine_name == "microschool_website_changes"
                and hosting == "cloud_hosted"
            ):
                # Prefer cloud/browser assumptions; keep routine execution but fact is available for future branching
                pass
            result = self._execute_many(steps)
            project_path = self._resolve_project_path(
                str(routine.get("project_path", "")).strip()
            )
            context_name = str(routine.get("context_name", "")).strip() or routine_name
            apps = [
                str(x).strip().lower()
                for x in routine.get("apps", [])
                if str(x).strip()
            ]
            url_keys = [
                str(x).strip().lower()
                for x in routine.get("url_keys", [])
                if str(x).strip()
            ]
            urls = [str(x).strip() for x in routine.get("urls", []) if str(x).strip()]
            notes = str(routine.get("description", "")).strip()
            tags = [str(x).strip() for x in routine.get("tags", []) if str(x).strip()]
            layout = str(routine.get("layout", "")).strip()
            ctx = self.memory.remember_context(
                name=context_name,
                project_path=project_path,
                apps=apps,
                url_keys=url_keys,
                urls=urls,
                notes=notes,
                tags=tags,
                layout=layout,
                source=f"routine:{source}",
            )
            self.memory.touch_routine(routine_name)
            self._run_layout_hook(ctx)
            self._episode(
                "tool_action",
                f"Ran routine: {routine_name}",
                tags=["routine"],
                data={"action": action, "routine_name": routine_name, "source": source},
            )
            self.working.write(
                {
                    "last_tool_action": "run_routine",
                    "active_workspace": context_name,
                    "active_context_name": context_name,
                }
            )
            return ToolResult(
                result.ok,
                f"Ran routine: {routine_name}. {result.message}",
                {
                    "routine": routine_name,
                    "context": ctx,
                    "results": (result.data or {}).get("results", []),
                },
            )
        if action == "list_files":
            folder = (
                str(payload.get("project_path", "")).strip()
                or str(payload.get("path", "")).strip()
            )
            folder = self._resolve_project_path(folder)
            if not folder:
                return ToolResult(False, "No folder path was provided.")
            p = Path(folder)
            if not p.exists() or not p.is_dir():
                return ToolResult(False, f"Folder not found: {folder}")
            items = []
            for child in sorted(p.iterdir()):
                items.append(
                    {
                        "name": child.name,
                        "is_dir": child.is_dir(),
                        "path": str(child),
                    }
                )
            return ToolResult(True, f"Found {len(items)} items.", {"items": items})
        if action == "read_file":
            path = str(payload.get("path", "")).strip()
            if not path:
                return ToolResult(False, "No file path was provided.")
            p = Path(path).expanduser()
            if not p.exists() or not p.is_file():
                return ToolResult(False, f"File not found: {path}")
            text = p.read_text(encoding="utf-8", errors="ignore")
            return ToolResult(
                True, f"Read file: {p.name}", {"path": str(p), "content": text[:12000]}
            )
        if action == "write_file":
            path = str(payload.get("path", "")).strip()
            content = str(payload.get("content", ""))
            if not path:
                return ToolResult(False, "No file path was provided.")
            try:
                ensure_workspace_root()
                p = resolve_workspace_path(path)
            except (ValueError, PermissionError) as exc:
                log_event("write_file_blocked", {"path": path, "reason": str(exc)})
                return ToolResult(False, f"Write blocked: {exc}")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return ToolResult(True, f"Wrote file: {p.name}", {"path": str(p)})
        if action == "mode_lock_in":
            launched: list[str] = []
            for cmd in CONFIG.get("modes", {}).get("lock_in", []):
                subprocess.Popen(
                    shlex.split(cmd),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                launched.append(shlex.split(cmd)[0])
            self._episode(
                "tool_action",
                "Activated lock in mode.",
                tags=["mode"],
                data={"action": action, "launched": launched},
            )
            return ToolResult(
                True, f"Lock in mode activated. Launched: {', '.join(launched)}."
            )
        if action == "volume_change":
            if not command_exists("amixer"):
                return ToolResult(False, "amixer is not installed.")
            delta = int(payload.get("delta", 0))
            sign = "+" if delta >= 0 else "-"
            run_cmd(["amixer", "-D", "pulse", "sset", "Master", f"{abs(delta)}%{sign}"])
            return ToolResult(True, f"Volume adjusted by {delta}.")
        if action == "volume_set":
            if not command_exists("amixer"):
                return ToolResult(False, "amixer is not installed.")
            value = max(0, min(100, int(payload.get("value", 50))))
            run_cmd(["amixer", "-D", "pulse", "sset", "Master", f"{value}%"])
            return ToolResult(True, f"Volume set to {value} percent.")
        if action == "mute_toggle":
            if not command_exists("amixer"):
                return ToolResult(False, "amixer is not installed.")
            run_cmd(["amixer", "-D", "pulse", "sset", "Master", "toggle"])
            return ToolResult(True, "Mute toggled.")
        if action == "screenshot":
            ok, result = self._capture_screenshot()
            if not ok:
                return ToolResult(False, result)
            return ToolResult(True, f"Screenshot saved to {result}.", {"path": result})
        if action == "tell_time":
            return ToolResult(True, time.strftime("It is %I:%M %p."))
        if action == "projector_on":
            script = Path(CONFIG.get("projector_on_script", "")).expanduser()
            if not script.exists():
                return ToolResult(False, f"Projector on script not found: {script}")
            subprocess.Popen(
                [str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            return ToolResult(True, "Projector on sequence started.")
        if action == "projector_off":
            script = Path(CONFIG.get("projector_off_script", "")).expanduser()
            if not script.exists():
                return ToolResult(False, f"Projector off script not found: {script}")
            subprocess.Popen(
                [str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            return ToolResult(True, "Projector off sequence started.")
        if action == "sleep":
            run_cmd(["systemctl", "suspend"])
            return ToolResult(True, "Suspending.")
        if action == "restart":
            run_cmd(["systemctl", "reboot"])
            return ToolResult(True, "Restarting.")
        if action == "shutdown":
            run_cmd(["systemctl", "poweroff"])
            return ToolResult(True, "Shutting down.")
        if action == "background_task":
            description = str(
                payload.get("description") or payload.get("task") or payload.get("intent") or payload.get("request_text") or ""
            ).strip()
            task_context = payload.get("context") or {}
            if not description:
                log_event("background_task_no_description", {"payload_keys": list(payload.keys())})
                return ToolResult(False, "No task description provided.")
            if self.worker_pool is None:
                return ToolResult(False, "Background worker pool is not running.")
            context = {**self.working.read(), **task_context}
            self.worker_pool.submit(description, context)
            short = description[:60] + ("…" if len(description) > 60 else "")
            log_event("background_task_submitted", {"description": short})
            return ToolResult(True, f"Background task started: {short}")

        if action == "run_python":
            project_path = str(payload.get("project_path") or "").strip()
            command = str(payload.get("command") or "").strip()
            if not command:
                log_event("run_python_error", {"error": "no command"})
                return ToolResult(ok=False, message="run_python: no command provided")
            _PYTHON_BLOCKED = [
                "rm ", "rmtree", "os.remove", "shutil.rmtree",
                "sys.exit(0)", "subprocess.call", "os.system",
            ]
            if any(p in command for p in _PYTHON_BLOCKED):
                log_event("run_python_blocked", {"command": command[:120]})
                return ToolResult(ok=False, message="run_python: blocked — command contains restricted patterns")
            python_bin = "python3"
            if project_path:
                venv_python = Path(project_path) / ".venv" / "bin" / "python3"
                if venv_python.exists():
                    python_bin = str(venv_python)
            try:
                cmd_args = (
                    [python_bin, command]
                    if command.endswith(".py")
                    else [python_bin, "-c", command]
                )
                result = subprocess.run(
                    cmd_args,
                    capture_output=True, text=True, timeout=30,
                    cwd=project_path or None,
                )
                output = (result.stdout + result.stderr).strip()[:2000]
                ok = result.returncode == 0
                log_event(
                    "run_python_ok" if ok else "run_python_failed",
                    {"returncode": result.returncode, "output_len": len(output)},
                )
                return ToolResult(
                    ok=ok,
                    message="Python executed." if ok else "Python error.",
                    data={"output": output, "returncode": result.returncode},
                )
            except subprocess.TimeoutExpired:
                log_event("run_python_timeout", {})
                return ToolResult(ok=False, message="run_python: timed out after 30s")
            except Exception as exc:
                log_event("run_python_error", {"error": str(exc)})
                return ToolResult(ok=False, message=f"run_python error: {exc}")

        if action == "run_shell":
            command = str(payload.get("command") or "").strip()
            if not command:
                log_event("run_shell_error", {"error": "no command"})
                return ToolResult(ok=False, message="run_shell: no command provided")
            _SHELL_WHITELIST = {
                "grep", "find", "ls", "cat", "python3", "pip",
                "git", "wmctrl", "pgrep", "echo",
                "tail", "journalctl", "head", "less", "jq",
                "docker", "npm", "systemctl",
            }
            try:
                tokens = shlex.split(command)
            except Exception:
                return ToolResult(ok=False, message="run_shell: could not parse command")
            first_token = tokens[0] if tokens else ""
            if first_token not in _SHELL_WHITELIST:
                return ToolResult(
                    ok=False,
                    message=f"run_shell: '{first_token}' not in whitelist. Allowed: {sorted(_SHELL_WHITELIST)}",
                )
            # Per-command sub-restrictions
            if first_token == "git":
                git_sub = tokens[1] if len(tokens) > 1 else ""
                _GIT_ALLOWED = {"status", "diff", "log", "add"}
                if git_sub not in _GIT_ALLOWED:
                    return ToolResult(
                        ok=False,
                        message=f"run_shell: git {git_sub} not allowed. Allowed: {sorted(_GIT_ALLOWED)}",
                    )
            elif first_token == "tail":
                if "-f" in tokens or "--follow" in tokens:
                    return ToolResult(ok=False, message="run_shell: tail -f is not allowed (no streaming)")
                for tok in tokens[1:]:
                    if tok.startswith("-") and not (tok == "-n" or tok.startswith("-n")):
                        return ToolResult(ok=False, message=f"run_shell: tail flag '{tok}' not allowed. Only -n is permitted.")
            elif first_token == "journalctl":
                if "-f" in tokens or "--follow" in tokens:
                    return ToolResult(ok=False, message="run_shell: journalctl -f is not allowed (no streaming)")
                _JOURNALCTL_ALLOWED = {"-n", "-u", "--since", "--no-pager", "-p"}
                for tok in tokens[1:]:
                    if tok.startswith("-"):
                        if not any(tok == f or tok.startswith(f) for f in _JOURNALCTL_ALLOWED):
                            return ToolResult(
                                ok=False,
                                message=f"run_shell: journalctl flag '{tok}' not allowed. Allowed: {sorted(_JOURNALCTL_ALLOWED)}",
                            )
            elif first_token == "docker":
                docker_sub = tokens[1] if len(tokens) > 1 else ""
                _DOCKER_ALLOWED = {"status", "logs", "ps"}
                if docker_sub not in _DOCKER_ALLOWED:
                    return ToolResult(
                        ok=False,
                        message=f"run_shell: docker {docker_sub} not allowed. Allowed: {sorted(_DOCKER_ALLOWED)}",
                    )
            elif first_token == "npm":
                npm_sub = tokens[1] if len(tokens) > 1 else ""
                _NPM_ALLOWED = {"list", "run", "test"}
                if npm_sub not in _NPM_ALLOWED:
                    return ToolResult(
                        ok=False,
                        message=f"run_shell: npm {npm_sub} not allowed. Allowed: {sorted(_NPM_ALLOWED)}",
                    )
            elif first_token == "systemctl":
                systemctl_sub = tokens[1] if len(tokens) > 1 else ""
                _SYSTEMCTL_ALLOWED = {"status", "is-active"}
                if systemctl_sub not in _SYSTEMCTL_ALLOWED:
                    return ToolResult(
                        ok=False,
                        message=f"run_shell: systemctl {systemctl_sub} not allowed. Allowed: {sorted(_SYSTEMCTL_ALLOWED)}",
                    )
            try:
                result = subprocess.run(
                    command, shell=True, capture_output=True, text=True, timeout=15,
                )
                output = (result.stdout + result.stderr).strip()[:3000]
                ok = result.returncode == 0
                log_event(
                    "run_shell_ok" if ok else "run_shell_failed",
                    {"command": command[:80], "returncode": result.returncode},
                )
                return ToolResult(
                    ok=ok,
                    message="Shell command executed.",
                    data={"output": output, "returncode": result.returncode},
                )
            except subprocess.TimeoutExpired:
                log_event("run_shell_timeout", {"command": command[:80]})
                return ToolResult(ok=False, message="run_shell: timed out after 15s")
            except Exception as exc:
                log_event("run_shell_error", {"error": str(exc)})
                return ToolResult(ok=False, message=f"run_shell error: {exc}")

        if action == "show_logs":
            source = str(payload.get("source") or "").strip()
            lines = max(1, min(500, int(payload.get("lines") or 50)))
            try:
                from prometheus.infra.log_viewer import (
                    list_log_files,
                    read_latest_log_tail,
                    read_log_tail,
                )
                if source:
                    try:
                        output = read_log_tail(source, tail_lines=lines)
                    except ValueError:
                        return ToolResult(ok=False, message=f"show_logs: invalid filename: {source!r}")
                    file_label = source
                else:
                    file_label, output = read_latest_log_tail(tail_lines=lines)
                    if not file_label:
                        log_event("show_logs_empty", {})
                        return ToolResult(
                            ok=True,
                            message="No log files found.",
                            data={"output": "", "source": "none", "count": 0},
                        )
                out_lines = [ln for ln in output.splitlines() if ln.strip()]
                count = len(out_lines)
                output_clipped = "\n".join(out_lines)[:3000]
                msg = (
                    f"No entries in {file_label}."
                    if count == 0
                    else f"Last {count} log entries from {file_label}."
                )
                log_event("show_logs_file", {"source": file_label, "lines": count})
                return ToolResult(
                    ok=True,
                    message=msg,
                    data={"output": output_clipped, "source": file_label, "count": count},
                )
            except Exception as exc:
                return ToolResult(ok=False, message=f"show_logs error: {exc}")

        if action == "search_codebase":
            query = str(payload.get("query") or "").strip()
            project_path = str(payload.get("project_path") or "").strip()
            if not query:
                return ToolResult(ok=False, message="search_codebase: no query provided")
            if not project_path:
                try:
                    vs = json.loads(VISUAL_STATE_PATH.read_text(encoding="utf-8"))
                    project_path = str(
                        vs.get("active_project_path") or vs.get("project_path") or ""
                    )
                except Exception:
                    pass
            if not project_path or not Path(project_path).is_dir():
                return ToolResult(ok=False, message="search_codebase: no valid project path")
            try:
                result = subprocess.run(
                    [
                        "grep", "-rn",
                        "--include=*.py", "--include=*.js", "--include=*.ts",
                        "--include=*.json", "--include=*.md",
                        "-m", "50", query, project_path,
                    ],
                    capture_output=True, text=True, timeout=15,
                )
                lines = result.stdout.strip().splitlines()[:50]
                output = "\n".join(lines)
                log_event("search_codebase_done", {"query": query[:80], "count": len(lines)})
                return ToolResult(
                    ok=True,
                    message=f"Found {len(lines)} matches.",
                    data={"matches": lines, "count": len(lines), "output": output},
                )
            except Exception as exc:
                log_event("search_codebase_error", {"error": str(exc)})
                return ToolResult(ok=False, message=f"search_codebase error: {exc}")

        if action == "git_status":
            project_path = str(payload.get("project_path") or "").strip()
            if not project_path:
                try:
                    vs = json.loads(VISUAL_STATE_PATH.read_text(encoding="utf-8"))
                    project_path = str(vs.get("active_project_path") or "")
                except Exception:
                    pass
            if not project_path or not Path(project_path).is_dir():
                return ToolResult(ok=False, message="git_status: no valid project path")
            try:
                result = subprocess.run(
                    ["git", "status", "--short"],
                    capture_output=True, text=True, timeout=10, cwd=project_path,
                )
                output = result.stdout.strip()
                log_event("git_status_done", {"project_path": project_path, "clean": not bool(output)})
                return ToolResult(
                    ok=True,
                    message="Git status retrieved.",
                    data={"status": output, "clean": not bool(output)},
                )
            except Exception as exc:
                log_event("git_status_error", {"error": str(exc)})
                return ToolResult(ok=False, message=f"git_status error: {exc}")

        if action == "git_diff":
            project_path = str(payload.get("project_path") or "").strip()
            file_arg = str(payload.get("file") or "").strip()
            if not project_path:
                try:
                    vs = json.loads(VISUAL_STATE_PATH.read_text(encoding="utf-8"))
                    project_path = str(vs.get("active_project_path") or "")
                except Exception:
                    pass
            if not project_path or not Path(project_path).is_dir():
                return ToolResult(ok=False, message="git_diff: no valid project path")
            try:
                cmd = ["git", "diff"]
                if file_arg:
                    cmd.append(file_arg)
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=10, cwd=project_path,
                )
                output = result.stdout.strip()[:4000]
                log_event("git_diff_done", {"project_path": project_path, "has_changes": bool(output)})
                return ToolResult(
                    ok=True,
                    message="Git diff retrieved.",
                    data={"diff": output, "has_changes": bool(output)},
                )
            except Exception as exc:
                log_event("git_diff_error", {"error": str(exc)})
                return ToolResult(ok=False, message=f"git_diff error: {exc}")

        if action == "git_commit":
            project_path = str(payload.get("project_path") or "").strip()
            message = str(payload.get("message") or "").strip()
            confirmed = bool(payload.get("confirmed", False))
            if not project_path:
                try:
                    vs = json.loads(VISUAL_STATE_PATH.read_text(encoding="utf-8"))
                    project_path = str(vs.get("active_project_path") or "")
                except Exception:
                    pass
            if not project_path or not Path(project_path).is_dir():
                return ToolResult(ok=False, message="git_commit: no valid project path")
            if not message:
                return ToolResult(ok=False, message="git_commit: no commit message provided")
            if not confirmed:
                return ToolResult(
                    ok=False,
                    message=(
                        f"Awaiting confirmation: commit all changes with message '{message}'. "
                        "Say 'confirm commit' to proceed."
                    ),
                )
            try:
                subprocess.run(
                    ["git", "add", "-A"],
                    capture_output=True, cwd=project_path, timeout=10,
                )
                result = subprocess.run(
                    ["git", "commit", "-m", message],
                    capture_output=True, text=True, timeout=15, cwd=project_path,
                )
                output = result.stdout.strip()
                ok = result.returncode == 0
                log_event(
                    "git_commit_ok" if ok else "git_commit_failed",
                    {"message": message[:80], "returncode": result.returncode},
                )
                return ToolResult(
                    ok=ok,
                    message=f"Committed: {message}" if ok else f"Commit failed: {result.stderr.strip()[:200]}",
                    data={"output": output},
                )
            except Exception as exc:
                log_event("git_commit_error", {"error": str(exc)})
                return ToolResult(ok=False, message=f"git_commit error: {exc}")

        if action == "session_wrapup":
            try:
                from session_summarizer import SessionSummarizer

                ss = SessionSummarizer()
                ok = ss.trigger_wrapup(client=None)
                wm = WorkingMemory()
                wm_data = wm.read()
                project = str(wm_data.get("active_workspace") or "the current project")
                last_req = str(
                    wm_data.get("last_user_request") or wm_data.get("last_tool_action") or "recent work"
                )
                wm.write({"next_session_context": f"{project}: {last_req[:100]}"})
                log_event("session_wrapup_triggered", {"ok": ok})
                return ToolResult(
                    ok=ok,
                    message=(
                        "Session wrapped up. Summary written to vault."
                        if ok
                        else "Wrap-up failed — check vault_path config."
                    ),
                    data={"project": project},
                )
            except Exception as exc:
                log_event("session_wrapup_error", {"error": str(exc)})
                return ToolResult(ok=False, message=f"Wrap-up error: {exc}")

        if action == "system_status":
            try:
                wm = WorkingMemory().read()
                vs: dict = {}
                try:
                    vs = json.loads(VISUAL_STATE_PATH.read_text(encoding="utf-8"))
                except Exception:
                    pass
                bt: list = []
                try:
                    bt_path = Path.home() / ".jarvis" / "background_tasks.json"
                    if bt_path.exists():
                        raw_bt = json.loads(bt_path.read_text(encoding="utf-8"))
                        if isinstance(raw_bt, list):
                            bt = [t for t in raw_bt[-3:] if isinstance(t, dict)]
                except Exception:
                    pass
                active_win = vs.get("active_window") or {}
                win_title = (
                    str(active_win.get("title", ""))
                    if isinstance(active_win, dict)
                    else str(active_win)
                )
                status = {
                    "active_project": str(
                        vs.get("active_project")
                        or vs.get("active_project_name")
                        or wm.get("active_workspace", "unknown")
                    ),
                    "active_window": win_title,
                    "last_user_request": str(wm.get("last_user_request", "")),
                    "last_tool_action": str(wm.get("last_tool_action", "")),
                    "background_tasks": bt,
                    "xbox_state": vs.get("xbox_state"),
                }
                return ToolResult(ok=True, message="System status retrieved.", data=status)
            except Exception as exc:
                log_event("system_status_error", {"error": str(exc)})
                return ToolResult(ok=False, message=f"system_status error: {exc}")

        if action == "get_priorities":
            try:
                from memory_core import query_vault

                month = time.strftime("%B")
                results = query_vault(f"priorities goals {month}", limit=5)
                priorities: list[str] = []
                for r in results[:3]:
                    title = str(r.get("title") or "").strip()
                    text = str(r.get("text") or "")
                    first_sentence = text.split(".")[0][:100].strip() if text else ""
                    label = title or first_sentence
                    if label and label not in priorities:
                        priorities.append(label)
                wm_data = WorkingMemory().read()
                active_goal = str(wm_data.get("active_goal") or "").strip()
                if active_goal and active_goal not in priorities:
                    priorities.insert(0, active_goal)
                priorities = priorities[:3]
                log_event("get_priorities_done", {"count": len(priorities)})
                return ToolResult(
                    ok=True,
                    message=f"Found {len(priorities)} priorities.",
                    data={"priorities": priorities},
                )
            except Exception as exc:
                log_event("get_priorities_error", {"error": str(exc)})
                return ToolResult(ok=False, message=f"get_priorities error: {exc}")

        if action == "query_vault":
            try:
                from memory_core import query_vault as _qv
                query = str(payload.get("query") or payload.get("q") or "").strip()
                limit = int(payload.get("limit") or 5)
                if not query:
                    return ToolResult(ok=False, message="query_vault: 'query' is required")
                results = _qv(query, limit=limit)
                chunks = []
                for r in results:
                    title = str(r.get("title") or "").strip()
                    year = str(r.get("year") or "").strip()
                    text = str(r.get("text") or "").strip()[:400]
                    if text:
                        header = f"[{title}" + (f" | {year}" if year else "") + "]"
                        chunks.append(f"{header}\n{text}")
                formatted = "\n\n".join(chunks) if chunks else "No results found in vault."
                log_event("query_vault_done", {"query": query[:60], "count": len(results)})
                return ToolResult(
                    ok=True,
                    message=formatted,
                    data={"results": results, "count": len(results)},
                )
            except Exception as exc:
                log_event("query_vault_error", {"error": str(exc)})
                return ToolResult(ok=False, message=f"query_vault error: {exc}")

        if action == "run_diagnostics":
            diag = run_diagnostics()
            return ToolResult(
                True,
                diag.get("spoken_summary", "Diagnostics complete."),
                diag,
            )

        if action == "get_mission_status":
            try:
                from mission_state import MissionState
                ms = MissionState()
                data = ms.get_mission()
                summary = ms.summary_text()
                log_event("get_mission_status", {"has_mission": bool(data.get("current_mission"))})
                return ToolResult(ok=True, message=summary, data=data)
            except Exception as exc:
                log_event("get_mission_status_error", {"error": str(exc)})
                return ToolResult(ok=False, message=f"get_mission_status error: {exc}")

        if action == "set_mission":
            try:
                from mission_state import MissionState
                mission = str(payload.get("mission") or payload.get("description") or "").strip()
                goal = str(payload.get("goal") or "").strip()
                if not mission:
                    return ToolResult(ok=False, message="set_mission: 'mission' is required")
                MissionState().set_mission(mission, goal=goal)
                log_event("mission_set_tool", {"mission": mission[:80]})
                return ToolResult(ok=True, message=f"Mission set: {mission[:60]}")
            except Exception as exc:
                log_event("set_mission_error", {"error": str(exc)})
                return ToolResult(ok=False, message=f"set_mission error: {exc}")

        if action == "add_subtask":
            try:
                from mission_state import MissionState
                description = str(payload.get("description") or payload.get("task") or "").strip()
                if not description:
                    return ToolResult(ok=False, message="add_subtask: 'description' is required")
                task_id = MissionState().add_subtask(description)
                return ToolResult(ok=True, message=f"Subtask added: {description[:60]}", data={"id": task_id})
            except Exception as exc:
                log_event("add_subtask_error", {"error": str(exc)})
                return ToolResult(ok=False, message=f"add_subtask error: {exc}")

        if action == "complete_subtask":
            try:
                from mission_state import MissionState
                ref = str(payload.get("id") or payload.get("description") or "").strip()
                if not ref:
                    return ToolResult(ok=False, message="complete_subtask: 'id' or 'description' is required")
                ok = MissionState().complete_subtask(ref)
                if ok:
                    return ToolResult(ok=True, message=f"Subtask completed: {ref[:60]}")
                return ToolResult(ok=False, message=f"No matching subtask found for: {ref[:60]}")
            except Exception as exc:
                log_event("complete_subtask_error", {"error": str(exc)})
                return ToolResult(ok=False, message=f"complete_subtask error: {exc}")

        if action == "start_coding_task":
            try:
                from coding_agent import start_coding_task as _start
                goal = str(payload.get("goal") or payload.get("description") or "").strip()
                context = str(payload.get("context") or "").strip()
                if not goal:
                    return ToolResult(ok=False, message="start_coding_task: 'goal' is required")
                result = _start(goal=goal, context=context)
                log_event("coding_task_dispatched_tool", {"goal": goal[:80]})
                return ToolResult(
                    ok=True,
                    message=f"Coding task started: {goal[:60]}",
                    data=result,
                )
            except Exception as exc:
                log_event("start_coding_task_error", {"error": str(exc)})
                return ToolResult(ok=False, message=f"start_coding_task error: {exc}")

        if action == "get_coding_status":
            try:
                from coding_agent import get_coding_status as _status
                result = _status()
                has_result = result.get("status") != "no task running"
                msg = (
                    f"Task {'succeeded' if result.get('success') else 'failed'}: {result.get('goal','')[:60]}"
                    if has_result and "success" in result
                    else "No coding task has been run yet."
                )
                return ToolResult(ok=True, message=msg, data=result)
            except Exception as exc:
                log_event("get_coding_status_error", {"error": str(exc)})
                return ToolResult(ok=False, message=f"get_coding_status error: {exc}")

        if action == "start_build":
            try:
                from orchestrator import start_build as _start_build
                goal = str(params.get("goal") or "").strip()
                context = str(params.get("context") or "").strip()
                if not goal:
                    return ToolResult(ok=False, message="start_build: 'goal' is required")
                result = _start_build(goal=goal, context=context)
                return ToolResult(
                    ok=True,
                    message=f"Build started for: {goal[:60]}",
                    data=result,
                )
            except Exception as exc:
                log_event("start_build_error", {"error": str(exc)})
                return ToolResult(ok=False, message=f"start_build error: {exc}")

        if action == "get_build_status":
            try:
                from orchestrator import get_build_status as _build_status
                result = _build_status()
                has_result = result.get("status") not in ("no build running", None)
                if not has_result:
                    msg = "No orchestrated build has been run yet."
                elif result.get("status") == "running":
                    msg = f"Build running: {result.get('goal','')[:60]}"
                elif result.get("success"):
                    tr = result.get("test_results", {})
                    msg = (
                        f"Build succeeded: {result.get('goal','')[:50]}. "
                        f"{tr.get('passed', 0)} tests passing."
                    )
                else:
                    needs_human = result.get("needs_human", False)
                    msg = (
                        f"Build {'needs human review' if needs_human else 'failed'}: "
                        f"{result.get('goal','')[:50]}."
                    )
                return ToolResult(ok=True, message=msg, data=result)
            except Exception as exc:
                log_event("get_build_status_error", {"error": str(exc)})
                return ToolResult(ok=False, message=f"get_build_status error: {exc}")

        # ── Calendar reads (read-only, no writes) ─────────────────────────────
        if action in {
            "calendar_list_upcoming",
            "calendar_get_today",
            "calendar_get_tomorrow",
            "calendar_get_date",
            "calendar_next_event",
            "calendar_summarize_day",
            "calendar_find_free_blocks",
        }:
            return self._execute_calendar_read(action, payload)

        return ToolResult(False, f"Unknown action: {action}")

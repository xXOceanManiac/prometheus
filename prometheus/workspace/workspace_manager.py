from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import os

import requests as _requests

from prometheus.infra.config import BASE_DIR, CONFIG, VISUAL_STATE_PATH
from prometheus.infra.utils import log_event

_HA_ENTITY = "media_player.blackhawkred"
_HA_TIMEOUT = 3.0  # seconds per HA request


_VS_CODE_RE = re.compile(
    r"^.+?\s*[—–\-]\s*(.+?)\s*[—–\-]\s*Visual Studio Code\s*$",
    re.IGNORECASE,
)
# Absolute paths embedded in window titles (terminals, file managers, etc.)
_TITLE_PATH_RE = re.compile(r"(/(?:home|root|tmp|opt|usr|srv)/[^\s\x00:]+)")
# KDE Konsole default title format: "dirname : shellprocess — Konsole"
_KONSOLE_DIR_RE = re.compile(r"^([^:—\n]+?)\s*(?::\s*.+?)?\s*[—–\-]\s*Konsole\s*$", re.IGNORECASE)
# Shell PS1 "user@host:~/path" or "user@host:/abs/path"
_PS1_PATH_RE = re.compile(r"@[^:]+:(?:~|(/[^\s\x00]+))")


def _run(cmd: list[str], timeout: float = 3.0) -> tuple[int, str]:
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.returncode, (r.stdout or "").strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return 1, ""


def _wid_int(s: str) -> int:
    """Parse a window ID from decimal or 0x-prefixed hex to int. Returns -1 on failure."""
    s = s.strip()
    try:
        return int(s, 16) if s.lower().startswith("0x") else int(s)
    except (ValueError, TypeError):
        return -1


def _resolve_cwd(pid: str | int) -> str:
    """Read /proc/<pid>/cwd to get the working directory of a process."""
    try:
        link = Path(f"/proc/{int(pid)}/cwd")
        resolved = link.resolve(strict=False)
        if resolved.exists():
            return str(resolved)
    except (OSError, PermissionError, ValueError, TypeError):
        pass
    return ""


def _child_pids(pid: str | int) -> list[int]:
    """Return immediate child PIDs of a process via /proc/<pid>/task/*/children."""
    children: list[int] = []
    try:
        ipid = int(pid)
        task_dir = Path(f"/proc/{ipid}/task")
        for task in task_dir.iterdir():
            children_file = task / "children"
            try:
                for cid in children_file.read_text().split():
                    children.append(int(cid))
            except Exception:
                continue
    except (OSError, PermissionError, ValueError, TypeError):
        pass
    return children


def _resolve_cwd_deep(pid: str | int) -> str:
    """
    Resolve the most relevant CWD for a window's process tree.
    For terminal emulators, the useful CWD is in the shell child, not the emulator itself.
    Walk one level of children; pick the child with the deepest (longest) path.
    """
    own_cwd = _resolve_cwd(pid)
    children = _child_pids(pid)
    candidates = [own_cwd] if own_cwd else []
    for cpid in children[:16]:
        cwd = _resolve_cwd(cpid)
        if cwd:
            candidates.append(cwd)
    if not candidates:
        return ""
    # Prefer deeper paths (more specific directories) over home directory
    home = str(Path.home())
    non_home = [c for c in candidates if c != home]
    pool = non_home if non_home else candidates
    return max(pool, key=len)


def _atomic_merge_json(path: Path, updates: dict[str, Any]) -> None:
    """Read path, merge updates, write atomically via rename. Never corrupts on crash."""
    try:
        existing: dict[str, Any] = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(existing, dict):
                    existing = {}
            except Exception:
                existing = {}
        existing.update(updates)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        try:
            path.with_suffix(".tmp").unlink(missing_ok=True)
        except Exception:
            pass


class _WindowInfo:
    __slots__ = ("window_id_int", "window_id_hex", "pid", "title", "wm_class", "workspace")

    def __init__(self) -> None:
        self.window_id_int: int = -1
        self.window_id_hex: str = ""
        self.pid: str = ""
        self.title: str = ""
        self.wm_class: str = ""
        self.workspace: str = ""

    @property
    def is_empty(self) -> bool:
        return self.window_id_int < 0 and not self.title

    def to_dict(self) -> dict[str, str]:
        return {
            "window_id": self.window_id_hex or str(self.window_id_int),
            "pid": self.pid,
            "title": self.title,
            "class": self.wm_class,
            "workspace": self.workspace,
        }


class WorkspaceManager:
    """
    Background thread that polls the active X11 window every poll_interval seconds,
    infers the active project, and writes the result to ~/.jarvis/visual_state.json.

    Uses xdotool for the focused window ID/PID/title and wmctrl for the WM_CLASS.
    Falls back gracefully when either tool is missing or the display is unavailable.
    """

    def __init__(
        self,
        poll_interval: float = 5.0,
        on_project_change=None,
        on_workspace_change=None,
    ) -> None:
        self.poll_interval = max(1.0, float(poll_interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_project_path: str = ""
        self._last_xbox_key: str = ""
        self._xdotool_ok = self._probe("xdotool")
        self._wmctrl_ok = self._probe("wmctrl")
        # HA error backoff: only log once per 5 minutes to avoid log spam.
        self._ha_last_error_ts: float = 0.0
        self._ha_error_backoff: float = 300.0
        # Optional callback: (project_name: str, project_path: str) -> None
        # Called from the workspace thread on every project change.
        self._on_project_change = on_project_change
        # Optional callback: (workspace_state: dict) -> None
        # Called when xbox state or significant workspace state changes.
        self._on_workspace_change = on_workspace_change

    @staticmethod
    def _probe(cmd: str) -> bool:
        code, _ = _run(["which", cmd], timeout=2.0)
        return code == 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def detect_once(self) -> dict[str, Any]:
        """
        Run a single synchronous detection tick and return current_project().
        Safe to call before start() — does not touch the background thread.
        """
        try:
            self._tick()
        except Exception as exc:
            log_event("workspace_detect_once_error", {"error": str(exc)})
        return self.current_project()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="workspace-watcher",
            daemon=True,
        )
        self._thread.start()
        log_event(
            "workspace_watcher_started",
            {
                "poll_interval": self.poll_interval,
                "xdotool": self._xdotool_ok,
                "wmctrl": self._wmctrl_ok,
            },
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.poll_interval + 2.0)
        log_event("workspace_watcher_stopped", {})

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:
                log_event("workspace_watcher_error", {"error": str(exc)})
            self._stop.wait(self.poll_interval)

    def _tick(self) -> None:
        win = self._get_active_window()
        xbox = self._poll_xbox_state()

        if win.is_empty and xbox is None:
            return

        cwd = _resolve_cwd_deep(win.pid) if (not win.is_empty and win.pid) else ""
        project_path, project_name = ("", "") if win.is_empty else self._infer_project(win, cwd)

        self._write_visual_state(win, project_path, project_name, xbox)

        if project_path and project_path != self._last_project_path:
            prev = self._last_project_path
            self._last_project_path = project_path
            self._notify_project_change(win, project_path, project_name)
            log_event(
                "workspace_project_changed",
                {"from": prev, "to": project_path, "name": project_name},
            )

        # Fire workspace_change callback when xbox state changes (separate from project change)
        if xbox is not None and self._on_workspace_change is not None:
            new_xbox_key = f"{xbox.get('xbox_state') or ''}|{xbox.get('xbox_app') or ''}"
            if new_xbox_key != self._last_xbox_key:
                self._last_xbox_key = new_xbox_key
                workspace_state = {
                    "project_name": project_name,
                    "active_project_path": project_path,
                    "active_window": win.to_dict() if not win.is_empty else {},
                    "xbox_state": xbox.get("xbox_state"),
                    "xbox_app": xbox.get("xbox_app"),
                    "xbox_media_title": xbox.get("xbox_media_title"),
                }
                try:
                    self._on_workspace_change(workspace_state)
                except Exception as exc:
                    log_event("workspace_change_callback_error", {"error": str(exc)})

    # ------------------------------------------------------------------
    # Window detection
    # ------------------------------------------------------------------

    def _get_active_window(self) -> _WindowInfo:
        win = _WindowInfo()

        if not self._xdotool_ok:
            return win

        # Get focused window ID
        code, raw_id = _run(["xdotool", "getactivewindow"])
        if code != 0 or not raw_id:
            return win
        win.window_id_int = _wid_int(raw_id)
        if win.window_id_int < 0:
            return win

        # Title
        _, win.title = _run(["xdotool", "getwindowname", raw_id])

        # PID
        _, win.pid = _run(["xdotool", "getwindowpid", raw_id])
        win.pid = win.pid.strip()

        # WM_CLASS + workspace from wmctrl (compares IDs as integers to handle hex/dec mismatch)
        if self._wmctrl_ok:
            code, output = _run(["wmctrl", "-lx"])
            if code == 0:
                for line in output.splitlines():
                    parts = line.split(None, 4)
                    if len(parts) < 5:
                        continue
                    lwid, workspace, wm_class, _host, wmctrl_title = parts
                    if _wid_int(lwid) == win.window_id_int:
                        win.window_id_hex = lwid
                        win.workspace = workspace
                        win.wm_class = wm_class
                        if not win.title:
                            win.title = wmctrl_title
                        break

        if not win.window_id_hex:
            win.window_id_hex = hex(win.window_id_int)

        return win

    # ------------------------------------------------------------------
    # Project inference
    # ------------------------------------------------------------------

    def _infer_project(self, win: _WindowInfo, cwd: str) -> tuple[str, str]:
        """Return (project_path, project_name). Both empty strings if unknown."""
        config_projects: dict[str, Any] = CONFIG.get("projects", {})

        # 1. PID CWD — most reliable for terminals and editors
        if cwd:
            hit = self._match_by_path(cwd, config_projects)
            if hit:
                return hit
            cp = Path(cwd)
            if (cp / ".git").is_dir() or (cp / "pyproject.toml").is_file() or (cp / "package.json").is_file():
                return cwd, cp.name

        wm_lower = win.wm_class.lower()
        title = win.title.strip()

        # 2. VS Code: "filename — Project Name — Visual Studio Code"
        if "code" in wm_lower or "vscode" in wm_lower:
            m = _VS_CODE_RE.match(title)
            if m:
                hint = m.group(1).strip()
                hit = self._match_by_name(hint, config_projects)
                if hit:
                    return hit
                found = self._search_roots(hint)
                if found:
                    return found, Path(found).name

        # 3. Terminal (konsole/bash/zsh): parse title for cwd hints
        if any(k in wm_lower for k in ("konsole", "terminal", "bash", "zsh", "xterm", "kitty", "alacritty")):
            # KDE Konsole format: "dirname : process — Konsole"
            km = _KONSOLE_DIR_RE.match(title)
            if km:
                dir_hint = km.group(1).strip()
                if dir_hint:
                    hit = self._match_by_name(dir_hint, config_projects)
                    if hit:
                        return hit
                    found = self._search_roots(dir_hint)
                    if found:
                        return found, Path(found).name
            # Absolute paths in title
            for m in _TITLE_PATH_RE.finditer(title):
                raw_path = m.group(1).rstrip(":")
                p = Path(raw_path).expanduser()
                if p.is_dir():
                    hit = self._match_by_path(str(p), config_projects)
                    if hit:
                        return hit
                    if (p / ".git").is_dir():
                        return str(p), p.name
            # Shell PS1 "user@host:~/path" — tilde is home, so we rely on CWD instead

        # 4. Dolphin / Nautilus file managers: show path in title
        if any(k in wm_lower for k in ("dolphin", "nautilus", "nemo", "thunar")):
            for m in _TITLE_PATH_RE.finditer(title):
                raw_path = m.group(1).rstrip(":")
                p = Path(raw_path).expanduser()
                if p.is_dir():
                    hit = self._match_by_path(str(p), config_projects)
                    if hit:
                        return hit

        # 5. Scan title for known project keys or directory names
        title_lower = title.lower()
        for key, proj_cfg in config_projects.items():
            if not isinstance(proj_cfg, dict):
                continue
            proj_path_str = str(proj_cfg.get("path", "")).strip()
            if not proj_path_str:
                continue
            proj_path = Path(proj_path_str).expanduser()
            proj_name_lower = proj_path.name.lower()
            if key.lower() in title_lower or proj_name_lower in title_lower:
                if proj_path.is_dir():
                    return str(proj_path), proj_path.name

        return "", ""

    def _match_by_path(
        self, path: str, config_projects: dict[str, Any]
    ) -> tuple[str, str] | None:
        """Return (project_path, name) if path is at or inside a known project root."""
        try:
            p = Path(path).resolve()
        except Exception:
            return None
        for key, cfg in config_projects.items():
            if not isinstance(cfg, dict):
                continue
            raw = str(cfg.get("path", "")).strip()
            if not raw:
                continue
            try:
                root = Path(raw).expanduser().resolve()
            except Exception:
                continue
            if p == root or str(p).startswith(str(root) + os.sep):
                return str(root), root.name
        return None

    def _match_by_name(
        self, name: str, config_projects: dict[str, Any]
    ) -> tuple[str, str] | None:
        """Return (project_path, name) for an exact key or directory name match."""
        nl = name.strip().lower()
        if not nl:
            return None
        for key, cfg in config_projects.items():
            if not isinstance(cfg, dict):
                continue
            raw = str(cfg.get("path", "")).strip()
            if not raw:
                continue
            p = Path(raw).expanduser()
            if key.lower() == nl or p.name.lower() == nl:
                if p.is_dir():
                    return str(p), p.name
        return None

    def _search_roots(self, name: str) -> str | None:
        """Fuzzy-search project_search_roots for a directory named like `name`."""
        nl = name.strip().lower()
        if not nl:
            return None
        roots: list[str] = CONFIG.get(
            "project_search_roots",
            [str(Path.home() / "Desktop"), str(Path.home() / "Documents")],
        )
        exact: str | None = None
        partial: str | None = None
        for root_str in roots:
            root = Path(root_str).expanduser()
            if not root.is_dir():
                continue
            try:
                for child in root.iterdir():
                    if not child.is_dir():
                        continue
                    cn = child.name.lower()
                    if cn == nl:
                        exact = str(child)
                        break
                    if nl in cn and partial is None:
                        partial = str(child)
            except PermissionError:
                continue
            if exact:
                break
        return exact or partial

    # ------------------------------------------------------------------
    # Xbox / Home Assistant media state
    # ------------------------------------------------------------------

    def _poll_xbox_state(self) -> dict[str, Any] | None:
        """
        Query HA for media_player.blackhawkred.
        Returns dict with xbox_state/xbox_app/xbox_media_title, or None on error.
        On any failure the window polling continues unaffected.
        """
        base_url = os.getenv("HOME_ASSISTANT_URL", "").strip().rstrip("/")
        token = os.getenv("HOME_ASSISTANT_API_KEY", "").strip()
        if not base_url or not token:
            return None
        try:
            resp = _requests.get(
                f"{base_url}/api/states/{_HA_ENTITY}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=_HA_TIMEOUT,
            )
            if resp.status_code == 404:
                return {"xbox_state": None, "xbox_app": None, "xbox_media_title": None}
            if resp.status_code >= 400:
                return None
            data = resp.json()
            attrs = data.get("attributes") or {}
            return {
                "xbox_state": str(data.get("state") or ""),
                "xbox_app": str(attrs.get("app_name") or attrs.get("source") or ""),
                "xbox_media_title": str(attrs.get("media_title") or ""),
            }
        except Exception as exc:
            now = time.time()
            if now - self._ha_last_error_ts >= self._ha_error_backoff:
                self._ha_last_error_ts = now
                log_event("xbox_poll_error", {"error": str(exc)})
            return None

    # ------------------------------------------------------------------
    # State writing
    # ------------------------------------------------------------------

    def _write_visual_state(
        self,
        win: _WindowInfo,
        project_path: str,
        project_name: str,
        xbox: dict[str, Any] | None = None,
    ) -> None:
        updates: dict[str, Any] = {
            "active_project_path": project_path,
            "active_project_name": project_name,
            "workspace_updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        if not win.is_empty:
            updates["active_window"] = win.to_dict()
        if xbox is not None:
            updates["xbox_state"] = xbox.get("xbox_state")
            updates["xbox_app"] = xbox.get("xbox_app")
            updates["xbox_media_title"] = xbox.get("xbox_media_title")
        try:
            _atomic_merge_json(VISUAL_STATE_PATH, updates)
        except Exception as exc:
            log_event("workspace_state_write_error", {"error": str(exc)})

    def _notify_project_change(
        self, win: _WindowInfo, project_path: str, project_name: str
    ) -> None:
        try:
            from prometheus.memory.working_memory import WorkingMemory
            wm = WorkingMemory()
            updates: dict[str, Any] = {"screen_focus": win.to_dict()}
            if project_name:
                updates["active_workspace"] = project_name
                updates["active_context_name"] = project_name
            wm.write(updates)
        except Exception as exc:
            log_event("workspace_working_memory_error", {"error": str(exc)})

        if self._on_project_change is not None:
            try:
                self._on_project_change(project_name, project_path)
            except Exception as exc:
                log_event("workspace_change_callback_error", {"error": str(exc)})

    # ------------------------------------------------------------------
    # Public utilities (used by tools.py / planner)
    # ------------------------------------------------------------------

    def normalize_project_name(self, value: str) -> str:
        return " ".join(str(value or "").strip().lower().replace("_", " ").split())

    def project_hint_to_path(self, value: str) -> str:
        """Resolve a project hint (key name or directory name) to its filesystem path."""
        hint = self.normalize_project_name(value)
        if not hint:
            return ""
        config_projects: dict[str, Any] = CONFIG.get("projects", {})
        hit = self._match_by_name(hint, config_projects)
        if hit:
            return hit[0]
        found = self._search_roots(hint)
        return found or ""

    def current_project(self) -> dict[str, Any]:
        """Return the last-written project state from visual_state.json."""
        try:
            data = json.loads(VISUAL_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {
                    "project_path": data.get("active_project_path", ""),
                    "project_name": data.get("active_project_name", ""),
                    "active_window": data.get("active_window", {}),
                    "updated_at": data.get("workspace_updated_at", ""),
                    "xbox_state": data.get("xbox_state"),
                    "xbox_app": data.get("xbox_app"),
                    "xbox_media_title": data.get("xbox_media_title"),
                    "open_windows": data.get("open_windows", []),
                }
        except Exception:
            pass
        return {"project_path": "", "project_name": "", "active_window": {}, "updated_at": ""}

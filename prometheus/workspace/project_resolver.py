"""
prometheus/workspace/project_resolver.py — Resolve which project the user means.

Used by the summarize_screen tool to attach the right project to a request.
Reads remembered contexts from MemoryStore and session state from WorkingMemory;
never writes anything.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


class ProjectResolver:
    def __init__(self, memory: Any, working: Any) -> None:
        self.memory = memory
        self.working = working

    def _norm(self, value: str) -> str:
        value = str(value or "").strip().lower()
        for ch in ["_", "-", "/", ".", ":", "\\"]:
            value = value.replace(ch, " ")
        return " ".join(value.split())

    def _read_working(self) -> dict[str, Any]:
        try:
            return self.working.read()
        except Exception:
            return {}

    def _maybe_existing_path(self, value: str) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        p = Path(raw).expanduser()
        return str(p) if p.exists() else ""

    def _all_contexts(self) -> list[dict[str, Any]]:
        try:
            data = self.memory._read()  # type: ignore[attr-defined]
            return list(data.get("contexts", []))
        except Exception:
            return []

    def _find_context_by_name(self, name: str) -> dict[str, Any] | None:
        if not name:
            return None
        try:
            return self.memory.get_context(name)
        except Exception:
            return None

    def _cwd_from_pid(self, pid_value: Any) -> str:
        try:
            pid = int(pid_value)
        except Exception:
            return ""
        proc_path = Path(f"/proc/{pid}/cwd")
        try:
            resolved = proc_path.resolve()
        except Exception:
            return ""
        return self._maybe_existing_path(str(resolved))

    def _paths_from_window_title(self, title: str) -> list[str]:
        out: list[str] = []
        q = self._norm(title)
        if not q:
            return out
        for ctx in self._all_contexts():
            proj = self._maybe_existing_path(ctx.get("project_path", ""))
            if not proj:
                continue
            name = self._norm(ctx.get("name", ""))
            base = self._norm(Path(proj).name)
            if (name and name in q) or (base and base in q):
                out.append(proj)
        return out

    def resolve_active_project(
        self,
        desktop_state: dict[str, Any] | None = None,
        request_text: str = "",
    ) -> dict[str, Any]:
        """
        Resolution priority:
        1) explicit mention in current request
        2) active window PID cwd (terminal / code child process)
        3) active context / workspace from working memory
        4) active window title match
        5) last plan / last tool result project path
        6) most recently used context with a valid path
        """
        working = self._read_working()
        candidates: list[tuple[str, str, float, str]] = []

        req = self._norm(
            request_text
            or " ".join(
                [
                    str(working.get("last_user_request", "")),
                    str(working.get("last_user_transcript", "")),
                ]
            )
        )

        # 1) explicit current request mention
        if req:
            for ctx in self._all_contexts():
                proj = self._maybe_existing_path(ctx.get("project_path", ""))
                if not proj:
                    continue
                ctx_name = self._norm(ctx.get("name", ""))
                proj_name = self._norm(Path(proj).name)
                if (ctx_name and ctx_name in req) or (proj_name and proj_name in req):
                    candidates.append(
                        (
                            proj,
                            str(ctx.get("name", "")).strip() or Path(proj).name,
                            1.00,
                            "request_text",
                        )
                    )

        # 2) active PID cwd
        if desktop_state:
            active = desktop_state.get("active_window", {}) or {}
            cwd = self._cwd_from_pid(active.get("pid"))
            if cwd:
                candidates.append((cwd, Path(cwd).name, 0.99, "active_pid_cwd"))

        # 3) active context / workspace
        for label in [
            str(working.get("active_context_name", "")).strip(),
            str(working.get("active_workspace", "")).strip(),
        ]:
            if not label or label.lower().startswith("session "):
                continue
            ctx = self._find_context_by_name(label)
            if ctx:
                proj = self._maybe_existing_path(ctx.get("project_path", ""))
                if proj:
                    candidates.append(
                        (
                            proj,
                            str(ctx.get("name", "")).strip() or Path(proj).name,
                            0.96,
                            "working_context",
                        )
                    )

        # 4) active window title match
        if desktop_state:
            title = str(
                (desktop_state.get("active_window") or {}).get("title", "")
            ).strip()
            for proj in self._paths_from_window_title(title):
                candidates.append((proj, Path(proj).name, 0.92, "window_title"))

        # 5) last plan / result
        last_tool = working.get("last_tool_result", {}) or {}
        plan = (last_tool.get("data") or {}).get("plan", {}) or {}
        for maybe in [
            plan.get("project_path", ""),
            (working.get("last_plan") or {}).get("project_path", ""),
        ]:
            proj = self._maybe_existing_path(maybe)
            if proj:
                candidates.append((proj, Path(proj).name, 0.70, "last_plan"))

        # 6) newest remembered context with valid path
        for ctx in reversed(self._all_contexts()):
            proj = self._maybe_existing_path(ctx.get("project_path", ""))
            if proj:
                candidates.append(
                    (
                        proj,
                        str(ctx.get("name", "")).strip() or Path(proj).name,
                        0.45,
                        "recent_context",
                    )
                )
                break

        if not candidates:
            return {"project_path": "", "project_name": "", "source": "none"}

        best = sorted(candidates, key=lambda item: item[2], reverse=True)[0]
        return {"project_path": best[0], "project_name": best[1], "source": best[3]}

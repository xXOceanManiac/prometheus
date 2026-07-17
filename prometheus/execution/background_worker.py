from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

_TASKS_FILE = Path.home() / ".jarvis" / "background_tasks.json"


# ---------------------------------------------------------------------------
# Tasks-file helpers (safe to call from any process)
# ---------------------------------------------------------------------------

def _humanize(action: str) -> str:
    return action.replace("_", " ").title()


def _update_tasks_file(task_id: str, task_data: dict[str, Any]) -> None:
    """Atomically update or insert a task entry in background_tasks.json."""
    _TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(3):
        try:
            if _TASKS_FILE.exists():
                raw = _TASKS_FILE.read_text(encoding="utf-8")
                data: dict[str, Any] = json.loads(raw)
                if not isinstance(data, dict):
                    data = {}
            else:
                data = {}

            tasks: list[dict[str, Any]] = data.get("tasks") or []
            if not isinstance(tasks, list):
                tasks = []

            updated = False
            for i, t in enumerate(tasks):
                if isinstance(t, dict) and t.get("id") == task_id:
                    tasks[i] = task_data
                    updated = True
                    break
            if not updated:
                tasks.append(task_data)

            data["tasks"] = tasks[-20:]  # keep last 20

            tmp = _TASKS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, _TASKS_FILE)
            return
        except Exception:
            time.sleep(0.05)


# ---------------------------------------------------------------------------
# Module-level worker function — must be picklable for ProcessPoolExecutor
# ---------------------------------------------------------------------------

def _run_background_task(
    task_description: str,
    context: dict[str, Any],
    task_id: str,
) -> dict[str, Any]:
    """
    Runs in a worker subprocess.  Imports are deferred so the subprocess
    does not inherit the parent's open file handles or audio threads.
    """
    _project_root = str(Path(__file__).resolve().parents[2])
    if _project_root not in sys.path:
        sys.path.insert(0, _project_root)

    try:
        import psutil
        psutil.Process().cpu_affinity([4, 5, 6, 7])
    except Exception:
        pass

    started_at = time.strftime("%Y-%m-%dT%H:%M:%S")

    result: dict[str, Any] = {
        "id": task_id,
        "description": task_description,
        "ok": False,
        "message": "",
        "result_summary": "",
        "output_path": "",
        "steps_run": 0,
        "cycles": 0,
    }

    try:
        from prometheus.planning.planner import Planner
        from prometheus.planning.executor import Executor
        from prometheus.planning.verifier import Verifier
        from prometheus.execution.tools import ToolRegistry
        from prometheus.memory.working_memory import WorkingMemory
        from prometheus.infra.utils import log_event

        planner = Planner()
        tools = ToolRegistry()
        executor = Executor(tools)
        verifier = Verifier()
        working = WorkingMemory()

        MAX_CYCLES = 3
        correction_context: dict[str, Any] = {}

        for cycle in range(MAX_CYCLES):
            result["cycles"] = cycle + 1
            merged = {**context, **correction_context}

            plan = planner.build(task_description, merged)

            # Write initial task skeleton once we have the plan steps
            task_entry: dict[str, Any] = {
                "id": task_id,
                "intent": task_description,
                "status": "running",
                "steps": [
                    {"name": _humanize(s.action), "status": "pending"}
                    for s in plan.steps
                ],
                "started_at": started_at,
                "completed_at": None,
            }
            _update_tasks_file(task_id, task_entry)

            if plan.clarification_needed:
                result["ok"] = False
                result["message"] = f"Clarification needed: {plan.clarification_question}"
                task_entry["status"] = "failed"
                task_entry["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                _update_tasks_file(task_id, task_entry)
                break

            if not plan.steps:
                result["ok"] = False
                result["message"] = "Planner produced no steps."
                task_entry["status"] = "failed"
                task_entry["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                _update_tasks_file(task_id, task_entry)
                break

            def _on_step(idx: int, step_result: Any, exec_result: Any) -> None:
                steps_info = []
                for i, ps in enumerate(plan.steps):
                    if i < idx:
                        sr = exec_result.steps[i]
                        status = "complete" if sr.ok else "failed"
                    elif i == idx:
                        status = "complete" if step_result.ok else "failed"
                    elif i == idx + 1:
                        status = "running"
                    else:
                        status = "pending"
                    steps_info.append({"name": _humanize(ps.action), "status": status})
                task_entry["steps"] = steps_info
                _update_tasks_file(task_id, {**task_entry})

            exec_result = executor.run(plan, merged, on_step=_on_step)
            result["steps_run"] = exec_result.total_steps

            verification = verifier.verify(task_description, plan, exec_result)

            if verification.verified:
                result["ok"] = True
                result["message"] = f"Task complete. {exec_result.summary}"
                result["result_summary"] = exec_result.summary
                # Extract output_path from last step result if available
                for step_result in reversed(exec_result.steps):
                    candidate = str(
                        (step_result.data or {}).get("output_path")
                        or (step_result.data or {}).get("path")
                        or ""
                    ).strip()
                    if candidate:
                        result["output_path"] = candidate
                        break
                task_entry["status"] = "complete"
                task_entry["result_summary"] = result["result_summary"]
                task_entry["output_path"] = result["output_path"]
                task_entry["steps"] = [
                    {"name": _humanize(s.action), "status": "complete" if r.ok else "failed"}
                    for s, r in zip(plan.steps, exec_result.steps)
                ]
                task_entry["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                _update_tasks_file(task_id, task_entry)
                break

            correction_context = verification.correction_context
            log_event("background_task_cycle_failed", {
                "cycle": cycle + 1,
                "reason": verification.reason,
                "description": task_description[:80],
            })
        else:
            result["ok"] = False
            result["message"] = f"Task failed after {MAX_CYCLES} cycles."
            task_entry["status"] = "failed"
            task_entry["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            _update_tasks_file(task_id, task_entry)

        wm_data = working.read()
        completed: list[dict[str, Any]] = wm_data.get("completed_tasks") or []
        if not isinstance(completed, list):
            completed = []
        completed.append({**result, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
        working.write({
            "completed_tasks": completed[-20:],
            "background_task_state": {},
        })

    except Exception as exc:
        result["ok"] = False
        result["message"] = f"Worker error: {exc}"
        try:
            _update_tasks_file(task_id, {
                "id": task_id,
                "intent": task_description,
                "status": "failed",
                "steps": [],
                "started_at": started_at,
                "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
        except Exception:
            pass
        try:
            from prometheus.infra.utils import log_event
            log_event("background_task_worker_error", {
                "error": str(exc)[:200],
                "description": task_description[:80],
            })
        except Exception:
            pass

    return result


# ---------------------------------------------------------------------------
# Pool manager — lives in the main process
# ---------------------------------------------------------------------------

class BackgroundWorkerPool:
    """
    Manages a ProcessPoolExecutor capped at max_workers=4.
    Workers are pinned to CPU cores 4-7 inside _run_background_task.
    """

    def __init__(self, max_workers: int = 4) -> None:
        self._max_workers = max_workers
        self._executor: concurrent.futures.ProcessPoolExecutor | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._on_complete: Callable[[dict[str, Any]], None] | None = None
        # Maps future → (description, per-task completion_callback | None)
        self._active: dict[
            concurrent.futures.Future[dict[str, Any]],
            tuple[str, Callable[[dict[str, Any]], None] | None],
        ] = {}

    def start(
        self,
        loop: asyncio.AbstractEventLoop | None = None,
        on_complete: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._loop = loop
        self._on_complete = on_complete
        self._executor = concurrent.futures.ProcessPoolExecutor(
            max_workers=self._max_workers
        )
        try:
            from prometheus.infra.utils import log_event
            log_event("background_pool_started", {"max_workers": self._max_workers})
        except Exception:
            pass

    def submit(
        self,
        description: str,
        context: dict[str, Any] | None = None,
        completion_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> concurrent.futures.Future[dict[str, Any]] | None:
        if self._executor is None:
            try:
                from prometheus.infra.utils import log_event
                log_event("background_pool_not_started", {"description": description[:80]})
            except Exception:
                pass
            return None

        task_id = uuid.uuid4().hex[:8]

        # Write a placeholder entry immediately so HUD shows the task right away
        _update_tasks_file(task_id, {
            "id": task_id,
            "intent": description,
            "status": "running",
            "steps": [],
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "completed_at": None,
        })

        future = self._executor.submit(
            _run_background_task, description, context or {}, task_id
        )
        self._active[future] = (description, completion_callback)
        future.add_done_callback(self._done_callback)

        try:
            from prometheus.infra.utils import log_event
            log_event("background_task_submitted", {
                "task_id": task_id,
                "description": description[:80],
            })
        except Exception:
            pass

        return future

    def _done_callback(
        self, future: concurrent.futures.Future[dict[str, Any]]
    ) -> None:
        entry = self._active.pop(future, ("", None))
        _description, per_task_callback = entry if isinstance(entry, tuple) else (str(entry), None)

        try:
            result = future.result()
        except Exception as exc:
            result = {
                "ok": False,
                "message": f"Future error: {exc}",
                "description": _description,
                "result_summary": "",
                "output_path": "",
            }

        try:
            from prometheus.infra.utils import log_event
            log_event("background_task_done", {
                "ok": result.get("ok"),
                "description": str(result.get("description", ""))[:80],
                "cycles": result.get("cycles", 0),
            })
        except Exception:
            pass

        if self._on_complete is not None:
            if self._loop is not None and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._on_complete, result)
            else:
                try:
                    self._on_complete(result)
                except Exception:
                    pass

        if per_task_callback is not None:
            if self._loop is not None and self._loop.is_running():
                self._loop.call_soon_threadsafe(per_task_callback, result)
            else:
                try:
                    per_task_callback(result)
                except Exception:
                    pass

    def shutdown(self, wait: bool = True, cancel_futures: bool = True) -> None:
        if self._executor is not None:
            try:
                self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
            except TypeError:
                self._executor.shutdown(wait=wait)
            self._executor = None
        try:
            from prometheus.infra.utils import log_event
            log_event("background_pool_stopped", {})
        except Exception:
            pass

    @property
    def active_count(self) -> int:
        return len(self._active)

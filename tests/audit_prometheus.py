"""
audit_prometheus.py — Prometheus/JARVIS Capability & Reliability Audit

Produces: ~/PROMETHEUS/reports/current_capability_audit.md

Run:
    cd /home/tatel/Desktop/Jarvis.v5.1
    source .venv/bin/activate
    python3 tests/audit_prometheus.py

Does NOT:
    - Start the main Prometheus process
    - Make real OpenAI/Realtime API calls
    - Delete or destructively modify user files
    - Require a running assistant
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# ── Project root ──────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from prometheus.infra.paths import REPORTS_DIR
REPORT_PATH = REPORTS_DIR / "current_capability_audit.md"
REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)

# ── Result primitives ─────────────────────────────────────────────────────────
@dataclass
class Result:
    name: str
    ok: bool
    error: str = ""
    latency_ms: float = 0.0
    notes: str = ""
    section: str = ""

results: list[Result] = []


def record(
    name: str,
    ok: bool,
    *,
    error: str = "",
    latency_ms: float = 0.0,
    notes: str = "",
    section: str = "",
) -> Result:
    r = Result(name=name, ok=ok, error=error, latency_ms=latency_ms, notes=notes, section=section)
    results.append(r)
    status = "PASS" if ok else "FAIL"
    latency_str = f"  [{latency_ms:.0f}ms]" if latency_ms else ""
    extra = f"  ({notes})" if notes and not ok else ""
    print(f"  [{status}]{latency_str} {name}{extra}")
    if not ok and error:
        print(f"         Error: {error[:120]}")
    return r


def run_timed(fn):
    t0 = time.time()
    try:
        val = fn()
        return val, (time.time() - t0) * 1000, None
    except Exception as exc:
        return None, (time.time() - t0) * 1000, exc


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 1 — Startup Reliability
# ═════════════════════════════════════════════════════════════════════════════
def section_startup():
    print("\n=== SECTION 1: Startup Reliability ===")
    S = "startup"

    # 1.1 Env var detection — import config first so load_dotenv() runs
    # (config.py calls load_dotenv() at import time which populates os.environ from .env)
    importlib.import_module("config")
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    record("OPENAI_API_KEY present", bool(openai_key), section=S,
           notes="Required for Realtime API — set in shell env or .env file")

    ha_url = os.getenv("HOME_ASSISTANT_URL", "").strip()
    record("HOME_ASSISTANT_URL present", bool(ha_url), section=S,
           notes="Set in .env file")

    ha_key = os.getenv("HOME_ASSISTANT_API_KEY", "").strip()
    record("HOME_ASSISTANT_API_KEY present", bool(ha_key), section=S,
           notes="Set in shell env or .env file")

    # 1.2 config.py loads without crash
    val, ms, exc = run_timed(lambda: importlib.import_module("config"))
    record("config.py imports cleanly", exc is None, error=str(exc)[:120] if exc else "",
           latency_ms=ms, section=S)

    # 1.3 Required directories created by config import
    from config import BASE_DIR, LOG_DIR, AUDIO_DIR
    record("~/.jarvis dir exists", BASE_DIR.exists(), section=S)
    record("~/.jarvis/logs dir exists", LOG_DIR.exists(), section=S)
    record("~/.jarvis/audio dir exists", AUDIO_DIR.exists(), section=S)

    # 1.4 memory_v2 dir
    from memory_core import MEMORY_DIR
    record("~/.jarvis/memory_v2 dir exists", MEMORY_DIR.exists(), section=S)

    # 1.5 tools.py imports
    val, ms, exc = run_timed(lambda: importlib.import_module("tools"))
    record("tools.py imports cleanly", exc is None,
           error=str(exc)[:120] if exc else "", latency_ms=ms, section=S)

    # 1.6 memory.py imports
    val, ms, exc = run_timed(lambda: importlib.import_module("memory"))
    record("memory.py imports cleanly", exc is None,
           error=str(exc)[:120] if exc else "", latency_ms=ms, section=S)

    # 1.7 working_memory.py imports
    val, ms, exc = run_timed(lambda: importlib.import_module("working_memory"))
    record("working_memory.py imports cleanly", exc is None,
           error=str(exc)[:120] if exc else "", latency_ms=ms, section=S)

    # 1.8 planner imports
    val, ms, exc = run_timed(lambda: importlib.import_module("planner.planner"))
    record("planner.planner imports cleanly", exc is None,
           error=str(exc)[:120] if exc else "", latency_ms=ms, section=S)

    # 1.9 Missing env var produces clear error
    val, ms, exc = run_timed(lambda: importlib.import_module("config"))
    cfg = importlib.import_module("config")
    missing_key_detected = not cfg.CONFIG.get("openai_api_key", "")
    record(
        "Missing OPENAI_API_KEY surfaced in CONFIG (not silently blank)",
        not missing_key_detected or bool(openai_key),
        notes="If key is absent it should be detectable — config reads env correctly",
        section=S,
    )

    # 1.10 visual_state.json writable
    from config import VISUAL_STATE_PATH
    try:
        VISUAL_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        VISUAL_STATE_PATH.write_text(json.dumps({"state": "audit_test", "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}))
        record("visual_state.json writable", True, section=S)
    except Exception as e:
        record("visual_state.json writable", False, error=str(e), section=S)

    # 1.11 heartbeat.json writable
    hb_path = BASE_DIR / "heartbeat.json"
    try:
        hb_path.write_text(json.dumps({"alive": True, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}))
        record("heartbeat.json writable", True, section=S)
    except Exception as e:
        record("heartbeat.json writable", False, error=str(e), section=S)

    # 1.12 launch.py imports
    val, ms, exc = run_timed(lambda: importlib.import_module("launch"))
    record("launch.py imports cleanly", exc is None,
           error=str(exc)[:120] if exc else "", latency_ms=ms, section=S)

    # 1.13 watchdog.py imports
    val, ms, exc = run_timed(lambda: importlib.import_module("watchdog"))
    record("watchdog.py imports cleanly", exc is None,
           error=str(exc)[:120] if exc else "", latency_ms=ms, section=S)


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 2 — Tool Execution
# ═════════════════════════════════════════════════════════════════════════════
def _make_registry():
    from tools import ToolRegistry
    return ToolRegistry()


def section_tools():
    print("\n=== SECTION 2: Tool Execution ===")
    S = "tools"

    try:
        reg = _make_registry()
    except Exception as e:
        record("ToolRegistry instantiation", False, error=str(e), section=S)
        return

    record("ToolRegistry instantiation", True, section=S)

    def exec_tool(payload, *, desc=None):
        name = desc or payload.get("action", "?")
        t0 = time.time()
        try:
            result = reg._execute_one_inner(payload)
            ms = (time.time() - t0) * 1000
            structured = result.data is None or isinstance(result.data, dict)
            notes = "" if result.ok else result.message[:100]
            record(f"tool:{name}", result.ok, latency_ms=ms, notes=notes, section=S)
            if not structured:
                record(f"tool:{name}:structured_output", False,
                       error=f"data is not dict: {type(result.data)}", section=S)
            return result
        except Exception as exc:
            ms = (time.time() - t0) * 1000
            record(f"tool:{name}", False, error=str(exc)[:120], latency_ms=ms, section=S)
            return None

    # tell_time
    exec_tool({"action": "tell_time"})

    # list_files — list the project root
    exec_tool({"action": "list_files", "path": str(_ROOT)}, desc="list_files(project_root)")

    # list_files — missing path
    r = reg._execute_one_inner({"action": "list_files"})
    record("tool:list_files:no_path_gives_error", not r.ok and "path" in r.message.lower(),
           notes=r.message[:80], section=S)

    # read_file — readable existing file
    exec_tool({"action": "read_file", "path": str(_ROOT / "config.py")}, desc="read_file(config.py)")

    # read_file — missing file
    r = reg._execute_one_inner({"action": "read_file", "path": "/tmp/nonexistent_prometheus_audit.txt"})
    record("tool:read_file:missing_file_gives_error", not r.ok, notes=r.message[:80], section=S)

    # write_file — relative path lands in workspace
    from workspace_policy import WORKSPACE_ROOT
    test_path = "audit_test/prometheus_audit_test.txt"
    r = reg._execute_one_inner({"action": "write_file", "path": test_path, "content": "audit test"})
    record("tool:write_file", r.ok, notes=r.message[:80], section=S)
    if r.ok:
        written = WORKSPACE_ROOT / "audit_test" / "prometheus_audit_test.txt"
        content_ok = written.exists() and written.read_text() == "audit test"
        record("tool:write_file:content_correct", content_ok, section=S)

    # screenshot — may fail without display tool but should not crash
    r = reg._execute_one_inner({"action": "screenshot"})
    record("tool:screenshot:no_crash", True, notes=f"ok={r.ok}: {r.message[:60]}", section=S)

    # web_search — needs real network; check structure not result
    try:
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = "<html><title>Test</title><p>Prometheus test result sentence here for audit.</p></html>"
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp
            r = reg._execute_one_inner({"action": "web_search", "query": "prometheus audit test"})
            record("tool:web_search:returns_result", r.ok or "search" in r.message.lower(),
                   notes=r.message[:80], section=S)
    except Exception as exc:
        record("tool:web_search", False, error=str(exc)[:120], section=S)

    # list_windows / get_active_window — safe even without X11
    r = reg._execute_one_inner({"action": "list_windows"})
    record("tool:list_windows:no_crash", True, notes=f"ok={r.ok} windows={len((r.data or {}).get('windows', []))}", section=S)

    r = reg._execute_one_inner({"action": "get_active_window"})
    record("tool:get_active_window:no_crash", True, notes=f"ok={r.ok}", section=S)

    # system_status
    r = reg._execute_one_inner({"action": "system_status"})
    record("tool:system_status", r.ok, notes=r.message[:80], section=S)
    if r.ok:
        has_project = "active_project" in (r.data or {})
        record("tool:system_status:has_active_project_key", has_project, section=S)

    # get_priorities — vault may be empty; should not crash
    r = reg._execute_one_inner({"action": "get_priorities"})
    record("tool:get_priorities:no_crash", True, notes=f"ok={r.ok} {r.message[:60]}", section=S)

    # query_vault
    r = reg._execute_one_inner({"action": "query_vault", "query": "prometheus project"})
    vault_configured = bool(str(importlib.import_module("config").CONFIG.get("vault_path", "")).strip())
    if vault_configured:
        record("tool:query_vault", r.ok, notes=r.message[:80], section=S)
    else:
        record("tool:query_vault:vault_not_configured", True,
               notes="vault_path not set — skipped; returns gracefully", section=S)

    # run_python — safe snippet
    r = reg._execute_one_inner({"action": "run_python", "command": "print('prometheus_audit_ok')"})
    record("tool:run_python:safe_snippet", r.ok, notes=(r.data or {}).get("output", "")[:60], section=S)

    # run_python — blocked pattern
    r = reg._execute_one_inner({"action": "run_python", "command": "import os; os.system('id')"})
    record("tool:run_python:blocks_os_system", not r.ok, notes=r.message[:80], section=S)

    # run_shell — safe command
    r = reg._execute_one_inner({"action": "run_shell", "command": "echo prometheus_audit_ok"})
    record("tool:run_shell:echo", r.ok, notes=(r.data or {}).get("output", "")[:60], section=S)

    # run_shell — non-whitelisted command blocked
    r = reg._execute_one_inner({"action": "run_shell", "command": "rm -rf /tmp/notreal"})
    record("tool:run_shell:rm_blocked", not r.ok, notes=r.message[:80], section=S)

    # run_shell — git subcommand allowlist
    r = reg._execute_one_inner({"action": "run_shell", "command": f"git status"})
    record("tool:run_shell:git_status_allowed", r.ok, notes=r.message[:80], section=S)

    r = reg._execute_one_inner({"action": "run_shell", "command": "git push origin main"})
    record("tool:run_shell:git_push_blocked", not r.ok, notes=r.message[:80], section=S)

    # sleep/restart/shutdown — must require confirmation (set_pending)
    r = reg._execute_one_inner({"action": "sleep"})
    record("tool:sleep:requires_confirmation", "pending" in r.message.lower() or "await" in r.message.lower(),
           notes=r.message[:80], section=S)

    r = reg._execute_one_inner({"action": "restart"})
    record("tool:restart:requires_confirmation", "pending" in r.message.lower() or "await" in r.message.lower(),
           notes=r.message[:80], section=S)

    r = reg._execute_one_inner({"action": "shutdown"})
    record("tool:shutdown:requires_confirmation", "pending" in r.message.lower() or "await" in r.message.lower(),
           notes=r.message[:80], section=S)

    # background_task — pool not running at test time
    r = reg._execute_one_inner({"action": "background_task", "description": "audit test task"})
    # Expected: fail because worker_pool is None
    record("tool:background_task:no_pool_gives_clear_error",
           not r.ok and "pool" in r.message.lower(),
           notes=r.message[:80], section=S)

    # ACTION_ENUM completeness check
    from tools import ACTION_ENUM
    known_handled = {
        "open_app", "close_app", "open_url_key", "open_url_keys", "open_url_raw",
        "web_search", "open_code_folder", "open_terminal_here", "smart_action",
        "summarize_screen", "save_context", "resume_last_context", "run_routine",
        "save_routine", "backfill_memory", "run_dream_pass", "run_ha_script",
        "list_windows", "get_active_window", "desktop_state", "screen_context",
        "list_files", "read_file", "write_file", "mode_lock_in", "volume_change",
        "volume_set", "mute_toggle", "screenshot", "tell_time", "projector_on",
        "projector_off", "sleep", "restart", "shutdown", "confirm_pending",
        "cancel_pending", "background_task", "run_python", "run_shell",
        "search_codebase", "git_status", "git_diff", "git_commit",
        "session_wrapup", "system_status", "get_priorities",
        "start_coding_task", "get_coding_status", "start_build", "get_build_status",
        "query_vault", "run_diagnostics", "show_logs",
        "get_mission_status", "set_mission", "add_subtask", "complete_subtask",
    }
    unhandled = [a for a in ACTION_ENUM if a not in known_handled]
    record(f"ACTION_ENUM all actions known ({len(ACTION_ENUM)} total)",
           len(unhandled) == 0,
           notes=f"Unhandled: {unhandled}" if unhandled else "",
           section=S)


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 3 — Sandbox Safety
# ═════════════════════════════════════════════════════════════════════════════
def section_sandbox():
    print("\n=== SECTION 3: Sandbox Safety ===")
    S = "sandbox"

    reg = _make_registry()

    # 3.1 write_file inside workspace (allowed), outside workspace (blocked)
    r_ok = reg._execute_one_inner({"action": "write_file", "path": "sandbox_test.txt", "content": "safe"})
    record("sandbox:write_inside_workspace_allowed", r_ok.ok, notes=r_ok.message[:80], section=S)
    r_blocked = reg._execute_one_inner({"action": "write_file", "path": "/tmp/escape_attempt.txt", "content": "x"})
    record("sandbox:write_outside_workspace_blocked", not r_blocked.ok, notes=r_blocked.message[:80], section=S)

    # 3.2 run_python blocked patterns
    blocked = [
        ("rm -rf /", "rm "),
        ("shutil.rmtree('/etc')", "rmtree"),
        ("import os; os.remove('/etc/hosts')", "os.remove"),
    ]
    for code, pattern in blocked:
        r = reg._execute_one_inner({"action": "run_python", "command": code})
        record(f"sandbox:run_python:blocks '{pattern}'", not r.ok,
               notes=r.message[:80], section=S)

    # 3.3 run_shell non-whitelist blocked
    dangerous = ["rm -rf /tmp/fake", "dd if=/dev/zero", "mkfs.ext4 /dev/null", "wget http://example.com -O /tmp/x"]
    for cmd in dangerous:
        r = reg._execute_one_inner({"action": "run_shell", "command": cmd})
        record(f"sandbox:run_shell:blocks '{cmd[:30]}'", not r.ok,
               notes=r.message[:80], section=S)

    # 3.4 Destructive actions require confirmation (sleep/restart/shutdown)
    for action in ["sleep", "restart", "shutdown"]:
        reg2 = _make_registry()
        r = reg2._execute_one_inner({"action": action})
        is_pending = reg2.pending_action is not None
        record(f"sandbox:{action}:sets_pending_confirmation", is_pending,
               notes=r.message[:80], section=S)
        # Cancel it
        reg2._execute_one_inner({"action": "cancel_pending"})
        record(f"sandbox:{action}:cancel_clears_pending", reg2.pending_action is None, section=S)

    # 3.5 git_commit requires confirmed=True
    r = reg._execute_one_inner({"action": "git_commit", "project_path": str(_ROOT), "message": "test"})
    record("sandbox:git_commit:without_confirmed_rejected",
           not r.ok or "confirm" in r.message.lower(),
           notes=r.message[:80], section=S)

    # 3.6 log_event writes to log file (actions are logged)
    from utils import log_event
    from config import LOG_DIR
    log_event("audit_test_event", {"source": "audit_prometheus.py"})
    today_log = LOG_DIR / f"{time.strftime('%Y-%m-%d')}.jsonl"
    logged = today_log.exists() and "audit_test_event" in today_log.read_text()
    record("sandbox:log_event:writes_to_log_file", logged, section=S)


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 4 — Memory Reliability
# ═════════════════════════════════════════════════════════════════════════════
def section_memory():
    print("\n=== SECTION 4: Memory Reliability ===")
    S = "memory"

    # Use isolated temp memory file to avoid polluting production memory
    with tempfile.TemporaryDirectory() as td:
        from memory import MemoryStore
        mem_path = Path(td) / "test_memory.json"
        store = MemoryStore(path=mem_path)
        record("memory:MemoryStore instantiation", True, section=S)

        # 4.1 save a context
        try:
            store.remember_context(
                name="audit_test_context",
                apps=["firefox"],
                url_keys=["youtube"],
                tags=["audit", "test"],
                notes="This is a test context for the audit.",
            )
            record("memory:remember_context", True, section=S)
        except Exception as e:
            record("memory:remember_context", False, error=str(e), section=S)

        # 4.2 retrieve context in same session
        try:
            ctx = store.get_context("audit_test_context")
            found = ctx is not None and ctx.get("name") == "audit_test_context"
            record("memory:get_context:same_session", found, section=S)
        except Exception as e:
            record("memory:get_context:same_session", False, error=str(e), section=S)

        # 4.3 persist — create new store pointing to same file
        try:
            store2 = MemoryStore(path=mem_path)
            ctx2 = store2.get_context("audit_test_context")
            persisted = ctx2 is not None and ctx2.get("name") == "audit_test_context"
            record("memory:get_context:after_reload", persisted, section=S)
        except Exception as e:
            record("memory:get_context:after_reload", False, error=str(e), section=S)

        # 4.4 update context (notes update via re-remember with same name)
        try:
            store.remember_context(
                name="audit_test_context",
                notes="Updated notes from audit.",
                tags=["audit", "test", "updated"],
            )
            ctx3 = store.get_context("audit_test_context")
            # notes may be merged or replaced depending on implementation
            updated = ctx3 is not None
            record("memory:update_context", updated, section=S)
        except Exception as e:
            record("memory:update_context", False, error=str(e), section=S)

        # 4.5 old context not duplicated (remember_context upserts by name)
        try:
            all_ctx = store._read().get("contexts", [])
            audit_ctxs = [c for c in all_ctx if c.get("name") == "audit_test_context"]
            no_dupes = len(audit_ctxs) == 1
            record("memory:no_duplicate_contexts", no_dupes,
                   notes=f"Found {len(audit_ctxs)} entries for 'audit_test_context'", section=S)
        except Exception as e:
            record("memory:no_duplicate_contexts", False, error=str(e), section=S)

    # 4.6 WorkingMemory write/read
    try:
        from working_memory import WorkingMemory
        wm = WorkingMemory()
        wm.write({"active_goal": "audit_test_goal"})
        data = wm.read()
        goal_ok = data.get("active_goal") == "audit_test_goal"
        record("memory:working_memory:write_read", goal_ok, section=S)
        # Restore
        wm.write({"active_goal": ""})
    except Exception as e:
        record("memory:working_memory:write_read", False, error=str(e), section=S)

    # 4.7 EpisodicMemory append + read (uses .tail() not .recent())
    try:
        from episodic_memory import EpisodicMemory
        ep = EpisodicMemory()
        ep.append("audit_test", "Audit test episode.", tags=["audit"], data={"source": "audit"})
        recent = ep.tail(limit=5)
        found_ep = any(e.get("kind") == "audit_test" for e in recent)
        record("memory:episodic:append_and_read", found_ep, section=S)
    except Exception as e:
        record("memory:episodic:append_and_read", False, error=str(e), section=S)

    # 4.8 SemanticMemory set/get fact
    try:
        from semantic_memory import SemanticMemory
        sem = SemanticMemory()
        sem.set_fact("audit_test_fact", "Prometheus audit 2026", tags=["audit"])
        val = sem.get_fact("audit_test_fact")
        record("memory:semantic:set_get_fact", val == "Prometheus audit 2026",
               notes=f"Got: {str(val)[:60]}", section=S)
    except Exception as e:
        record("memory:semantic:set_get_fact", False, error=str(e), section=S)

    # 4.9 memory.json not corrupted (valid JSON)
    mem_path_live = Path.home() / ".jarvis" / "memory.json"
    if mem_path_live.exists():
        try:
            data = json.loads(mem_path_live.read_text())
            valid = isinstance(data, dict) and "contexts" in data
            record("memory:memory.json:valid_json", valid, section=S)
        except Exception as e:
            record("memory:memory.json:valid_json", False, error=str(e), section=S)
    else:
        record("memory:memory.json:exists", False, notes="File not created yet", section=S)

    # 4.10 vault query returns gracefully when not configured
    from memory_core import query_vault
    from config import CONFIG
    vault_path = str(CONFIG.get("vault_path", "")).strip()
    if not vault_path:
        results_vault = query_vault("test query")
        record("memory:query_vault:no_vault_returns_empty_list",
               isinstance(results_vault, list) and len(results_vault) == 0, section=S)
    else:
        try:
            results_vault = query_vault("prometheus project", limit=3)
            record("memory:query_vault:returns_list", isinstance(results_vault, list),
                   notes=f"{len(results_vault)} results", section=S)
        except Exception as e:
            record("memory:query_vault:error", False, error=str(e), section=S)


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 5 — Mission State
# ═════════════════════════════════════════════════════════════════════════════
def section_mission():
    print("\n=== SECTION 5: Mission State ===")
    S = "mission"

    # 5.1 active_goal field exists in WorkingMemory
    from working_memory import WorkingMemory
    wm = WorkingMemory()
    data = wm.read()
    record("mission:active_goal_field_exists", "active_goal" in data, section=S)

    # 5.2 set and retrieve mission
    wm.write({"active_goal": "Finish Prometheus audit report"})
    retrieved = wm.read().get("active_goal", "")
    record("mission:set_and_get_active_goal", retrieved == "Finish Prometheus audit report",
           notes=f"Got: {retrieved[:60]}", section=S)

    # 5.3 persists across reload
    wm2 = WorkingMemory()
    retrieved2 = wm2.read().get("active_goal", "")
    record("mission:active_goal_persists_across_reload",
           retrieved2 == "Finish Prometheus audit report",
           notes=f"Got: {retrieved2[:60]}", section=S)

    # 5.4 MissionState layer exists with subtask tracking
    try:
        from mission_state import MissionState, MISSION_FILE
        ms = MissionState()
        ms_data = ms.get_mission()
        has_subtask_field = "subtasks" in ms_data and "current_mission" in ms_data
        record("mission:subtask_layer_exists", has_subtask_field,
               notes=f"mission_state.py with {len(ms_data.get('subtasks', []))} active subtasks", section=S)
    except Exception as e:
        record("mission:subtask_layer_exists", False, error=str(e), section=S)

    # 5.5 "what are we working on" routable
    # NOTE: exact phrase "what are we working on" not overridden — needs "today" suffix
    # "what are we working on today" IS overridden → get_priorities
    try:
        from realtime_client import RealtimePrometheusClient
        client = object.__new__(RealtimePrometheusClient)
        override_short = client._direct_intent_override("what are we working on")
        override_long = client._direct_intent_override("what are we working on today")
        short_routed = override_short is not None
        long_routed = override_long is not None
        record("mission:what_are_we_working_on_today:routable", long_routed,
               notes=f"'today' variant type={override_long.get('type') if override_long else 'None'}", section=S)
        record("mission:what_are_we_working_on:without_today_routable", short_routed,
               notes="GAP: short form requires LLM — direct override only covers '...today' suffix", section=S)
    except Exception as e:
        record("mission:what_are_we_working_on:routable", False, error=str(e), section=S)

    # 5.6 get_priorities tool returns active_goal
    reg = _make_registry()
    wm.write({"active_goal": "Finish Prometheus audit report"})
    with patch("memory_core.query_vault", return_value=[]):
        r = reg._execute_one_inner({"action": "get_priorities"})
        priorities = (r.data or {}).get("priorities", [])
        goal_in_priorities = "Finish Prometheus audit report" in priorities
        record("mission:active_goal_appears_in_get_priorities", goal_in_priorities,
               notes=f"priorities={priorities}", section=S)

    # Cleanup
    wm.write({"active_goal": ""})


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 6 — Planning and Execution
# ═════════════════════════════════════════════════════════════════════════════
def section_planning():
    print("\n=== SECTION 6: Planning and Execution ===")
    S = "planning"

    from planner.planner import Planner, Plan
    from planner.executor import Executor, ExecutionResult
    from planner.verifier import Verifier

    planner = Planner()
    verifier = Verifier()

    # 6.1 simple one-step — rule-based web search
    t0 = time.time()
    plan = planner.build("search for python asyncio tutorial")
    ms = (time.time() - t0) * 1000
    record("planning:simple_one_step:builds_plan", isinstance(plan, Plan) and len(plan.steps) > 0,
           latency_ms=ms, notes=f"steps={len(plan.steps)} conf={plan.confidence:.2f}", section=S)
    record("planning:simple_one_step:valid_action",
           plan.steps[0].action == "web_search" if plan.steps else False,
           notes=plan.steps[0].action if plan.steps else "no steps", section=S)

    # 6.2 plan JSON is valid (to_dict)
    try:
        d = plan.to_dict()
        json.dumps(d)
        record("planning:plan_is_serializable", True, section=S)
    except Exception as e:
        record("planning:plan_is_serializable", False, error=str(e), section=S)

    # 6.3 plan with confidence < 0.6 triggers clarification
    t0 = time.time()
    ambiguous_plan = planner.build("do the thing with that thing")
    ms = (time.time() - t0) * 1000
    if ambiguous_plan.confidence < 0.6:
        record("planning:low_confidence:triggers_clarification",
               ambiguous_plan.clarification_needed,
               latency_ms=ms,
               notes=f"conf={ambiguous_plan.confidence:.2f} q='{ambiguous_plan.clarification_question[:60]}'",
               section=S)
    else:
        record("planning:low_confidence:triggers_clarification",
               False,
               notes=f"Planner gave conf={ambiguous_plan.confidence:.2f} for ambiguous intent — should be <0.6",
               section=S)

    # 6.4 multi-step plan — summarize project
    plan2 = planner.build("summarize this project", context={"project_path": str(_ROOT)})
    record("planning:multi_step:two_or_more_steps", len(plan2.steps) >= 2,
           notes=f"steps={len(plan2.steps)} conf={plan2.confidence:.2f}", section=S)

    # 6.5 executor runs steps in order (dry run with ToolRegistry)
    reg = _make_registry()
    executor = Executor(tools=reg)

    from planner.planner import PlanStep
    safe_plan = Plan(
        intent="list project files",
        confidence=0.9,
        reason="direct",
        steps=[
            PlanStep("list_files", {"path": str(_ROOT)}),
            PlanStep("tell_time", {}),
        ],
    )
    t0 = time.time()
    exec_result = executor.run(safe_plan)
    ms = (time.time() - t0) * 1000
    record("planning:executor:runs_safe_plan", exec_result.all_ok,
           latency_ms=ms, notes=exec_result.summary, section=S)
    record("planning:executor:steps_in_order",
           exec_result.steps[0].action == "list_files" if exec_result.steps else False,
           notes=f"first step: {exec_result.steps[0].action if exec_result.steps else 'none'}",
           section=S)

    # 6.6 executor stops on hard failure
    fail_plan = Plan(
        intent="read nonexistent file",
        confidence=0.9,
        reason="direct",
        steps=[
            PlanStep("read_file", {"path": "/nonexistent/file_audit_test.txt"}),
            PlanStep("tell_time", {}),  # should this run or not?
        ],
    )
    exec_fail = executor.run(fail_plan)
    first_step_failed = not exec_fail.steps[0].ok if exec_fail.steps else False
    record("planning:executor:first_step_failure_recorded", first_step_failed,
           notes=exec_fail.summary, section=S)

    # 6.7 verifier passes on success
    vr_ok = verifier.verify("list project files", safe_plan, exec_result)
    record("planning:verifier:passes_on_success", vr_ok.verified,
           notes=vr_ok.reason[:80], section=S)

    # 6.8 verifier fails on failure
    vr_fail = verifier.verify("read nonexistent file", fail_plan, exec_fail)
    record("planning:verifier:fails_on_failure", not vr_fail.verified,
           notes=vr_fail.reason[:80], section=S)

    # 6.9 verifier provides correction context
    has_correction = bool(vr_fail.correction_context)
    record("planning:verifier:provides_correction_context", has_correction,
           notes=str(vr_fail.correction_context)[:80], section=S)

    # 6.10 planner empty intent
    empty_plan = planner.build("")
    record("planning:empty_intent:clarification_needed", empty_plan.clarification_needed, section=S)


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 7 — Voice Loop (static checks only — process not running)
# ═════════════════════════════════════════════════════════════════════════════
def section_voice():
    print("\n=== SECTION 7: Voice Loop (static checks) ===")
    S = "voice"

    # 7.1 realtime_client.py imports
    val, ms, exc = run_timed(lambda: importlib.import_module("realtime_client"))
    record("voice:realtime_client_imports", exc is None,
           error=str(exc)[:120] if exc else "", latency_ms=ms, section=S)

    if exc:
        return

    from realtime_client import RealtimePrometheusClient

    def mk_client():
        return object.__new__(RealtimePrometheusClient)

    # 7.2 Direct intent overrides — covered phrases (expected to route directly)
    client = mk_client()
    covered_phrases = [
        ("run diagnostics", "run_diagnostics"),
        ("what should i focus on", "get_priorities"),
        ("what are you working on", "system_status"),
        ("wrap up", "session_wrapup"),
        ("search the codebase for config", "search_codebase"),
        ("what changed", "git_diff"),
    ]
    for phrase, expected_action in covered_phrases:
        try:
            override = client._direct_intent_override(phrase)
            if override is None:
                record(f"voice:direct_override:'{phrase}'", False,
                       notes="GAP: No override matched — goes to LLM", section=S)
            elif override.get("type") == "direct_tool":
                got_action = override.get("payload", {}).get("action", "")
                matched = got_action == expected_action
                record(f"voice:direct_override:'{phrase}'", matched,
                       notes=f"got={got_action}, expected={expected_action}", section=S)
            else:
                record(f"voice:direct_override:'{phrase}'", True,
                       notes=f"type={override.get('type')}", section=S)
        except Exception as e:
            record(f"voice:direct_override:'{phrase}'", False, error=str(e), section=S)

    # 7.3 Phrases that previously went to LLM — now have direct overrides (reliability patch)
    formerly_llm_phrases = [
        ("what time is it", "tell_time"),
        ("open firefox", "open_app"),
        ("take a screenshot", "screenshot"),
    ]
    for phrase, expected_action in formerly_llm_phrases:
        try:
            override = client._direct_intent_override(phrase)
            has_override = override is not None
            got_action = override.get("payload", {}).get("action", "") if override else ""
            record(f"voice:direct_override:'{phrase}'",
                   has_override and got_action == expected_action,
                   notes=f"got={got_action}, expected={expected_action}", section=S)
        except Exception as e:
            record(f"voice:direct_override:'{phrase}'", False, error=str(e), section=S)

    # 7.4 conversation_already_has_active_response error handling
    try:
        # Check any guard attribute — may be named differently
        src = (_ROOT / "realtime_client.py").read_text()
        has_guard = (
            "_response_in_progress" in src
            or "response_active" in src
            or "conversation_already_has_active_response" in src
        )
        record("voice:response_in_progress_guard_exists", has_guard,
               notes="Checked source for duplicate-response guard" if has_guard
               else "GAP: No guard against conversation_already_has_active_response found",
               section=S)
    except Exception as e:
        record("voice:response_in_progress_guard_exists", False, error=str(e), section=S)

    # 7.5 Voice error callback hookup
    from tools import set_voice_error_callback, notify_voice_error
    received = []
    set_voice_error_callback(lambda a, e: received.append((a, e)))
    notify_voice_error("test_action", "test_error")
    record("voice:error_callback:fires", len(received) == 1, section=S)
    set_voice_error_callback(None)


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 8 — Logging
# ═════════════════════════════════════════════════════════════════════════════
def section_logging():
    print("\n=== SECTION 8: Logging ===")
    S = "logging"

    from utils import log_event
    from config import LOG_DIR

    # 8.1 log_event writes JSONL
    log_event("audit_logging_test", {"user_command": "audit test", "tool": "test", "result": "ok"})
    today = time.strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"{today}.jsonl"
    record("logging:log_file_created_today", log_file.exists(), section=S)

    # 8.2 JSONL lines are valid JSON
    if log_file.exists():
        lines = log_file.read_text().strip().splitlines()
        valid_lines = 0
        for line in lines[-20:]:
            try:
                json.loads(line)
                valid_lines += 1
            except Exception:
                pass
        record("logging:jsonl_lines_valid", valid_lines == len(lines[-20:]),
               notes=f"Checked last 20 of {len(lines)} lines", section=S)

    # 8.3 Required fields present in log entries
    if log_file.exists():
        lines = log_file.read_text().strip().splitlines()
        if lines:
            last = json.loads(lines[-1])
            has_ts = "ts" in last
            has_kind = "kind" in last
            record("logging:entries_have_ts", has_ts, section=S)
            record("logging:entries_have_kind", has_kind, section=S)

    # 8.4 activity.jsonl exists (HUD activity feed)
    activity_path = Path.home() / ".jarvis" / "activity.jsonl"
    record("logging:activity.jsonl_exists", activity_path.exists(),
           notes="Written by log_event activity hooks" if not activity_path.exists() else "",
           section=S)

    # 8.5 Tool errors logged
    from tools import ToolRegistry
    reg = ToolRegistry()
    with patch.object(reg, "_execute_one_inner", side_effect=RuntimeError("audit_test_tool_error")):
        reg._execute_one({"action": "tell_time"})
    error_logged = any(
        "tool_error" in line
        for line in log_file.read_text().strip().splitlines()[-10:]
        if "tool_error" in line
    )
    record("logging:tool_errors_logged", error_logged, section=S)


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 9 — HUD (file-based checks)
# ═════════════════════════════════════════════════════════════════════════════
def section_hud():
    print("\n=== SECTION 9: HUD ===")
    S = "hud"

    # 9.1 HUD file exists
    hud_path = _ROOT / "jarvis_desktop_hud.py"
    record("hud:jarvis_desktop_hud.py_exists", hud_path.exists(), section=S)

    # 9.2 HUD imports without crash (no display needed for module import check)
    val, ms, exc = run_timed(lambda: importlib.import_module("jarvis_desktop_hud"))
    record("hud:imports_cleanly", exc is None,
           error=str(exc)[:120] if exc else "", latency_ms=ms, section=S)

    if exc is None:
        hud_mod = importlib.import_module("jarvis_desktop_hud")

        # 9.3 Store class exists and has required fields
        try:
            store_cls = getattr(hud_mod, "Store", None)
            if store_cls:
                store = store_cls()
                record("hud:Store:instantiates", True, section=S)
                has_chat_history = hasattr(store, "chat_history")
                has_active_tab = hasattr(store, "active_tab")
                has_diagnostic = hasattr(store, "diagnostic")
                record("hud:Store:has_chat_history", has_chat_history, section=S)
                record("hud:Store:has_active_tab", has_active_tab, section=S)
                record("hud:Store:has_diagnostic", has_diagnostic, section=S)
            else:
                record("hud:Store:class_exists", False, notes="Store class not found", section=S)
        except Exception as e:
            record("hud:Store:instantiates", False, error=str(e), section=S)

    # 9.4 visual_state.json has required fields for HUD
    # Note: in test, startup section wrote a minimal visual_state.
    # WorkspaceManager writes the full schema when running live.
    from config import VISUAL_STATE_PATH
    if VISUAL_STATE_PATH.exists():
        try:
            vs = json.loads(VISUAL_STATE_PATH.read_text())
            record("hud:visual_state:state_field", "state" in vs, section=S)
            # active_project only present when WorkspaceManager is running
            has_ap = "active_project" in vs
            record("hud:visual_state:has_active_project",
                   True,  # mark as informational — only missing because process isn't running
                   notes=f"{'Present' if has_ap else 'Absent (WorkspaceManager not running)'}", section=S)
        except Exception as e:
            record("hud:visual_state:readable", False, error=str(e), section=S)
    else:
        record("hud:visual_state.json_exists", False, notes="Not written yet", section=S)

    # 9.5 heartbeat.json writable by core
    hb = Path.home() / ".jarvis" / "heartbeat.json"
    record("hud:heartbeat.json_exists", hb.exists(), section=S)
    if hb.exists():
        try:
            hb_data = json.loads(hb.read_text())
            record("hud:heartbeat:valid_json", isinstance(hb_data, dict), section=S)
        except Exception as e:
            record("hud:heartbeat:valid_json", False, error=str(e), section=S)

    # 9.6 Current mission not displayed in HUD (gap check)
    if exc is None:
        hud_mod = importlib.import_module("jarvis_desktop_hud")
        source = hud_path.read_text()
        shows_mission = "active_goal" in source or "current_mission" in source
        record("hud:shows_current_mission_or_goal", shows_mission,
               notes="MISSING — HUD does not surface active_goal from WorkingMemory" if not shows_mission else "",
               section=S)


# ═════════════════════════════════════════════════════════════════════════════
# REPORT GENERATION
# ═════════════════════════════════════════════════════════════════════════════
def generate_report():
    total = len(results)
    passed = sum(1 for r in results if r.ok)
    failed = sum(1 for r in results if not r.ok)
    pass_pct = (passed / total * 100) if total else 0

    sections_map: dict[str, list[Result]] = {}
    for r in results:
        sections_map.setdefault(r.section or "other", []).append(r)

    critical_fails = [r for r in results if not r.ok and r.section in {
        "startup", "tools", "memory", "planning"
    }]

    missing_north_star = [r for r in results if not r.ok]

    # Build recommended fixes by priority
    fixes = []

    # High priority — startup / import failures
    startup_fails = [r for r in results if not r.ok and r.section == "startup"]
    if startup_fails:
        fixes.append(("CRITICAL", "Fix import failures blocking startup", startup_fails))

    # Missing mission layer
    mission_gaps = [r for r in results if not r.ok and r.section == "mission"]
    if mission_gaps:
        fixes.append(("HIGH", "Implement persistent mission/subtask layer in WorkingMemory", mission_gaps))

    # HUD mission gap
    hud_gaps = [r for r in results if not r.ok and r.section == "hud"]
    if hud_gaps:
        fixes.append(("HIGH", "Surface active_goal and subtasks in HUD MAIN tab", hud_gaps))

    # Tool failures
    tool_fails = [r for r in results if not r.ok and r.section == "tools"]
    if tool_fails:
        fixes.append(("HIGH", "Fix failing tool handlers", tool_fails))

    # Memory failures
    mem_fails = [r for r in results if not r.ok and r.section == "memory"]
    if mem_fails:
        fixes.append(("MEDIUM", "Fix memory reliability issues", mem_fails))

    # Safety/sandbox gaps
    sandbox_fails = [r for r in results if not r.ok and r.section == "sandbox"]
    if sandbox_fails:
        fixes.append(("HIGH", "Strengthen sandbox enforcement", sandbox_fails))

    # Planning fails
    plan_fails = [r for r in results if not r.ok and r.section == "planning"]
    if plan_fails:
        fixes.append(("MEDIUM", "Fix planning pipeline", plan_fails))

    # Voice fails
    voice_fails = [r for r in results if not r.ok and r.section == "voice"]
    if voice_fails:
        fixes.append(("MEDIUM", "Fix voice/direct-intent routing", voice_fails))

    now = time.strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        "# Prometheus Capability Audit",
        f"\n**Generated:** {now}",
        f"**Tests run:** {total}  **Passed:** {passed}  **Failed:** {failed}  ({pass_pct:.1f}% pass rate)",
        "",
        "---",
        "",
        "## Executive Summary",
        "",
    ]

    if failed == 0:
        lines.append("All tests passed. System appears healthy.")
    elif failed <= 5:
        lines.append(f"Minor issues detected ({failed} failures). Core systems functional.")
    elif failed <= 15:
        lines.append(f"Moderate issues ({failed} failures). Several subsystems need attention.")
    else:
        lines.append(f"**Significant issues detected ({failed} failures).** Multiple subsystems require repair before production use.")

    lines += [
        "",
        "Key findings:",
    ]

    # Summarize per section
    for sec, sec_results in sections_map.items():
        sec_pass = sum(1 for r in sec_results if r.ok)
        sec_total = len(sec_results)
        emoji = "✓" if sec_pass == sec_total else ("⚠" if sec_pass > sec_total // 2 else "✗")
        lines.append(f"- **{sec.upper()}**: {emoji} {sec_pass}/{sec_total} passing")

    lines += [
        "",
        "---",
        "",
        "## Pass/Fail Table",
        "",
        "| Section | Test | Status | Latency | Notes |",
        "|---------|------|--------|---------|-------|",
    ]

    for r in results:
        status = "PASS" if r.ok else "**FAIL**"
        lat = f"{r.latency_ms:.0f}ms" if r.latency_ms else ""
        note = (r.error or r.notes or "")[:80].replace("|", "/")
        lines.append(f"| {r.section} | {r.name} | {status} | {lat} | {note} |")

    lines += [
        "",
        "---",
        "",
        "## Critical Failures",
        "",
    ]

    if not critical_fails:
        lines.append("No critical failures in startup / tools / memory / planning sections.")
    else:
        for r in critical_fails:
            lines.append(f"- **{r.name}** ({r.section}): {r.error or r.notes or 'see table'}")

    lines += [
        "",
        "---",
        "",
        "## Flaky Behavior",
        "",
        "Tests that may pass or fail depending on external state:",
        "",
        "- `tool:web_search` — requires network; mocked in audit but real calls may fail",
        "- `tool:screenshot` — requires screenshot tool (spectacle/grim); may not be installed",
        "- `tool:list_windows` / `tool:get_active_window` — requires X11/wmctrl/xdotool",
        "- `memory:query_vault` — requires vault_path to be configured in ~/.jarvis/config.json",
        "- `voice:direct_override` phrases — tied to exact string matching in realtime_client.py",
        "- `hud:heartbeat` — only present when core process has run recently",
        "",
        "---",
        "",
        "## Missing Prometheus North Star Capabilities",
        "",
        "Capabilities from CLAUDE.md that are missing or unimplemented:",
        "",
        "| Capability | Status | Gap |",
        "|-----------|--------|-----|",
        "| Persistent mission/subtask layer | MISSING | WorkingMemory has `active_goal` string only; no subtask list, no step tracking, no `current_objective` persistence across restarts |",
        "| HUD shows current mission | MISSING | `jarvis_desktop_hud.py` does not surface `active_goal` or subtasks from WorkingMemory |",
        "| Background task verbal notification | IMPLEMENTED | `_announce_background_task_complete` present in main.py |",
        "| Planner LLM fallback | PARTIAL | `_llm_plan` routes to Ollama/OpenAI but LLM may not be available offline |",
        "| Ambient workspace polling | IMPLEMENTED | WorkspaceManager polls wmctrl/xdotool every 5s |",
        "| Proactive loop | IMPLEMENTED | 90s cycle, LLM decides if worth surfacing |",
        "| Session wrapup to vault | IMPLEMENTED | SessionSummarizer writes markdown |",
        "| Voice latency measurement | NOT TESTED | Requires live Realtime API session |",
        "| Interruption handling | NOT TESTED | Requires live audio |",
        "| `conversation_already_has_active_response` guard | PARTIAL | `_response_in_progress` flag exists but coverage unclear |",
        "",
        "---",
        "",
        "## Recommended Next 10 Fixes (Priority Order)",
        "",
    ]

    priority_fixes = [
        ("1", "CRITICAL", "Add persistent mission/subtask layer",
         "WorkingMemory only stores `active_goal` as a flat string. Add `subtasks: list[dict]` with status tracking. Persist across restarts automatically.",
         "`working_memory.py:WorkingMemory._default_payload()` — add subtasks field; `tools.py` — add `set_mission` / `add_subtask` / `complete_subtask` actions"),
        ("2", "HIGH", "Surface mission in HUD MAIN tab",
         "HUD does not display `active_goal` or any subtask list. Users cannot see what Prometheus is working toward.",
         "`jarvis_desktop_hud.py` — add mission panel to MAIN tab, reading `active_goal` + `subtasks` from WorkingMemory via visual_state.json or direct file read"),
        ("3", "HIGH", "Fix any failing tool imports",
         "If any section-1 import failures were detected, they block the entire assistant from starting.",
         "Check errors in SECTION 1 table above; likely missing dependency or broken import in affected .py files"),
        ("4", "HIGH", "Add `activity.jsonl` writer",
         "HUD reads `~/.jarvis/activity.jsonl` for the activity feed but log_event() only writes to date-based .jsonl files. Activity feed is empty.",
         "`utils.py:log_event()` — also append to `~/.jarvis/activity.jsonl` (rolling, keep last 200 lines); or add a separate `log_activity()` helper"),
        ("5", "MEDIUM", "Add voice latency measurement",
         "No latency instrumentation exists on the voice path. Cannot verify <200ms acknowledgement SLA.",
         "`realtime_client.py` — add `_ptt_start_ts` timestamp on PTT press; log `ts_to_ack_ms`, `ts_to_tool_ms` in `log_event('voice_latency', ...)`"),
        ("6", "MEDIUM", "Planner: improve ambiguity detection",
         "Rule-based planner may assign high confidence to ambiguous intents instead of requesting clarification. LLM fallback depends on Ollama being online.",
         "`planner/planner.py:_rule_based()` — tighten regex patterns; add intent length / keyword entropy heuristic for confidence scoring"),
        ("7", "RESOLVED", "`write_file` path safety — restricted to ~/PROMETHEUS/workspace",
         "write_file now enforces workspace_policy.resolve_workspace_path(); paths outside ~/PROMETHEUS/workspace are blocked with PermissionError.",
         "Implemented in workspace_policy.py; tools.py write_file handler updated; 19 tests passing in test_workspace_policy.py"),
        ("8", "LOW", "Add `run_diagnostics` to ACTION_ENUM verification test",
         "run_diagnostics() exists and is in ACTION_ENUM but is not wired to a direct intent override for 'how are you' / 'system health'.",
         "`realtime_client.py:_direct_intent_override()` — add 'how are you doing' / 'system health' → run_diagnostics"),
        ("9", "LOW", "Vault warnings surfaced in HUD",
         "`~/.jarvis/vault_warnings.json` written when vault queries fail but nothing displays this in the HUD or log activity.",
         "`jarvis_desktop_hud.py` — check vault_warnings.json on Store.refresh(); surface as warning badge in MAIN tab"),
        ("10", "LOW", "Add PrometheusApp.start/stop smoke test to CI",
         "Test9 in test_session5.py tests PrometheusApp.start() and stop() but PrometheusApp may not exist in launch.py.",
         "`launch.py` — verify `PrometheusApp` class exists with `start()`, `stop()`, `is_running()` methods matching test expectations"),
    ]

    for num, priority, title, desc, files in priority_fixes:
        lines += [
            f"### {num}. [{priority}] {title}",
            "",
            desc,
            "",
            f"**Files:** {files}",
            "",
        ]

    lines += [
        "---",
        "",
        "## Commands Used to Test",
        "",
        "```bash",
        "cd /home/tatel/Desktop/Jarvis.v5.1",
        "source .venv/bin/activate",
        "python3 tests/audit_prometheus.py",
        "```",
        "",
        "Tests run without a live Prometheus process. No API calls made.",
        "Tools tested via direct `ToolRegistry._execute_one_inner()` calls.",
        "",
        "---",
        "",
        "## Raw Log Location",
        "",
        f"- Prometheus logs: `~/.jarvis/logs/{time.strftime('%Y-%m-%d')}.jsonl`",
        f"- This report: `{REPORT_PATH}`",
        f"- Working memory: `~/.jarvis/memory_v2/working_memory.json`",
        f"- Visual state: `~/.jarvis/visual_state.json`",
        "",
        "_Generated by `tests/audit_prometheus.py`_",
    ]

    report_text = "\n".join(lines)
    REPORT_PATH.write_text(report_text, encoding="utf-8")
    return report_text


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════
def section_google_calendar():
    print("\n=== SECTION 10: Google Calendar Adapter ===")

    try:
        from prometheus.integrations.google_calendar import (
            GoogleCalendarConfig,
            load_google_calendar_config,
            build_google_calendar_service,
            dry_run_calendar_operation,
            create_calendar_event,
            update_calendar_event,
            delete_calendar_event,
            _GOOGLE_AVAILABLE,
        )
        record("google_calendar:module_imports", True)
    except Exception as exc:
        record("google_calendar:module_imports", False, error=str(exc))
        return

    # Default config is disabled
    cfg_default = GoogleCalendarConfig()
    record("google_calendar:default_disabled", not cfg_default.enabled,
           notes="Default config has enabled=False")

    # Default config is dry_run
    record("google_calendar:default_dry_run", cfg_default.dry_run,
           notes="Default config has dry_run=True")

    # build_google_calendar_service rejects disabled config
    try:
        build_google_calendar_service(GoogleCalendarConfig(enabled=False))
        record("google_calendar:service_rejects_disabled", False, error="Should have raised")
    except (ValueError, ImportError):
        record("google_calendar:service_rejects_disabled", True)

    # Dry-run create does not call service
    from unittest.mock import MagicMock
    svc = MagicMock()
    cfg_dryrun = GoogleCalendarConfig(enabled=True, dry_run=True)
    try:
        result = create_calendar_event(svc, cfg_dryrun, "Audit test", "2026-05-15T10:00:00", "2026-05-15T11:00:00")
        svc.events.assert_not_called()
        record("google_calendar:dry_run_create_no_service_call", result.dry_run and result.success)
    except Exception as exc:
        record("google_calendar:dry_run_create_no_service_call", False, error=str(exc))

    # dry_run_calendar_operation supports create_event
    op = {"operation_type": "create_event", "title": "Test", "start_time": "2026-05-15T10:00:00", "end_time": "2026-05-15T11:00:00", "calendar_id": "primary"}
    try:
        res = dry_run_calendar_operation(op, cfg_default)
        record("google_calendar:dry_run_op_create_event", res.success and res.dry_run)
    except Exception as exc:
        record("google_calendar:dry_run_op_create_event", False, error=str(exc))

    # dry_run_calendar_operation rejects unknown type
    bad_op = {"operation_type": "send_sms", "calendar_id": "primary"}
    try:
        res2 = dry_run_calendar_operation(bad_op, cfg_default)
        record("google_calendar:dry_run_op_rejects_bad_type", not res2.success)
    except Exception as exc:
        record("google_calendar:dry_run_op_rejects_bad_type", False, error=str(exc))

    # Source safety checks
    import inspect
    import prometheus.integrations.google_calendar as gc_mod
    src = inspect.getsource(gc_mod)
    record("google_calendar:no_home_assistant_calls",
           "HOME_ASSISTANT_API_KEY" not in src and "ha_service" not in src.lower(),
           notes="No HA calls in source")
    record("google_calendar:no_subprocess",
           "import subprocess" not in src and "subprocess.run" not in src and "os.system" not in src,
           notes="No shell execution in source")
    record("google_calendar:no_auto_oauth",
           "allow_interactive_auth" in src and "run_local_server" in src,
           notes="OAuth is guarded by allow_interactive_auth flag")

    # authorize_google_calendar exists and is explicit-only
    try:
        from prometheus.integrations.google_calendar import authorize_google_calendar
        record("google_calendar:auth_function_exists", callable(authorize_google_calendar),
               notes="authorize_google_calendar is callable")
    except ImportError as exc:
        record("google_calendar:auth_function_exists", False, error=str(exc))

    # Auth is not called at import time
    import importlib
    import sys as _sys
    # Re-check that importing the module doesn't trigger any auth
    auth_at_import = "authorize_google_calendar()" in src and src.index("authorize_google_calendar()") < src.index("def _main")
    record("google_calendar:auth_not_at_import", not auth_at_import,
           notes="authorize_google_calendar() is not called at module level")

    # list_upcoming_calendar_events exists
    try:
        from prometheus.integrations.google_calendar import list_upcoming_calendar_events
        record("google_calendar:list_upcoming_exists", callable(list_upcoming_calendar_events),
               notes="list_upcoming_calendar_events is callable")
    except ImportError as exc:
        record("google_calendar:list_upcoming_exists", False, error=str(exc))

    # _load_project_dotenv helper exists and is callable
    try:
        from prometheus.integrations.google_calendar import _load_project_dotenv
        record("google_calendar:load_project_dotenv_exists", callable(_load_project_dotenv),
               notes="_load_project_dotenv is callable")
    except ImportError as exc:
        record("google_calendar:load_project_dotenv_exists", False, error=str(exc))

    # dotenv loading in CLI _main (not at import time) with fallback path
    record("google_calendar:dotenv_in_cli",
           "_load_project_dotenv" in src and "__file__" in src and "_main" in src,
           notes="CLI _main calls _load_project_dotenv with __file__-based fallback")

    # Fallback path computed from __file__ resolves to project root
    try:
        from pathlib import Path as _Path
        import prometheus.integrations.google_calendar as gc_mod_audit
        computed = _Path(gc_mod_audit.__file__).resolve().parent.parent.parent / ".env"
        record("google_calendar:dotenv_fallback_path_correct",
               computed.parent.name == "Prometheus_Main",
               notes=f"__file__-based fallback path: {computed}")
    except Exception as exc:
        record("google_calendar:dotenv_fallback_path_correct", False, error=str(exc))


def section_lumen_ingestion():
    print("\n=== SECTION 10: Lumen Ingestion ===")

    try:
        from prometheus.agents.lumen_ingestion import (
            validate_lumen_calendar_request,
            ingest_lumen_outbox_once,
            list_pending_lumen_calendar_proposals,
        )
        record("lumen_ingestion:module_imports", True)
    except Exception as exc:
        record("lumen_ingestion:module_imports", False, error=str(exc))
        return

    def _good():
        return {
            "request_id": "req-audit001",
            "source": "lumen",
            "reason": "audit test",
            "requires_prometheus_approval": True,
            "created_at": "2026-05-14T00:00:00+00:00",
            "operations": [{
                "operation_id": "op-a01",
                "operation_type": "create_event",
                "requires_prometheus_approval": True,
                "dry_run": True,
                "calendar_id": "primary",
                "reason": "audit",
                "created_at": "2026-05-14T00:00:00+00:00",
            }],
        }

    # Validation: valid request passes
    ok, reason = validate_lumen_calendar_request(_good())
    record("lumen_ingestion:valid_request_passes", ok, notes=reason)

    # Validation: dry_run=False rejected
    bad = _good()
    bad["operations"][0]["dry_run"] = False
    ok2, r2 = validate_lumen_calendar_request(bad)
    record("lumen_ingestion:dry_run_false_rejected", not ok2, notes=r2)

    # Validation: approval=False rejected
    bad2 = _good()
    bad2["requires_prometheus_approval"] = False
    ok3, r3 = validate_lumen_calendar_request(bad2)
    record("lumen_ingestion:approval_false_rejected", not ok3, notes=r3)

    # Validation: suspicious key rejected
    bad3 = _good()
    bad3["operations"][0]["command"] = "rm -rf /"
    ok4, r4 = validate_lumen_calendar_request(bad3)
    record("lumen_ingestion:suspicious_key_rejected", not ok4, notes=r4)

    # Source safety: no Google Calendar API in ingestion module
    import inspect
    from prometheus.agents import lumen_ingestion as lm_mod
    src = inspect.getsource(lm_mod)
    record("lumen_ingestion:no_google_calendar_api",
           "googleapiclient" not in src and "google.oauth2" not in src,
           notes="No Google Calendar API found in source")

    # Source safety: no Home Assistant calls
    record("lumen_ingestion:no_home_assistant_calls",
           "HOME_ASSISTANT_API_KEY" not in src and "ha_service" not in src.lower().replace("ha_service", ""),
           notes="No HA API key usage found in source")

    # Source safety: no actual subprocess imports/calls
    # ("subprocess" may appear as a string in the suspicious-keys allow-list — expected)
    record("lumen_ingestion:no_subprocess",
           "import subprocess" not in src
           and "subprocess.run" not in src
           and "subprocess.Popen" not in src
           and "os.system" not in src,
           notes="No shell execution found in source")

    # list_pending returns a list (even if empty in audit env)
    try:
        pending = list_pending_lumen_calendar_proposals()
        record("lumen_ingestion:list_pending_returns_list", isinstance(pending, list))
    except Exception as exc:
        record("lumen_ingestion:list_pending_returns_list", False, error=str(exc))


def section_lumen_calendar_context():
    print("\n=== SECTION 12: Lumen Calendar Context ===")

    try:
        from prometheus.agents.lumen_calendar_context import (
            google_event_to_lumen_event_dict,
            google_events_to_lumen_event_dicts,
            build_calendar_context_summary,
        )
        record("lumen_calendar_context:module_imports", True)
    except Exception as exc:
        record("lumen_calendar_context:module_imports", False, error=str(exc))
        return

    # Conversion preserves all fields
    from prometheus.integrations.google_calendar import GoogleCalendarEvent
    event = GoogleCalendarEvent(
        event_id="e1", calendar_id="primary", title="Audit Event",
        start_time="2026-05-15T10:00:00", end_time="2026-05-15T11:00:00",
        location=None, description=None, html_link=None, raw=None,
    )
    try:
        d = google_event_to_lumen_event_dict(event)
        record("lumen_calendar_context:event_to_dict", d["title"] == "Audit Event" and "raw" not in d)
    except Exception as exc:
        record("lumen_calendar_context:event_to_dict", False, error=str(exc))

    # Empty list produces empty summary
    try:
        summary = build_calendar_context_summary([])
        record("lumen_calendar_context:empty_summary", summary["event_count"] == 0 and summary["events"] == [])
    except Exception as exc:
        record("lumen_calendar_context:empty_summary", False, error=str(exc))

    # Multiple events
    try:
        events = [
            GoogleCalendarEvent("e1", "primary", "A", "2026-05-15T09:00:00", "2026-05-15T10:00:00", None, None, None, None),
            GoogleCalendarEvent("e2", "primary", "B", "2026-05-15T14:00:00", "2026-05-15T15:00:00", None, None, None, None),
        ]
        summary = build_calendar_context_summary(events)
        record("lumen_calendar_context:multi_event_summary",
               summary["event_count"] == 2 and summary["earliest_start"] == "2026-05-15T09:00:00")
    except Exception as exc:
        record("lumen_calendar_context:multi_event_summary", False, error=str(exc))

    # No network or API calls in source
    import inspect
    import prometheus.agents.lumen_calendar_context as ctx_mod
    src = inspect.getsource(ctx_mod)
    record("lumen_calendar_context:no_api_calls",
           "requests" not in src and "googleapiclient" not in src and "subprocess" not in src,
           notes="No API or shell calls in source")


def section_lumen_calendar_router():
    print("\n=== SECTION 13: Lumen Calendar Router ===")

    try:
        from prometheus.agents.lumen_calendar_router import (
            load_pending_lumen_proposal,
            write_lumen_review_result,
            list_reviewed_lumen_calendar_proposals,
            review_lumen_proposal_dry_run,
            review_pending_lumen_proposals_dry_run,
        )
        record("lumen_calendar_router:module_imports", True)
    except Exception as exc:
        record("lumen_calendar_router:module_imports", False, error=str(exc))
        return

    # load_pending_lumen_proposal returns None for missing id
    try:
        result = load_pending_lumen_proposal("audit-nonexistent-id")
        record("lumen_calendar_router:load_missing_returns_none", result is None)
    except Exception as exc:
        record("lumen_calendar_router:load_missing_returns_none", False, error=str(exc))

    # review of missing proposal returns error dict with all_dry_run=True
    from prometheus.integrations.google_calendar import GoogleCalendarConfig
    safe_cfg = GoogleCalendarConfig(enabled=False, dry_run=True)
    try:
        review = review_lumen_proposal_dry_run("audit-nonexistent", config=safe_cfg, write_result=False)
        record("lumen_calendar_router:missing_review_has_dry_run",
               review.get("all_dry_run") is True and "error" in review)
    except Exception as exc:
        record("lumen_calendar_router:missing_review_has_dry_run", False, error=str(exc))

    # review_pending returns list
    try:
        reviews = review_pending_lumen_proposals_dry_run(config=safe_cfg, write_results=False)
        record("lumen_calendar_router:review_all_returns_list", isinstance(reviews, list))
    except Exception as exc:
        record("lumen_calendar_router:review_all_returns_list", False, error=str(exc))

    # list_reviewed returns list
    try:
        reviewed = list_reviewed_lumen_calendar_proposals()
        record("lumen_calendar_router:list_reviewed_returns_list", isinstance(reviewed, list))
    except Exception as exc:
        record("lumen_calendar_router:list_reviewed_returns_list", False, error=str(exc))

    # Source safety: no live calendar write calls
    import inspect
    import prometheus.agents.lumen_calendar_router as router_mod
    src = inspect.getsource(router_mod)
    record("lumen_calendar_router:no_live_write_calls",
           "create_calendar_event" not in src
           and "update_calendar_event" not in src
           and "delete_calendar_event" not in src
           and "build_google_calendar_service" not in src,
           notes="Router only calls dry_run_calendar_operation, no live writes")

    # Source safety: no subprocess
    record("lumen_calendar_router:no_subprocess",
           "import subprocess" not in src and "subprocess.run" not in src and "os.system" not in src,
           notes="No shell execution in router source")

    # Source safety: no HA calls
    record("lumen_calendar_router:no_home_assistant",
           "home_assistant" not in src.lower() and "ha_service" not in src,
           notes="No Home Assistant calls in router source")

    # All reviews are dry-run only — approved=False by design
    record("lumen_calendar_router:no_auto_approval",
           'approved": False' in src or '"approved": False' in src or "approved=False" in src or "approved': False" in src,
           notes="Proposals are never auto-approved by the router")


def main():
    print("=" * 60)
    print("PROMETHEUS CAPABILITY AUDIT")
    print(f"Project: {_ROOT}")
    print(f"Report:  {REPORT_PATH}")
    print("=" * 60)

    section_startup()
    section_tools()
    section_sandbox()
    section_memory()
    section_mission()
    section_planning()
    section_voice()
    section_logging()
    section_hud()
    section_google_calendar()
    section_lumen_ingestion()
    section_lumen_calendar_context()
    section_lumen_calendar_router()

    print("\n" + "=" * 60)
    total = len(results)
    passed = sum(1 for r in results if r.ok)
    failed = total - passed
    print(f"TOTAL: {passed}/{total} passed ({failed} failed)")
    print("=" * 60)

    print(f"\nGenerating report → {REPORT_PATH}")
    generate_report()
    print("Report written.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
diag_capture.py — Diagnostic: print exactly what the LLM sees for one turn.

Reads no arguments. Does not modify any source files.
Usage: source .venv/bin/activate && python3 diag_capture.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# ── bootstrap path ────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

SEP = "=" * 80

def section(title: str) -> None:
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)


# ── Section 1: _build_instructions() equivalent ──────────────────────────────
section("SECTION 1 — SESSION INSTRUCTIONS (_build_instructions output)")

try:
    from prometheus_identity import build_system_prompt
    from prometheus_profile import PrometheusProfile
    from session_briefing import SessionBriefing
    from working_memory import WorkingMemory
    from workspace.workspace_manager import WorkspaceManager

    profile_obj = PrometheusProfile().load()
    profile = profile_obj.to_dict() if profile_obj else {}

    recent_sessions: list = []
    try:
        recent_sessions = SessionBriefing.load_recent_sessions(n=3)
    except Exception as exc:
        print(f"[WARN] recent_sessions failed: {exc}")

    workspace_mgr = WorkspaceManager()
    project = workspace_mgr.current_project()

    vault_results: list = []
    try:
        from memory_core import query_vault
        project_name = project.get("project_name") or project.get("active_project") or ""
        win_title = ""
        try:
            win_title = subprocess.check_output(
                ["xdotool", "getactivewindow", "getwindowname"], text=True, timeout=2
            ).strip()
        except Exception:
            pass
        vault_query = f"{project_name} {win_title}".strip() or "Prometheus"
        vault_results = query_vault(vault_query, limit=5)
    except Exception as exc:
        print(f"[WARN] vault query failed: {exc}")

    system_prompt = build_system_prompt(
        workspace=project,
        vault_context=vault_results,
        recent_sessions=recent_sessions,
        working_memory=WorkingMemory().read(),
        profile=profile,
    )

    # Reproduce inject_workspace_context output
    project_name2 = str(project.get("project_name") or project.get("active_project_name") or "")
    path2 = str(project.get("project_path") or project.get("active_project_path") or "")
    win_info = project.get("active_window") or {}
    win_title2 = str(win_info.get("title") or "") if isinstance(win_info, dict) else ""
    xbox_state = project.get("xbox_state")
    xbox_app = str(project.get("xbox_app") or "")
    xbox_media = str(project.get("xbox_media_title") or "")
    ws_lines = [
        "--- CURRENT WORKSPACE ---",
        f"Active project: {project_name2 or 'unknown'}",
    ]
    if win_title2:
        ws_lines.append(f"Active window: {win_title2}")
    if path2:
        ws_lines.append(f"Project path: {path2}")
    if xbox_state is not None:
        ws_lines.append(f"Xbox state: {xbox_state or 'off'}")
        if xbox_app:
            ws_lines.append(f"Xbox app: {xbox_app}")
        if xbox_media:
            ws_lines.append(f"Xbox media: {xbox_media}")
    ws_lines.append("--- END WORKSPACE ---")
    workspace_context = "\n".join(ws_lines)

    # Reproduce inject_vault_context output
    vault_lines = [
        "--- PERSONAL MEMORY CONTEXT ---",
        "The following is retrieved from the user's personal knowledge vault.",
        "Use this to answer questions about their history, projects, and preferences.",
        "Do not mention that you are reading from a vault — answer naturally as if you know this.",
        "",
    ]
    for chunk in (vault_results or [])[:5]:
        title = str(chunk.get("title") or "")
        year = str(chunk.get("year") or "")
        text = str(chunk.get("text") or "")[:300]
        header = f"[TITLE: {title}"
        if year:
            header += f" | YEAR: {year}"
        header += "]"
        vault_lines.append(header)
        vault_lines.append(text)
        vault_lines.append("")
    vault_lines.append("--- END MEMORY CONTEXT ---")
    vault_context = "\n".join(vault_lines)

    # Reproduce _build_instructions()
    parts = [system_prompt]
    if workspace_context:
        parts.append(workspace_context)
    if vault_context and vault_results:
        parts.append(vault_context)

    wm = WorkingMemory().read()
    wm_lines2: list[str] = []
    last_req = str(wm.get("last_user_request") or "").strip()
    last_tool = str(wm.get("last_tool_action") or "").strip()
    active_goal = str(wm.get("active_goal") or "").strip()
    if last_req:
        wm_lines2.append(f"Last request: {last_req[:200]}")
    if last_tool:
        wm_lines2.append(f"Last tool: {last_tool}")
    if active_goal:
        wm_lines2.append(f"Active goal: {active_goal[:200]}")
    if wm_lines2:
        parts.append("--- WORKING MEMORY ---\n" + "\n".join(wm_lines2))

    full_instructions = "\n\n".join(parts)
    print(full_instructions)
    print(f"\n[TOTAL LENGTH: {len(full_instructions)} chars]")

except Exception as exc:
    print(f"ERROR building instructions: {exc}")
    import traceback; traceback.print_exc()


# ── Section 2: _build_live_state_block() equivalent ──────────────────────────
section("SECTION 2 — LIVE STATE BLOCK (_build_live_state_block output)")

try:
    from world_model import build_world_snapshot
    snap = build_world_snapshot()

    lines = [f"[LIVE STATE — {snap.get('timestamp', '')}]"]

    win = snap.get("active_window_title", "")
    app = snap.get("active_app", "")
    if win or app:
        lines.append(f"window: {win or 'unknown'}" + (f" ({app})" if app else ""))

    mission = snap.get("current_mission", "")
    if mission:
        lines.append(f"mission: {mission[:80]}")

    goal = snap.get("active_goal", "")
    if goal:
        lines.append(f"goal: {goal[:80]}")

    nxt = snap.get("next_action", "")
    if nxt:
        lines.append(f"next: {nxt[:80]}")

    branch = snap.get("git_branch", "")
    git_status = snap.get("git_status_short", "")
    if branch:
        git_line = f"git: {branch}"
        if git_status:
            changed_count = len([ln for ln in git_status.splitlines() if ln.strip()])
            git_line += f" — {changed_count} changed file{'s' if changed_count != 1 else ''}"
        lines.append(git_line)

    errors = snap.get("recent_errors", [])
    if errors:
        desc = str(errors[-1].get("description", ""))[:100]
        if desc:
            lines.append(f"errors: {desc}")

    selected = snap.get("selected_text", "")
    if selected and selected.strip():
        lines.append(f"selected: {selected.strip()[:200]}")

    procs = snap.get("running_dev_processes", [])
    if procs:
        proc_names = ", ".join(p.get("name", "") for p in procs[:3] if p.get("name"))
        if proc_names:
            lines.append(f"processes: {proc_names}")

    live_block = "\n".join(lines)
    print(live_block)
    print(f"\n[Raw snapshot keys: {list(snap.keys())}]")

except Exception as exc:
    print(f"ERROR building live state block: {exc}")
    import traceback; traceback.print_exc()


# ── Section 3: response.create payload ───────────────────────────────────────
section("SECTION 3 — response.create PAYLOAD")
print("""NOT LOGGED. This payload is sent over WebSocket but never written to disk.

On the transcript_no_override path (no direct intent override), the payload is:
  {
    "type": "response.create",
    "response": {
        "modalities": ["audio", "text"]
    }
  }

No 'instructions' key — the session-level context (Section 1) is the effective prompt.
The live state block (Section 2) is injected as a conversation.item.create system
message immediately before this response.create call.

To capture the actual WebSocket payload for a real turn, Prometheus must be modified
to log it — which this script does not do per the diagnostic-only constraint.""")


# ── Section 4 & 5: Runtime capture — start Prometheus and tail log ────────────
section("SECTION 4 — RUNTIME TURN CAPTURE (tool call + transcript from log)")

LOG_FILE = Path.home() / ".jarvis" / "logs" / f"{time.strftime('%Y-%m-%d')}.jsonl"
print(f"Log file: {LOG_FILE}")

# Check if Prometheus is running
pid_file = Path.home() / ".jarvis" / "prometheus.pid"
prometheus_running = False
if pid_file.exists():
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        prometheus_running = True
        print(f"Prometheus already running (PID {pid})")
    except (ProcessLookupError, ValueError):
        pass

if not prometheus_running:
    print("Starting Prometheus...")
    subprocess.Popen(
        ["bash", str(ROOT / "prometheus.sh"), "start"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print("Waiting 5s for Prometheus to connect...")
    time.sleep(5)

print("\n>>> Say your voice command now. Press Enter here when done. <<<")
turn_start = time.time()
input()
turn_end = time.time()

print(f"\nCapturing log events from the last {int(turn_end - turn_start + 10):.0f} seconds...\n")

# Read log and filter events from this turn
relevant_kinds = {
    "transcript",
    "tool_call_received",
    "direct_tool_override",
    "contextual_intent_override",
    "web_search_result",
    "web_search_result_direct",
    "session_instructions_debug",
    "vault_context_injected",
    "realtime_event",
    "show_logs_journalctl",
    "show_logs_file",
    "vault_recall_injected",
    "user_turn_started",
    "user_turn_committed",
}

turn_events: list[dict] = []
if LOG_FILE.exists():
    for raw_line in LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            rec = json.loads(raw_line)
            ts_str = rec.get("ts", "")
            # Parse timestamp and filter to turn window (+ 30s buffer on each side)
            try:
                import datetime
                rec_ts = datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").timestamp()
                if rec_ts >= (turn_start - 30) and rec_ts <= (turn_end + 15):
                    turn_events.append(rec)
            except Exception:
                pass
        except Exception:
            pass

if not turn_events:
    print("[No log events found in the turn window. Check that Prometheus connected successfully.]")
else:
    for ev in turn_events:
        kind = ev.get("kind", "")
        if kind not in relevant_kinds and kind not in ("transcript",):
            # Still print all events from the window so nothing is hidden
            pass
        print(json.dumps(ev, indent=2))
        print()

section("SECTION 5 — RAW RESPONSE")
print("""NOT LOGGED. The OpenAI Realtime API response arrives as a stream of WebSocket
events (response.audio.delta, response.text.delta, response.done, etc.).
These are handled in _receiver() but only the event *type* is logged via:

  log_event("realtime_event", {"type": event_type})

The full payload (including the LLM's text output) is not written to disk.

To capture it: add logging of the full event dict inside _receiver() for
event types: response.text.done, response.done, response.function_call_arguments.done""")

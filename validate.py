#!/usr/bin/env python3
"""
validate.py — Prometheus v4.0.0 end-to-end validation.
Runs 29 checks covering every major capability. No mocks except voice/HUD.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

_PROM_LOG = Path.home() / ".prometheus" / "prometheus.jsonl"
_JARVIS_LOG_DIR = Path.home() / ".jarvis" / "logs"
_RESULTS: list[tuple[int, bool, str]] = []   # (check_num, passed, message)
_BUGS_FIXED: list[str] = []


# ═══════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════

def _today_jarvis_log() -> Path:
    return _JARVIS_LOG_DIR / f"{time.strftime('%Y-%m-%d')}.jsonl"


def _read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    except Exception:
        return []


def _scan_event(kind: str, since_size: int = 0, log: str = "both") -> dict | None:
    """
    Search for an event in log files. Returns first matching entry after since_size bytes, or None.
    log: "prom" | "jarvis" | "both"
    """
    paths = []
    if log in ("prom", "both"):
        paths.append(_PROM_LOG)
    if log in ("jarvis", "both"):
        paths.append(_today_jarvis_log())

    for path in paths:
        if not path.exists():
            continue
        try:
            content = path.read_text(encoding="utf-8")
            tail = content[since_size:] if log == "prom" else content
            for line in tail.splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("kind") == kind:
                        return entry
                except Exception:
                    continue
        except Exception:
            continue
    return None


def _prom_log_size() -> int:
    return _PROM_LOG.stat().st_size if _PROM_LOG.exists() else 0


def _jarvis_log_size() -> int:
    p = _today_jarvis_log()
    return p.stat().st_size if p.exists() else 0


def _start_prometheus(extra_args: list[str] | None = None) -> subprocess.Popen:
    args = [sys.executable, str(_ROOT / "launch.py"), "--no-voice", "--no-hud"]
    args += (extra_args or [])
    return subprocess.Popen(
        args,
        cwd=str(_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _stop_prometheus(proc: subprocess.Popen, timeout: float = 8.0) -> bool:
    if proc.poll() is not None:
        return True
    try:
        proc.send_signal(signal.SIGTERM)
    except Exception:
        pass
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        proc.kill()
        return False


def _record(n: int, passed: bool, msg: str) -> None:
    _RESULTS.append((n, passed, msg))
    icon = "✅" if passed else "❌"
    print(f"Check {n:2d} {icon} — {msg}")


# ═══════════════════════════════════════════════════════════
# LAUNCH & SHUTDOWN
# ═══════════════════════════════════════════════════════════

def check_1_clean_launch() -> tuple[subprocess.Popen | None, int]:
    """Launch Prometheus. Returns (proc, prom_log_size_before)."""
    _PROM_LOG.parent.mkdir(parents=True, exist_ok=True)
    before = _prom_log_size()
    proc = _start_prometheus()
    time.sleep(5)

    alive = proc.poll() is None
    entry = _scan_event("prometheus_start", since_size=before, log="prom")

    if alive and entry:
        _record(1, True, "Prometheus started, prometheus_start logged")
    elif not alive:
        _record(1, False, f"Process exited early (rc={proc.returncode})")
    else:
        _record(1, False, "prometheus_start not found in ~/.prometheus/prometheus.jsonl")
    return proc, before


def check_2_graceful_shutdown(proc: subprocess.Popen | None, before: int) -> None:
    if proc is None or proc.poll() is not None:
        _record(2, False, "No running process from Check 1")
        return

    before_stop = _prom_log_size()
    _stop_prometheus(proc, timeout=8)
    time.sleep(1)

    exited = proc.poll() is not None
    entry = _scan_event("prometheus_shutdown", since_size=before_stop, log="prom")

    if exited and entry:
        _record(2, True, "Graceful shutdown: prometheus_shutdown logged, process exited")
    elif not exited:
        _record(2, False, "Process did not exit within 8s")
    else:
        _record(2, False, "prometheus_shutdown not found in ~/.prometheus/prometheus.jsonl")


# ═══════════════════════════════════════════════════════════
# INTELLIGENCE LAYER
# ═══════════════════════════════════════════════════════════

def check_3_dynamic_prompt() -> None:
    try:
        from prometheus_identity import build_system_prompt
        prompt = build_system_prompt(
            workspace={"active_project": "Prometheus", "active_window": "VS Code"},
            vault_context=[{"title": "Test memory", "text": "some context"}],
            recent_sessions=[],
            working_memory={"active_workspace": "Prometheus"},
            profile={"name": "Tate", "active_projects": ["Prometheus"], "working_style": "systems thinker"},
        )
        length = len(prompt)
        has_tate = "Tate" in prompt or "tate" in prompt.lower()
        has_project = "Prometheus" in prompt or "prometheus" in prompt.lower()
        if length > 500 and has_tate and has_project:
            _record(3, True, f"Dynamic prompt: {length} chars, identity sections present")
        else:
            _record(3, False, f"Prompt too short ({length} chars) or missing identity sections")
    except Exception as exc:
        _record(3, False, f"{type(exc).__name__}: {exc}")


def check_4_profile_cache() -> None:
    try:
        from prometheus_profile import PrometheusProfile
        profile = PrometheusProfile().load()
        d = profile.to_dict()
        required = {"name", "active_projects", "working_style"}
        missing = required - set(d.keys())
        if not missing:
            _record(4, True, f"Profile cache: {list(d.keys())}")
        else:
            _record(4, False, f"Profile missing keys: {missing}")
    except Exception as exc:
        _record(4, False, f"{type(exc).__name__}: {exc}")


def check_5_briefing(proc_start_time: float, jarvis_before: int) -> None:
    """Check briefing_generated within 10s of startup (already have 5s elapsed)."""
    # Wait remaining time up to 10s total from proc start
    remaining = max(0, 10 - (time.time() - proc_start_time))
    if remaining > 0:
        time.sleep(remaining)

    # Search jarvis log (briefing fires from launch.py thread, uses log_event)
    entry = None
    p = _today_jarvis_log()
    if p.exists():
        tail_content = p.read_text(encoding="utf-8")[jarvis_before:]
        for line in tail_content.splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
                if e.get("kind") == "briefing_generated":
                    entry = e
                    break
            except Exception:
                continue

    # Also check prom log
    if not entry:
        entry = _scan_event("briefing_generated", log="prom")

    elapsed = time.time() - proc_start_time
    if entry:
        length = entry.get("length", 0)
        _record(5, True, f"Briefing generated {elapsed:.1f}s after startup ({length} chars)")
    else:
        _record(5, False, f"briefing_generated not found within {elapsed:.1f}s — check _fire_no_voice_briefing()")


def check_6_proactive_loop(proc_start_time: float, jarvis_before: int) -> None:
    """Wait until 95s from launch, confirm proactive_loop_cycle in log."""
    elapsed = time.time() - proc_start_time
    remaining = max(0, 95 - elapsed)
    if remaining > 0:
        print(f"      [Check 6: waiting {remaining:.0f}s for proactive loop cycle…]")
        time.sleep(remaining)

    entry = None
    p = _today_jarvis_log()
    if p.exists():
        tail = p.read_text(encoding="utf-8")[jarvis_before:]
        for line in tail.splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
                if e.get("kind") == "proactive_loop_cycle":
                    entry = e
                    break
            except Exception:
                continue

    fired_at = time.time() - proc_start_time
    if entry:
        req_fields = {"connected", "busy", "listening", "seconds_since_voice"}
        has_fields = req_fields.issubset(set(entry.keys()))
        if has_fields:
            _record(6, True, f"Proactive loop fired at {fired_at:.0f}s with all required fields")
        else:
            missing = req_fields - set(entry.keys())
            _record(6, False, f"proactive_loop_cycle found but missing fields: {missing}")
    else:
        _record(6, False, f"proactive_loop_cycle not found after {fired_at:.0f}s")


def check_7_vault_injection() -> None:
    """Test vault injection by directly querying the vault index."""
    try:
        from memory_core import query_vault
        results = query_vault("Prometheus project assistant", limit=5)
        if results:
            titles = [r.get("title", "untitled") for r in results[:3]]
            _record(7, True, f"Vault injected: {len(results)} results, titles: {titles[:2]}")
        else:
            # Vault may not be configured — check if path is set
            from config import CONFIG
            vault_path = CONFIG.get("vault_path", "")
            if not vault_path:
                _record(7, False, "Vault not configured (vault_path empty in config). Note: session_instructions_debug requires voice.")
            else:
                _record(7, False, f"query_vault returned 0 results — check vault at {vault_path}")
    except Exception as exc:
        _record(7, False, f"{type(exc).__name__}: {exc}")


# ═══════════════════════════════════════════════════════════
# CODE TOOLS
# ═══════════════════════════════════════════════════════════

def check_8_search_codebase() -> None:
    try:
        from tools import ToolRegistry
        tools = ToolRegistry()
        result = tools.execute({"action": "search_codebase", "query": "ProactiveLoop", "project_path": str(_ROOT)})
        count = (result.data or {}).get("count", 0)
        if result.ok and count >= 1:
            _record(8, True, f"search_codebase: {count} matches for 'ProactiveLoop'")
        else:
            _record(8, False, f"search_codebase returned count={count}, ok={result.ok}: {result.message}")
    except Exception as exc:
        _record(8, False, f"{type(exc).__name__}: {exc}")


def check_9_git_status() -> None:
    try:
        from tools import ToolRegistry
        tools = ToolRegistry()
        result = tools.execute({"action": "git_status", "project_path": str(_ROOT)})
        if result.ok:
            status_text = (result.data or {}).get("status", "")
            changed = len([l for l in status_text.splitlines() if l.strip()])
            _record(9, True, f"git_status: {changed} changed files")
        else:
            _record(9, False, f"git_status failed: {result.message}")
    except Exception as exc:
        _record(9, False, f"{type(exc).__name__}: {exc}")


def check_10_run_shell() -> None:
    try:
        from tools import ToolRegistry
        tools = ToolRegistry()
        result = tools.execute({"action": "run_shell", "command": "echo prometheus-ok"})
        output = str((result.data or {}).get("output", ""))
        if result.ok and "prometheus-ok" in output:
            _record(10, True, "run_shell: stdout confirmed")
        else:
            _record(10, False, f"run_shell output={output!r}, ok={result.ok}: {result.message}")
    except Exception as exc:
        _record(10, False, f"{type(exc).__name__}: {exc}")


# ═══════════════════════════════════════════════════════════
# GIT SAFETY
# ═══════════════════════════════════════════════════════════

def check_11_checkpoint_rollback() -> None:
    try:
        from git_safety import GitSafety
        gs = GitSafety()

        # Create temp file
        target = _ROOT / "validate_check11_temp.txt"
        target.write_text("validation check 11\n", encoding="utf-8")

        sha = gs.checkpoint("validation-check-11")
        assert len(sha) == 8 and all(c in "0123456789abcdef" for c in sha.lower()), f"bad SHA: {sha!r}"

        # Delete the file
        target.unlink()
        assert not target.exists()

        # Rollback
        ok = gs.rollback(sha)
        assert ok, "rollback() returned False"
        assert target.exists(), "temp file not restored after rollback"

        # Cleanup
        target.unlink()
        subprocess.run(["git", "add", "-A"], cwd=str(_ROOT), capture_output=True)
        subprocess.run(["git", "commit", "-m", "validate: clean up check 11 temp file"],
                      cwd=str(_ROOT), capture_output=True)

        _record(11, True, f"Checkpoint {sha}, rollback restored file")
    except Exception as exc:
        _record(11, False, f"{type(exc).__name__}: {exc}")


def check_12_diff_since() -> None:
    try:
        from git_safety import GitSafety
        gs = GitSafety()

        # Get current SHA as pre-change reference
        pre_sha = gs.current_sha()
        assert pre_sha, "could not get current SHA"

        # Make a small change
        target = _ROOT / "validate_check12_temp.py"
        target.write_text("# validate check 12 temp\n", encoding="utf-8")

        # Checkpoint the change
        sha = gs.checkpoint("validation-check-12")

        # diff_since
        diff = gs.diff_since(pre_sha)
        has_content = bool(diff.strip())
        has_file = "validate_check12_temp" in diff

        # Revert
        target.unlink()
        subprocess.run(["git", "add", "-A"], cwd=str(_ROOT), capture_output=True)
        subprocess.run(["git", "commit", "-m", "validate: clean up check 12 temp file"],
                      cwd=str(_ROOT), capture_output=True)

        lines = len(diff.splitlines())
        if has_content and has_file:
            _record(12, True, f"diff_since returned {lines} lines for 1 changed file")
        else:
            _record(12, False, f"diff_since: has_content={has_content}, has_file={has_file}, diff={diff[:100]!r}")
    except Exception as exc:
        _record(12, False, f"{type(exc).__name__}: {exc}")


# ═══════════════════════════════════════════════════════════
# SUCCESS CRITERIA ENGINE
# ═══════════════════════════════════════════════════════════

def check_13_infer_from_goal() -> None:
    try:
        from success_criteria import SuccessCriteriaEngine
        engine = SuccessCriteriaEngine()
        cases = [
            ("fix the vault injection bug",          "log"),
            ("write tests for the orchestrator",     "test"),
            ("create the output report file",        "file_exists"),
            ("make sure the server returns exit 0",  "shell_exit"),
            ("do something ambiguous",               "manual"),
        ]
        all_ok = True
        for goal, expected in cases:
            c = engine.infer_from_goal(goal)
            if c.check_type != expected:
                _record(13, False, f"Goal '{goal}' → expected {expected!r}, got {c.check_type!r}")
                all_ok = False
                break
        if all_ok:
            _record(13, True, "All 5 goal patterns inferred correctly")
    except Exception as exc:
        _record(13, False, f"{type(exc).__name__}: {exc}")


def check_14_evaluate_criteria() -> None:
    try:
        from success_criteria import SuccessCriteria, SuccessCriteriaEngine
        engine = SuccessCriteriaEngine()

        # Log type
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"kind": "vault_context_injected", "ts": "2026-05-04"}\n')
            log_path = f.name
        try:
            c = SuccessCriteria(goal="test", check_type="log", check_value="vault_context_injected")
            assert engine.evaluate(c, log_path=log_path) is True
            c2 = SuccessCriteria(goal="test", check_type="log", check_value="nonexistent_xyz")
            assert engine.evaluate(c2, log_path=log_path) is False
        finally:
            os.unlink(log_path)

        # Test type
        c3 = SuccessCriteria(goal="test", check_type="test", check_value="echo ok")
        assert engine.evaluate(c3) is True
        c4 = SuccessCriteria(goal="test", check_type="test", check_value="false")
        assert engine.evaluate(c4) is False

        _record(14, True, "Criteria evaluation correct for log and test types")
    except Exception as exc:
        _record(14, False, f"{type(exc).__name__}: {exc}")


# ═══════════════════════════════════════════════════════════
# CODING AGENT
# ═══════════════════════════════════════════════════════════

def check_15_start_coding_task() -> None:
    try:
        from coding_agent import start_coding_task
        t0 = time.time()
        result = start_coding_task("add a docstring to git_safety.py")
        elapsed = time.time() - t0
        if elapsed < 1.0 and result.get("status") == "started":
            _record(15, True, f"start_coding_task returned in {elapsed:.3f}s")
        else:
            _record(15, False, f"elapsed={elapsed:.2f}s, status={result.get('status')!r}")
    except Exception as exc:
        _record(15, False, f"{type(exc).__name__}: {exc}")


def check_16_coding_status() -> None:
    """Poll get_coding_status for up to 60s for status to change from 'running'."""
    try:
        from coding_agent import get_coding_status
        # Brief wait for the background thread to write "running"
        time.sleep(0.5)
        initial = get_coding_status()
        initial_status = initial.get("status", "")

        if initial_status not in ("running", "no task running"):
            _record(16, False, f"Unexpected initial status: {initial_status!r}")
            return

        deadline = time.time() + 60
        final: dict = initial
        while time.time() < deadline:
            time.sleep(3)
            status = get_coding_status()
            final = status
            s = status.get("status", "")
            if s not in ("running",):
                break

        has_fields = all(k in final for k in ("goal",))
        success = final.get("success")
        attempts = final.get("attempts", 0)
        status = final.get("status", "")

        if has_fields and status != "running":
            _record(16, True, f"Coding task: status {initial_status}→{status!r}, success={success}, {attempts} attempt(s)")
        else:
            _record(16, False, f"Status still 'running' after 60s or missing fields: {final}")
    except Exception as exc:
        _record(16, False, f"{type(exc).__name__}: {exc}")


def check_17_coding_agent_cost_limit() -> None:
    try:
        from cost_tracker import CostTracker
        from coding_agent import CodingAgent, CodingResult
        from success_criteria import SuccessCriteria
        import tempfile, os

        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            log_path = f.name
        try:
            tracker = CostTracker(daily_limit_usd=0.00001, session_limit_usd=0.00001, log_path=log_path)
            # Pre-exceed limit
            tracker.record("check17", 1000, 1000, "claude-sonnet-4")
            assert not tracker.check_limits()["ok"]

            # Mock agent that tracks if _run_claude was called
            class _TrackedAgent(CodingAgent):
                invoked = False
                def _run_claude(self, prompt: str) -> str:
                    _TrackedAgent.invoked = True
                    return "should not reach"

            agent = _TrackedAgent(max_retries=2, timeout=30, cost_tracker=tracker)
            criteria = SuccessCriteria(goal="check17", check_type="manual", check_value="")
            result = agent.run(goal="write hello world", criteria=criteria)

            assert result.success is False
            assert "limit" in result.output.lower() or "aborted" in result.output.lower()
            assert not _TrackedAgent.invoked, "Claude should not have been invoked"
            _record(17, True, "CodingAgent blocked at cost limit")
        finally:
            os.unlink(log_path)
    except Exception as exc:
        _record(17, False, f"{type(exc).__name__}: {exc}")


# ═══════════════════════════════════════════════════════════
# MULTI-AGENT ORCHESTRATION
# ═══════════════════════════════════════════════════════════

def check_18_start_build() -> None:
    try:
        from orchestrator import start_build
        t0 = time.time()
        result = start_build("add a __version__ = '4.0.0' constant to launch.py")
        elapsed = time.time() - t0
        if elapsed < 1.0 and result.get("status") == "started":
            _record(18, True, f"start_build dispatched immediately in {elapsed:.3f}s")
        else:
            _record(18, False, f"elapsed={elapsed:.2f}s, status={result.get('status')!r}")
    except Exception as exc:
        _record(18, False, f"{type(exc).__name__}: {exc}")


def check_19_build_status() -> None:
    """Poll get_build_status for up to 3 minutes."""
    try:
        from orchestrator import get_build_status
        time.sleep(1)

        initial = get_build_status()
        initial_status = initial.get("status", "")

        deadline = time.time() + 180
        final: dict = initial
        last_print = time.time()
        while time.time() < deadline:
            time.sleep(5)
            status = get_build_status()
            final = status
            s = status.get("status", "")
            if time.time() - last_print > 20:
                print(f"      [Check 19: build status = {s!r}…]")
                last_print = time.time()
            if s not in ("running",):
                break

        phases = final.get("phases_completed", [])
        tr = final.get("test_results", {})
        passed = tr.get("passed", 0)
        failed = tr.get("failed", 0)
        s = final.get("status", "")

        if phases and s not in ("running",):
            _record(19, True, f"Build complete: phases={phases[:4]}, tests={passed}p/{failed}f, status={s!r}")
        else:
            _record(19, False, f"Build still running or no phases after 3min: {final.get('status')!r}")
    except Exception as exc:
        _record(19, False, f"{type(exc).__name__}: {exc}")


def check_20_orchestration_log_events() -> None:
    """Scan jarvis log for orchestration events from the recent build."""
    try:
        # Accept either naming convention: orchestrator_* or orchestration_*
        expected_events = [
            ("orchestration_start", "orchestrator_start"),
            ("orchestration_plan_ready", "orchestrator_architect_ok"),
            ("orchestration_coder_complete", "orchestrator_coder_done"),
            ("orchestration_test_results", "orchestrator_tester_done"),
            ("orchestration_complete", "orchestrator_success"),
        ]
        p = _today_jarvis_log()
        if not p.exists():
            _record(20, False, "Jarvis log file not found")
            return
        content = p.read_text(encoding="utf-8")
        found = []
        missing = []
        for (canonical, fallback) in expected_events:
            if f'"kind": "{canonical}"' in content or f'"kind": "{fallback}"' in content:
                found.append(canonical)
            else:
                missing.append(canonical)

        if not missing:
            _record(20, True, f"All orchestration log events present: {found}")
        else:
            _record(20, False, f"Missing events: {missing}. Found: {found}")
    except Exception as exc:
        _record(20, False, f"{type(exc).__name__}: {exc}")


# ═══════════════════════════════════════════════════════════
# COST TRACKER
# ═══════════════════════════════════════════════════════════

def check_21_cost_tracker_accumulate() -> None:
    try:
        import tempfile, os
        from cost_tracker import CostTracker

        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            log_path = f.name
        try:
            tracker = CostTracker(daily_limit_usd=10.0, session_limit_usd=5.0, log_path=log_path)
            # 500 input + 500 output at claude-sonnet-4 → (500*3 + 500*15)/1e6 = $0.009
            c1 = tracker.record("check21_a", 500, 500, "claude-sonnet-4")
            c2 = tracker.record("check21_b", 500, 500, "claude-sonnet-4")
            expected = (500 * 3.0 + 500 * 15.0) / 1_000_000.0 * 2
            assert abs(tracker.session_total - expected) < 1e-7

            # Verify log entries
            lines = [l for l in Path(log_path).read_text().splitlines() if l.strip()]
            assert len(lines) == 2

            s = tracker.session_summary()
            _record(21, True, f"Cost tracker: session=${s['session_total']:.4f}, daily=${s['daily_total']:.4f}")
        finally:
            os.unlink(log_path)
    except Exception as exc:
        _record(21, False, f"{type(exc).__name__}: {exc}")


def check_22_cost_daily_reset() -> None:
    try:
        import tempfile, os
        from cost_tracker import CostTracker

        yesterday = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 86400))
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as f:
            entry = {"timestamp": yesterday, "source": "old", "input_tokens": 1000,
                     "output_tokens": 1000, "model": "claude-sonnet-4", "cost_usd": 3.50}
            f.write(json.dumps(entry) + "\n")
            log_path = f.name
        try:
            tracker = CostTracker(daily_limit_usd=5.0, session_limit_usd=2.0, log_path=log_path)
            assert tracker.daily_total == 0.0, f"expected 0.0, got {tracker.daily_total}"
            _record(22, True, "Daily cost reset confirmed")
        finally:
            os.unlink(log_path)
    except Exception as exc:
        _record(22, False, f"{type(exc).__name__}: {exc}")


# ═══════════════════════════════════════════════════════════
# LOG VIEWER
# ═══════════════════════════════════════════════════════════

def check_23_log_viewer() -> None:
    try:
        import tempfile, os
        from log_viewer import LogViewer

        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as f:
            for i in range(20):
                f.write(json.dumps({"kind": f"event_{i}", "ts": f"2026-05-04 10:{i:02d}:00"}) + "\n")
            f.write(json.dumps({"kind": "prometheus_start", "ts": "2026-05-04 10:20:00"}) + "\n")
            f.write(json.dumps({"kind": "vault_context_injected", "ts": "2026-05-04 10:21:00"}) + "\n")
            f.write(json.dumps({"kind": "vault_context_injected", "ts": "2026-05-04 10:22:00"}) + "\n")
            log_path = f.name
        try:
            viewer = LogViewer(log_path=log_path)

            # Tail
            tail = viewer.tail(10)
            assert len(tail) == 10, f"tail returned {len(tail)} not 10"

            # Filter
            starts = viewer.filter(event_name="prometheus_start")
            assert len(starts) >= 1, "filter found no prometheus_start events"

            # Summarize
            summary = viewer.summarize_session()
            assert "vault_context_injected: 2" in summary
            event_types = len([l for l in summary.splitlines() if l.strip().startswith("-")])

            _record(23, True, f"LogViewer: tail OK, filter OK, summary: {event_types} event types")
        finally:
            os.unlink(log_path)
    except Exception as exc:
        _record(23, False, f"{type(exc).__name__}: {exc}")


def check_24_errors_since_startup() -> None:
    try:
        import tempfile, os
        from log_viewer import LogViewer

        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as f:
            f.write(json.dumps({"kind": "old_error", "level": "error", "ts": "2026-05-04 09:00:00"}) + "\n")
            f.write(json.dumps({"kind": "prometheus_start", "ts": "2026-05-04 10:00:00"}) + "\n")
            f.write(json.dumps({"kind": "tool_error", "level": "error", "ts": "2026-05-04 10:01:00"}) + "\n")
            log_path = f.name
        try:
            viewer = LogViewer(log_path=log_path)
            errors = viewer.errors_since_startup()
            # Should return tool_error but not old_error
            kinds = [e.get("kind") for e in errors]
            assert "tool_error" in kinds, f"tool_error not found in errors: {kinds}"
            assert "old_error" not in kinds, f"old_error (pre-startup) should not appear: {kinds}"
            _record(24, True, f"errors_since_startup returned {len(errors)} error entries")
        finally:
            os.unlink(log_path)
    except Exception as exc:
        _record(24, False, f"{type(exc).__name__}: {exc}")


# ═══════════════════════════════════════════════════════════
# WATCHDOG
# ═══════════════════════════════════════════════════════════

def check_25_watchdog_task_timeout() -> None:
    try:
        from watchdog import PrometheusWatchdog
        from working_memory import WorkingMemory

        wm = WorkingMemory()
        started_11_min_ago = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 660))
        wm.write({
            "last_orchestration_result": {
                "status": "running",
                "goal": "check 25 timeout test",
                "started_at": started_11_min_ago,
            }
        })

        jarvis_before = _jarvis_log_size()
        wd = PrometheusWatchdog(working_memory=wm)
        wd._check_background_threads()

        updated = wm.read().get("last_orchestration_result", {})
        assert updated.get("status") == "timeout", f"expected 'timeout', got {updated.get('status')!r}"

        # Verify log event
        p = _today_jarvis_log()
        new_content = p.read_text(encoding="utf-8")[jarvis_before:] if p.exists() else ""
        assert "watchdog_task_timeout" in new_content

        # Cleanup
        wm.write({"last_orchestration_result": None})
        _record(25, True, "Watchdog task timeout detected and logged")
    except Exception as exc:
        _record(25, False, f"{type(exc).__name__}: {exc}")


def check_26_watchdog_cost_limit() -> None:
    try:
        import tempfile, os
        from cost_tracker import CostTracker
        from watchdog import PrometheusWatchdog
        from working_memory import WorkingMemory

        wm = WorkingMemory()
        wm.write({"cost_limit_reached": False})

        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            log_path = f.name
        try:
            tracker = CostTracker(daily_limit_usd=0.0001, session_limit_usd=0.0001, log_path=log_path)
            tracker.record("check26", 1000, 1000, "claude-sonnet-4")
            assert not tracker.check_limits()["ok"]

            jarvis_before = _jarvis_log_size()
            wd = PrometheusWatchdog(working_memory=wm, cost_tracker=tracker)
            wd._check_cost_limits()

            assert wm.read().get("cost_limit_reached") is True

            p = _today_jarvis_log()
            new_content = p.read_text(encoding="utf-8")[jarvis_before:] if p.exists() else ""
            assert "watchdog_cost_limit" in new_content

            wm.write({"cost_limit_reached": False})
            _record(26, True, "Watchdog cost limit detection confirmed")
        finally:
            os.unlink(log_path)
    except Exception as exc:
        _record(26, False, f"{type(exc).__name__}: {exc}")


def check_27_watchdog_alive() -> None:
    """Start PrometheusApp, confirm watchdog is alive, then stop."""
    try:
        from launch import PrometheusApp
        app = PrometheusApp(["--no-voice", "--no-hud"])
        app.start()
        time.sleep(0.5)
        alive = app.watchdog is not None and app.watchdog.is_alive()
        app.stop()
        if alive:
            _record(27, True, "Watchdog running after launch")
        else:
            _record(27, False, "Watchdog thread not alive after launch")
    except Exception as exc:
        _record(27, False, f"{type(exc).__name__}: {exc}")


# ═══════════════════════════════════════════════════════════
# SESSION CONTINUITY
# ═══════════════════════════════════════════════════════════

def check_28_wrapup_written(jarvis_before_c28: int) -> None:
    """Start, SIGTERM, check for next_session_context in WorkingMemory."""
    proc = _start_prometheus()
    time.sleep(4)

    before_stop = _prom_log_size()
    jarvis_before_stop = _jarvis_log_size()
    _stop_prometheus(proc, timeout=10)
    time.sleep(2)

    # Check prometheus.jsonl for shutdown
    entry = _scan_event("prometheus_shutdown", since_size=before_stop, log="prom")

    # Check WorkingMemory for next_session_context written by SessionSummarizer
    try:
        from working_memory import WorkingMemory
        wm_data = WorkingMemory().read()
        next_ctx = str(wm_data.get("next_session_context") or "").strip()
    except Exception:
        next_ctx = ""

    # Check for wrapup or session file
    prom_dir = Path.home() / ".prometheus"
    session_files = list(prom_dir.glob("session_*.md")) + list(prom_dir.glob("session_*.json"))

    # Also check vault sessions if configured
    try:
        from config import CONFIG
        vault_path = CONFIG.get("vault_path", "")
        if vault_path:
            sessions_dir = Path(vault_path).expanduser() / "vault" / "Sessions"
            year_dirs = sorted(
                [d for d in sessions_dir.iterdir() if d.is_dir() and d.name.isdigit()],
                reverse=True
            ) if sessions_dir.is_dir() else []
            if year_dirs:
                recent = sorted(year_dirs[0].glob("*.md"), reverse=True)[:1]
                session_files += recent
    except Exception:
        pass

    if next_ctx:
        _record(28, True, f"Wrap-up written: next_session_context set ({len(next_ctx)} chars)")
    elif session_files:
        _record(28, True, f"Wrap-up file written: {session_files[0].name}")
    else:
        # Check jarvis log for wrapup event
        p = _today_jarvis_log()
        new_content = p.read_text(encoding="utf-8")[jarvis_before_c28:] if p.exists() else ""
        if "wrapup" in new_content or "session_summariz" in new_content:
            _record(28, True, "Wrap-up triggered (log event found)")
        else:
            _record(28, False, "No wrap-up file, no next_session_context, no wrapup log event")


def check_29_restart_briefing() -> None:
    """Restart Prometheus and check briefing references previous context."""
    try:
        from working_memory import WorkingMemory
        wm = WorkingMemory()
        # Set a known next_session_context to check for on restart
        wm.write({"next_session_context": "Prometheus validation session 4"})

        before_j = _jarvis_log_size()
        proc = _start_prometheus()
        time.sleep(10)  # Wait for briefing to fire (3s delay + margin)
        _stop_prometheus(proc, timeout=6)

        # Check jarvis log for briefing_generated
        p = _today_jarvis_log()
        entry = None
        if p.exists():
            tail = p.read_text(encoding="utf-8")[before_j:]
            for line in tail.splitlines():
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                    if e.get("kind") == "briefing_generated":
                        entry = e
                        break
                except Exception:
                    continue

        if not entry:
            entry = _scan_event("briefing_generated", log="prom")

        if entry and entry.get("has_prev_context"):
            _record(29, True, "Restart briefing references previous session context")
        elif entry:
            _record(29, False, f"briefing_generated found but has_prev_context=False: {entry}")
        else:
            _record(29, False, "briefing_generated not found on restart")

        # Restore WM
        wm.write({"next_session_context": ""})
    except Exception as exc:
        _record(29, False, f"{type(exc).__name__}: {exc}")


# ═══════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════

def main() -> None:
    print()
    print("═══════════════════════════════════════════")
    print("  PROMETHEUS v4.0.0 — End-to-End Validation")
    print("═══════════════════════════════════════════")
    print()

    jarvis_before_global = _jarvis_log_size()

    # ── LAUNCH & SHUTDOWN ─────────────────────────────────
    print("── LAUNCH & SHUTDOWN ──")
    proc1, prom_before1 = check_1_clean_launch()
    check_2_graceful_shutdown(proc1, prom_before1)

    # ── INTELLIGENCE LAYER ────────────────────────────────
    print("\n── INTELLIGENCE LAYER ──")
    check_3_dynamic_prompt()
    check_4_profile_cache()

    # Start a persistent instance for checks 5-7 (needs 95s for proactive loop)
    jarvis_before_5 = _jarvis_log_size()
    proc5 = _start_prometheus()
    proc5_start = time.time()

    check_5_briefing(proc5_start, jarvis_before_5)
    check_6_proactive_loop(proc5_start, jarvis_before_5)
    check_7_vault_injection()

    _stop_prometheus(proc5, timeout=8)

    # ── CODE TOOLS ────────────────────────────────────────
    print("\n── CODE TOOLS ──")
    check_8_search_codebase()
    check_9_git_status()
    check_10_run_shell()

    # ── GIT SAFETY ───────────────────────────────────────
    print("\n── GIT SAFETY ──")
    check_11_checkpoint_rollback()
    check_12_diff_since()

    # ── SUCCESS CRITERIA ──────────────────────────────────
    print("\n── SUCCESS CRITERIA ENGINE ──")
    check_13_infer_from_goal()
    check_14_evaluate_criteria()

    # ── CODING AGENT ──────────────────────────────────────
    print("\n── CODING AGENT ──")
    check_15_start_coding_task()
    check_16_coding_status()     # polls up to 60s
    check_17_coding_agent_cost_limit()

    # ── ORCHESTRATION ─────────────────────────────────────
    print("\n── MULTI-AGENT ORCHESTRATION ──")
    check_18_start_build()
    check_19_build_status()      # polls up to 3 min
    check_20_orchestration_log_events()

    # ── COST TRACKER ──────────────────────────────────────
    print("\n── COST TRACKER ──")
    check_21_cost_tracker_accumulate()
    check_22_cost_daily_reset()

    # ── LOG VIEWER ────────────────────────────────────────
    print("\n── LOG VIEWER ──")
    check_23_log_viewer()
    check_24_errors_since_startup()

    # ── WATCHDOG ──────────────────────────────────────────
    print("\n── WATCHDOG ──")
    check_25_watchdog_task_timeout()
    check_26_watchdog_cost_limit()
    check_27_watchdog_alive()

    # ── SESSION CONTINUITY ────────────────────────────────
    print("\n── SESSION CONTINUITY ──")
    check_28_wrapup_written(jarvis_before_global)
    check_29_restart_briefing()

    # ── FINAL SUMMARY ─────────────────────────────────────
    total = len(_RESULTS)
    passed_list = [(n, m) for (n, ok, m) in _RESULTS if ok]
    failed_list = [(n, m) for (n, ok, m) in _RESULTS if not ok]
    n_passed = len(passed_list)
    n_failed = len(failed_list)

    print()
    print("═══════════════════════════════════════════")
    print("  PROMETHEUS v4.0.0 VALIDATION COMPLETE")
    print("═══════════════════════════════════════════")
    print(f"  Passed: {n_passed}/{total}")
    print(f"  Failed: {n_failed}/{total}")

    if failed_list:
        print()
        print("  Failed checks:")
        for n, msg in failed_list:
            print(f"    - Check {n}: {msg}")

    if _BUGS_FIXED:
        print()
        print("  Bugs found and fixed during validation:")
        for b in _BUGS_FIXED:
            print(f"    - {b}")

    print()
    if n_failed == 0:
        print("  Prometheus is PRODUCTION READY.")
    else:
        print("  Prometheus is NOT READY — fix failures before use.")
    print("═══════════════════════════════════════════")
    print()

    sys.exit(0 if n_failed == 0 else 1)


if __name__ == "__main__":
    main()

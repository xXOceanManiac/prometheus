"""
tests/test_session4.py — Session 4 test suite.

Tests CostTracker, LogViewer, Watchdog, CodingAgent cost abort, and launch cycle.
All tests must pass. Run with:  python3 tests/test_session4.py
"""
from __future__ import annotations

import json
import os
import sys
import signal
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cost_tracker import CostTracker
from log_viewer import LogViewer
from watchdog import PrometheusWatchdog
from working_memory import WorkingMemory
from coding_agent import CodingAgent, CodingResult
from success_criteria import SuccessCriteria

_PASS = "✅"
_FAIL = "❌"


def _run_test(name: str, fn) -> bool:
    try:
        fn()
        return True
    except AssertionError as exc:
        print(f"{_FAIL} {name} — AssertionError: {exc}")
        return False
    except Exception as exc:
        print(f"{_FAIL} {name} — {type(exc).__name__}: {exc}")
        return False


# ──────────────────────────────────────────────────────────
# Test 1 — CostTracker records and accumulates correctly
# ──────────────────────────────────────────────────────────

def test_1_cost_tracker_record():
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
        log_path = f.name

    try:
        tracker = CostTracker(
            daily_limit_usd=10.00,
            session_limit_usd=5.00,
            log_path=log_path,
        )

        # Known token counts for easy manual verification
        # claude-sonnet-4: $3/M input, $15/M output
        # Call 1: 1000 input, 1000 output → (1000*3 + 1000*15)/1e6 = $0.018
        c1 = tracker.record("test1", 1000, 1000, "claude-sonnet-4")
        # Call 2: 1000 input, 1000 output → $0.018
        c2 = tracker.record("test2", 1000, 1000, "claude-sonnet-4")
        # Call 3: 1000 input, 1000 output → $0.018
        c3 = tracker.record("test3", 1000, 1000, "claude-sonnet-4")

        expected_per_call = (1000 * 3.0 + 1000 * 15.0) / 1_000_000.0  # $0.018
        expected_total = expected_per_call * 3  # $0.054

        assert abs(c1 - expected_per_call) < 1e-8, f"call1 cost wrong: {c1}"
        assert abs(c2 - expected_per_call) < 1e-8, f"call2 cost wrong: {c2}"
        assert abs(c3 - expected_per_call) < 1e-8, f"call3 cost wrong: {c3}"
        assert abs(tracker.session_total - expected_total) < 1e-7, (
            f"session_total wrong: {tracker.session_total} vs {expected_total}"
        )

        # Verify log file has 3 entries
        lines = [l for l in Path(log_path).read_text().splitlines() if l.strip()]
        assert len(lines) == 3, f"expected 3 log entries, got {len(lines)}"

        summary = tracker.session_summary()
        assert summary["calls"] == 3, f"expected 3 calls, got {summary['calls']}"

        total = tracker.session_total
        print(f"{_PASS} Test 1 — CostTracker: 3 calls recorded, session_total=${total:.4f}")
    finally:
        os.unlink(log_path)


# ──────────────────────────────────────────────────────────
# Test 2 — CostTracker daily limit blocks further calls
# ──────────────────────────────────────────────────────────

def test_2_cost_limit_enforced():
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
        log_path = f.name

    try:
        # Very low limit — one call will exceed it
        tracker = CostTracker(
            daily_limit_usd=0.001,
            session_limit_usd=100.0,  # session limit high so daily limit triggers
            log_path=log_path,
        )

        # Record a call that will push daily total over 0.001
        # 1000 input + 1000 output at claude-sonnet-4 → $0.018 >> $0.001
        tracker.record("test", 1000, 1000, "claude-sonnet-4")

        result = tracker.check_limits()
        assert result["ok"] is False, f"expected ok=False, got {result}"
        assert isinstance(result["reason"], str), "expected reason string"
        assert "Daily limit" in result["reason"] or "limit" in result["reason"].lower(), (
            f"reason should mention 'limit': {result['reason']}"
        )
        assert result["daily_total"] > 0, "daily_total should be > 0"

        reason = result["reason"]
        print(f"{_PASS} Test 2 — Cost limit enforced: {reason}")
    finally:
        os.unlink(log_path)


# ──────────────────────────────────────────────────────────
# Test 3 — CostTracker daily reset on new calendar day
# ──────────────────────────────────────────────────────────

def test_3_daily_reset():
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as f:
        # Write an entry with yesterday's date and a large cost
        yesterday = time.strftime(
            "%Y-%m-%dT%H:%M:%S",
            time.localtime(time.time() - 86400)
        )
        entry = {
            "timestamp": yesterday,
            "source": "test",
            "input_tokens": 100000,
            "output_tokens": 100000,
            "model": "claude-sonnet-4",
            "cost_usd": 4.50,  # large amount from yesterday
        }
        f.write(json.dumps(entry) + "\n")
        log_path = f.name

    try:
        # New CostTracker should see yesterday's entry and reset daily_total to 0
        tracker = CostTracker(
            daily_limit_usd=5.00,
            session_limit_usd=2.00,
            log_path=log_path,
        )

        assert tracker.daily_total == 0.0, (
            f"daily_total should be 0.0 after daily reset, got {tracker.daily_total}"
        )
        assert tracker.session_total == 0.0, "session_total should start at 0.0"

        print(f"{_PASS} Test 3 — Daily cost reset on new calendar day")
    finally:
        os.unlink(log_path)


# ──────────────────────────────────────────────────────────
# Test 4 — CodingAgent aborts when cost limit reached
# ──────────────────────────────────────────────────────────

class _ClaudeNotInvokedAgent(CodingAgent):
    """Track whether _run_claude was ever called."""
    claude_invoked = False

    def _run_claude(self, prompt: str) -> str:
        _ClaudeNotInvokedAgent.claude_invoked = True
        return "should not be reached"


def test_4_coding_agent_cost_abort():
    _ClaudeNotInvokedAgent.claude_invoked = False

    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
        log_path = f.name

    try:
        # Set limit extremely low
        tracker = CostTracker(
            daily_limit_usd=0.001,
            session_limit_usd=0.001,
            log_path=log_path,
        )
        # Pre-exceed the limit
        tracker.record("setup", 1000, 1000, "claude-sonnet-4")

        # Verify limit is now exceeded
        check = tracker.check_limits()
        assert not check["ok"], "cost tracker should be over limit before test"

        agent = _ClaudeNotInvokedAgent(max_retries=3, timeout=30, cost_tracker=tracker)
        criteria = SuccessCriteria(
            goal="test goal",
            check_type="manual",
            check_value="",
        )
        result = agent.run(goal="test goal", criteria=criteria)

        assert result.success is False, f"expected failure due to cost limit, got success={result.success}"
        assert "limit" in result.output.lower() or "aborted" in result.output.lower(), (
            f"output should mention limit/aborted: {result.output!r}"
        )
        assert _ClaudeNotInvokedAgent.claude_invoked is False, (
            "Claude subprocess should NOT have been invoked after cost limit abort"
        )

        print(f"{_PASS} Test 4 — CodingAgent aborted at cost limit before invoking Claude")
    finally:
        os.unlink(log_path)


# ──────────────────────────────────────────────────────────
# Test 5 — LogViewer tail returns last N entries
# ──────────────────────────────────────────────────────────

def test_5_log_viewer_tail():
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as f:
        # Write 20 known entries
        for i in range(20):
            entry = {"kind": f"event_{i}", "ts": f"2026-05-04 10:{i:02d}:00", "index": i}
            f.write(json.dumps(entry) + "\n")
        log_path = f.name

    try:
        viewer = LogViewer(log_path=log_path)
        result = viewer.tail(5)

        assert len(result) == 5, f"expected 5 entries, got {len(result)}"
        # Should be the last 5 (indices 15–19)
        for expected_idx, entry in zip(range(15, 20), result):
            actual_idx = entry.get("index")
            assert actual_idx == expected_idx, (
                f"expected index {expected_idx}, got {actual_idx}"
            )

        print(f"{_PASS} Test 5 — LogViewer.tail(5) returned correct 5 entries")
    finally:
        os.unlink(log_path)


# ──────────────────────────────────────────────────────────
# Test 6 — LogViewer summarize_session groups events correctly
# ──────────────────────────────────────────────────────────

def test_6_log_viewer_summarize():
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as f:
        entries = [
            {"kind": "old_event", "ts": "2026-05-04 09:00:00"},
            {"kind": "prometheus_start", "ts": "2026-05-04 10:00:00"},
            {"kind": "vault_context_injected", "ts": "2026-05-04 10:01:00"},
            {"kind": "vault_context_injected", "ts": "2026-05-04 10:02:00"},
            {"kind": "coding_agent_success", "ts": "2026-05-04 10:03:00"},
        ]
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
        log_path = f.name

    try:
        viewer = LogViewer(log_path=log_path)
        summary = viewer.summarize_session()

        assert "vault_context_injected: 2" in summary, (
            f"expected 'vault_context_injected: 2' in summary:\n{summary}"
        )
        assert "coding_agent_success: 1" in summary, (
            f"expected 'coding_agent_success: 1' in summary:\n{summary}"
        )
        # old_event was before prometheus_start — should not appear
        assert "old_event" not in summary, (
            f"old_event (before prometheus_start) should not appear in summary:\n{summary}"
        )

        print(f"{_PASS} Test 6 — LogViewer session summary: {summary[:80].replace(chr(10),' | ')}")
    finally:
        os.unlink(log_path)


# ──────────────────────────────────────────────────────────
# Test 7 — Watchdog detects timed-out background task
# ──────────────────────────────────────────────────────────

def test_7_watchdog_task_timeout():
    wm = WorkingMemory()

    # Write a "running" task that started 11 minutes ago
    started_11_min_ago = time.strftime(
        "%Y-%m-%dT%H:%M:%S",
        time.localtime(time.time() - 11 * 60)
    )
    wm.write({
        "last_orchestration_result": {
            "status": "running",
            "goal": "test watchdog timeout",
            "started_at": started_11_min_ago,
        }
    })

    watchdog = PrometheusWatchdog(working_memory=wm)

    # Record log file state before check
    from config import LOG_DIR
    log_file = LOG_DIR / f"{time.strftime('%Y-%m-%d')}.jsonl"
    before_size = log_file.stat().st_size if log_file.exists() else 0

    # Run check directly (not via timer)
    watchdog._check_background_threads()

    # Verify WorkingMemory was updated
    updated = wm.read().get("last_orchestration_result", {})
    assert updated.get("status") == "timeout", (
        f"expected status='timeout', got {updated.get('status')!r}"
    )

    # Verify watchdog_task_timeout was logged
    if log_file.exists():
        content = log_file.read_text(encoding="utf-8")
        # Only check text added after our before_size
        new_content = content[before_size:]
        assert "watchdog_task_timeout" in new_content, (
            "watchdog_task_timeout event not found in log"
        )

    # Restore WM to not pollute other tests
    wm.write({"last_orchestration_result": None})

    print(f"{_PASS} Test 7 — Watchdog detected task timeout and updated WorkingMemory")


# ──────────────────────────────────────────────────────────
# Test 8 — Watchdog detects cost limit and sets WorkingMemory flag
# ──────────────────────────────────────────────────────────

def test_8_watchdog_cost_limit():
    wm = WorkingMemory()
    wm.write({"cost_limit_reached": False})

    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
        log_path = f.name

    try:
        # Pre-exceed the cost limit
        tracker = CostTracker(
            daily_limit_usd=0.001,
            session_limit_usd=0.001,
            log_path=log_path,
        )
        tracker.record("setup", 1000, 1000, "claude-sonnet-4")

        assert not tracker.check_limits()["ok"], "tracker should be over limit"

        # Record log file state
        from config import LOG_DIR
        log_file = LOG_DIR / f"{time.strftime('%Y-%m-%d')}.jsonl"
        before_size = log_file.stat().st_size if log_file.exists() else 0

        watchdog = PrometheusWatchdog(working_memory=wm, cost_tracker=tracker)
        watchdog._check_cost_limits()

        # Verify WorkingMemory flag
        assert wm.read().get("cost_limit_reached") is True, (
            "cost_limit_reached should be True in WorkingMemory"
        )

        # Verify event logged
        if log_file.exists():
            new_content = log_file.read_text(encoding="utf-8")[before_size:]
            assert "watchdog_cost_limit" in new_content, (
                "watchdog_cost_limit event not found in log"
            )

        # Restore
        wm.write({"cost_limit_reached": False})
        print(f"{_PASS} Test 8 — Watchdog set cost_limit_reached in WorkingMemory")
    finally:
        os.unlink(log_path)


# ──────────────────────────────────────────────────────────
# Test 9 — launch.py starts all components without error (--no-voice --no-hud)
# ──────────────────────────────────────────────────────────

def test_9_launch_cycle():
    from launch import PrometheusApp
    from config import LOG_DIR

    log_file = LOG_DIR / f"{time.strftime('%Y-%m-%d')}.jsonl"
    before_size = log_file.stat().st_size if log_file.exists() else 0

    app = PrometheusApp(["--no-voice", "--no-hud"])
    app.start()

    # Give watchdog thread time to start
    time.sleep(0.3)

    # Verify watchdog is alive
    assert app.watchdog is not None, "watchdog should be initialized"
    assert app.watchdog.is_alive(), "watchdog thread should be alive"

    # Verify prometheus_start was logged
    if log_file.exists():
        new_content = log_file.read_text(encoding="utf-8")[before_size:]
        assert "prometheus_start" in new_content, (
            "prometheus_start event not found in log after startup"
        )

    # Record size before shutdown
    before_stop = log_file.stat().st_size if log_file.exists() else 0

    # Stop the app
    app.stop()

    # Verify prometheus_shutdown was logged
    time.sleep(0.2)
    if log_file.exists():
        shutdown_content = log_file.read_text(encoding="utf-8")[before_stop:]
        assert "prometheus_shutdown" in shutdown_content, (
            "prometheus_shutdown event not found in log after stop()"
        )

    assert not app.is_running(), "app should not be running after stop()"

    print(f"{_PASS} Test 9 — Full launch/shutdown cycle: all components started and stopped cleanly")


# ──────────────────────────────────────────────────────────
# Runner
# ──────────────────────────────────────────────────────────

def main():
    print("\n═══════════════════════════════════════════")
    print("  Prometheus Session 4 — Test Suite")
    print("═══════════════════════════════════════════\n")

    tests = [
        ("Test 1 — CostTracker records and accumulates", test_1_cost_tracker_record),
        ("Test 2 — CostTracker daily limit enforced", test_2_cost_limit_enforced),
        ("Test 3 — CostTracker daily reset", test_3_daily_reset),
        ("Test 4 — CodingAgent cost abort", test_4_coding_agent_cost_abort),
        ("Test 5 — LogViewer tail", test_5_log_viewer_tail),
        ("Test 6 — LogViewer summarize_session", test_6_log_viewer_summarize),
        ("Test 7 — Watchdog task timeout", test_7_watchdog_task_timeout),
        ("Test 8 — Watchdog cost limit flag", test_8_watchdog_cost_limit),
        ("Test 9 — launch.py full cycle", test_9_launch_cycle),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        ok = _run_test(name, fn)
        if ok:
            passed += 1
        else:
            failed += 1

    print()
    print("═══════════════════════════════════════════")
    print(f"  Results: {passed}/{len(tests)} passed", ("✅" if failed == 0 else "❌"))
    print("═══════════════════════════════════════════\n")

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()

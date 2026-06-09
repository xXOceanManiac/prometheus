#!/usr/bin/env python3
"""prometheus_trace_debug.py — Print readable PTT turn timelines from the daily log.

Usage:
    python3 tools/prometheus_trace_debug.py              # last real trace
    python3 tools/prometheus_trace_debug.py --last 3     # last 3 real traces
    python3 tools/prometheus_trace_debug.py --trace-id 20260608-143022-what-time-xx01

Real traces: non-empty trace_id, not containing "-test-", not starting with "readiness-".
Log field: "kind" (not "event").
Log path: ~/.jarvis/logs/YYYY-MM-DD.jsonl (today's date).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path


_LOG_DIR = Path.home() / ".jarvis" / "logs"

_TRACE_EVENTS = [
    "user_turn_started",
    "ptt_audio_capture_started",
    "realtime_audio_append_sent",
    "ptt_released",
    "ptt_audio_capture_stopped",
    "user_turn_commit_attempt",
    "user_turn_commit_skipped",
    "response_create_skipped_active",
    "user_turn_committed",
    "input_transcript_completed",
    "direct_tool_override",
    "tool_execute",
    "tool_result",
]

_ANSI = {
    "green": "\033[0;32m",
    "red": "\033[0;31m",
    "yellow": "\033[1;33m",
    "cyan": "\033[0;36m",
    "bold": "\033[1m",
    "reset": "\033[0m",
}


def _c(color: str, text: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"{_ANSI[color]}{text}{_ANSI['reset']}"


def _is_real_trace(tid: str) -> bool:
    if not tid:
        return False
    if "-test-" in tid:
        return False
    if tid.startswith("readiness-"):
        return False
    return True


def _load_log(log_date: str) -> list[dict]:
    log_path = _LOG_DIR / f"{log_date}.jsonl"
    if not log_path.exists():
        print(f"Log not found: {log_path}", file=sys.stderr)
        return []
    lines = []
    with log_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                lines.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return lines


def _extract_real_traces(lines: list[dict]) -> list[str]:
    seen: list[str] = []
    for rec in lines:
        if rec.get("kind") == "user_turn_started":
            tid = rec.get("trace_id", "")
            if _is_real_trace(tid) and tid not in seen:
                seen.append(tid)
    return seen


def _print_trace(trace_id: str, lines: list[dict]) -> None:
    events = [r for r in lines if r.get("trace_id") == trace_id]

    print(_c("bold", f"\n── Trace: {trace_id} "))

    def first(kind: str) -> dict | None:
        for r in events:
            if r.get("kind") == kind:
                return r
        return None

    def count(kind: str) -> int:
        return sum(1 for r in events if r.get("kind") == kind)

    def ts(kind: str) -> str:
        r = first(kind)
        return r.get("ts", "?") if r else ""

    def ok(msg: str) -> None:
        print(_c("green", f"  ✓ {msg}"))

    def fail(msg: str) -> None:
        print(_c("red", f"  ✗ {msg}"))

    def info(msg: str) -> None:
        print(_c("cyan", f"  → {msg}"))

    def warn(msg: str) -> None:
        print(_c("yellow", f"  ! {msg}"))

    # user_turn_started
    t = ts("user_turn_started")
    if t:
        ok(f"user_turn_started @ {t}")
    else:
        fail("user_turn_started NOT found")

    # ptt_audio_capture_started
    t = ts("ptt_audio_capture_started")
    if t:
        ok(f"ptt_audio_capture_started @ {t}")
    else:
        fail("ptt_audio_capture_started NOT found")

    # realtime_audio_append_sent
    n = count("realtime_audio_append_sent")
    if n > 0:
        last = [r for r in events if r.get("kind") == "realtime_audio_append_sent"][-1]
        chunks = last.get("chunks_so_far", "?")
        byt = last.get("bytes_so_far", "?")
        ok(f"realtime_audio_append_sent: {n} log events, last={chunks} chunks / {byt} bytes")
    else:
        warn("realtime_audio_append_sent: 0 events (very short audio?)")

    # ptt_released
    t = ts("ptt_released")
    if t:
        ok(f"ptt_released @ {t}")
    else:
        warn("ptt_released not found")

    # ptt_audio_capture_stopped
    r = first("ptt_audio_capture_stopped")
    if r:
        byt = r.get("bytes", "?")
        chnk = r.get("chunks", "?")
        ms = r.get("duration_ms", "?")
        ok(f"ptt_audio_capture_stopped: {byt} bytes / {chnk} chunks / {ms}ms")
    else:
        fail("ptt_audio_capture_stopped NOT found")

    # commit attempt / skip
    r_attempt = first("user_turn_commit_attempt")
    r_skip = first("user_turn_commit_skipped")
    r_active = first("response_create_skipped_active")

    if r_attempt:
        byt = r_attempt.get("bytes", "?")
        chnk = r_attempt.get("chunks", "?")
        ok(f"user_turn_commit_attempt: bytes={byt}, chunks={chnk}")
    elif r_skip:
        reason = r_skip.get("reason", "?")
        byt = r_skip.get("bytes", "?")
        fail(f"user_turn_commit_skipped: reason={reason}, bytes={byt}")
    elif r_active:
        ctx = r_active.get("context", "?")
        fail(f"response_create_skipped_active: context={ctx}")
    else:
        fail("No commit attempt or skip found")

    # user_turn_committed
    t = ts("user_turn_committed")
    if t:
        ok(f"user_turn_committed @ {t}")

    # input_transcript_completed
    r = first("input_transcript_completed")
    if r:
        txt = r.get("transcript", "")
        ok(f"input_transcript_completed: \"{txt}\"")
    else:
        fail("input_transcript_completed NOT found")

    # direct_tool_override
    r_dto = first("direct_tool_override")
    r_tex = first("tool_execute")
    r_res = first("tool_result")

    if r_dto:
        action = r_dto.get("action", "?")
        ok(f"direct_tool_override → action={action}")
    elif r_tex:
        action = r_tex.get("action", "?")
        info(f"tool_execute (LLM path) → action={action}")
    else:
        fail("No tool routing (direct_tool_override or tool_execute)")

    if r_res:
        status = r_res.get("status", "?")
        ok(f"tool_result: status={status}")

    # API errors
    api_errors = [r for r in events if r.get("kind") == "realtime_api_error"]
    for err in api_errors[:3]:
        msg = err.get("message") or err.get("error") or str(err)
        fail(f"realtime_api_error: {msg}")

    # Summary
    print()
    transcript = first("input_transcript_completed")
    has_routing = bool(r_dto or r_tex)
    has_result = bool(r_res)

    if transcript and has_routing and has_result:
        print(_c("green", _c("bold", "  TURN COMPLETE: transcript → routing → tool result ✓")))
    elif r_skip:
        print(_c("red", _c("bold", "  TURN DROPPED: insufficient audio")))
    elif r_active:
        print(_c("red", _c("bold", "  TURN DROPPED: _response_active not cleared")))
    elif not transcript:
        print(_c("yellow", _c("bold", "  TURN STALLED: audio committed but no transcript arrived")))
        print("  → Check Realtime API errors above and session payload log")
    else:
        print(_c("yellow", _c("bold", "  TURN PARTIAL: transcript arrived but no tool was routed")))
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--last", type=int, default=1, metavar="N", help="Show last N real traces (default: 1)")
    parser.add_argument("--trace-id", metavar="ID", help="Show a specific trace by ID")
    parser.add_argument("--date", default=date.today().isoformat(), metavar="YYYY-MM-DD", help="Log date (default: today)")
    args = parser.parse_args()

    lines = _load_log(args.date)
    if not lines:
        print(f"No log lines loaded for {args.date}.")
        sys.exit(0)

    if args.trace_id:
        _print_trace(args.trace_id, lines)
        return

    real_traces = _extract_real_traces(lines)
    if not real_traces:
        print(f"No real PTT turns found in {args.date} log.")
        print("(Filtered: empty trace_id, '-test-' traces, 'readiness-' probes)")
        sys.exit(0)

    selected = real_traces[-args.last:]
    for tid in selected:
        _print_trace(tid, lines)


if __name__ == "__main__":
    main()

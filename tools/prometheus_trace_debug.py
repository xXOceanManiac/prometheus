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
    "ptt_audio_captured",              # throttled every 5 chunks
    "ptt_released",
    "ptt_audio_capture_stopped",
    "user_turn_commit_attempt",
    "user_turn_commit_skipped",
    "response_create_skipped_active",
    "stt_transcription_started",
    "stt_transcription_completed",
    "stt_transcription_failed",
    "stt_empty_transcript",
    "stt_all_models_failed",
    "ptt_transcript_route_started",    # fired at entry of _handle_ptt_transcript
    "ptt_transcript_route_direct_tool",# fired when direct_tool_override matches
    "ptt_transcript_route_no_tool",    # fired when no override — falls to Realtime chat
    "ptt_transcript_route_failed",     # fired if _handle_ptt_transcript raises
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

    # ptt_audio_captured (logged every 5 chunks during local accumulation)
    n = count("ptt_audio_captured")
    if n > 0:
        last = [r for r in events if r.get("kind") == "ptt_audio_captured"][-1]
        chunks = last.get("chunks_so_far", "?")
        byt = last.get("bytes_so_far", "?")
        ok(f"ptt_audio_captured: {n} log events, last={chunks} chunks / {byt} bytes")
    else:
        warn("ptt_audio_captured: 0 events (very short audio or pre-pass12 log?)")

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

    # stt_transcription_started
    r_stt_start = first("stt_transcription_started")
    r_stt_done = first("stt_transcription_completed")
    r_stt_fail = first("stt_transcription_failed")
    r_stt_empty = first("stt_empty_transcript")
    r_stt_all_fail = first("stt_all_models_failed")

    if r_stt_start:
        model = r_stt_start.get("model", "?")
        ok(f"stt_transcription_started: model={model}")
    elif r_attempt:
        fail("stt_transcription_started NOT found")

    if r_stt_done:
        model = r_stt_done.get("model", "?")
        ms = r_stt_done.get("duration_ms", "?")
        preview = r_stt_done.get("preview", "")
        ok(f"stt_transcription_completed: model={model} ({ms}ms) preview=\"{preview}\"")
    elif r_stt_fail:
        err = r_stt_fail.get("error", "?")
        model = r_stt_fail.get("model", "?")
        fail(f"stt_transcription_failed: model={model} error={err}")
    elif r_stt_all_fail:
        models = r_stt_all_fail.get("models_tried", [])
        fail(f"stt_all_models_failed: tried {models}")
    elif r_stt_empty:
        warn(f"stt_empty_transcript: STT returned blank text")
    elif r_stt_start:
        fail("stt_transcription_completed NOT found (in-flight or crashed?)")

    # ptt_transcript_route_started
    r_route_started = first("ptt_transcript_route_started")
    r_route_failed = first("ptt_transcript_route_failed")
    if r_route_started:
        ok(f"ptt_transcript_route_started: len={r_route_started.get('transcript_len', '?')}")
    elif r_stt_done and not r_stt_fail and not r_stt_all_fail:
        fail("ptt_transcript_route_started NOT found — _handle_ptt_transcript not called after STT")
    if r_route_failed:
        fail(f"ptt_transcript_route_failed: {r_route_failed.get('error', '?')[:120]}")

    # input_transcript_completed
    r = first("input_transcript_completed")
    if r:
        txt = r.get("transcript", "")
        ok(f"input_transcript_completed: \"{txt}\"")
    elif r_stt_done:
        fail("input_transcript_completed NOT found — STT succeeded but routing bridge failed")
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

    has_stt = bool(r_stt_done)
    has_stt_fail = bool(r_stt_fail or r_stt_all_fail)
    has_route_failed = bool(r_route_failed)

    if transcript and has_routing and has_result:
        print(_c("green", _c("bold", "  TURN COMPLETE: audio → STT → tool → result ✓")))
    elif r_skip:
        print(_c("red", _c("bold", "  TURN DROPPED: insufficient audio (hold PTT longer)")))
    elif r_active:
        print(_c("red", _c("bold", "  TURN DROPPED: _response_active not cleared")))
    elif has_stt_fail:
        print(_c("red", _c("bold", "  TURN STALLED: STT failed — check API key and network")))
    elif has_route_failed:
        print(_c("red", _c("bold", "  TURN STALLED: STT succeeded but transcript routing failed")))
        print("  → Check ptt_transcript_route_failed error above")
    elif has_stt and not transcript:
        print(_c("red", _c("bold", "  TURN STALLED: STT succeeded but transcript routing failed")))
        print("  → ptt_transcript_route_started not fired; _handle_ptt_transcript may have crashed")
    elif not has_stt and r_stt_start:
        print(_c("yellow", _c("bold", "  TURN STALLED: STT started but did not complete")))
    elif not transcript:
        print(_c("yellow", _c("bold", "  TURN STALLED: no transcript (STT not started — audio too short?)")))
        print("  → Check stt_transcription_failed or stt_empty_transcript events above")
    elif not has_routing:
        print(_c("yellow", _c("bold", "  TURN PARTIAL: transcript arrived but no tool was routed")))
    else:
        print(_c("yellow", _c("bold", "  TURN PARTIAL: tool ran but no result logged")))
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--last", type=int, nargs="?", const=1, default=1, metavar="N",
                        help="Show last N real traces (default: 1); --last with no value shows 1")
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

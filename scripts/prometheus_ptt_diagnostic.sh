#!/usr/bin/env bash
# prometheus_ptt_diagnostic.sh — Summarise the latest PTT turn from today's log.
#
# Usage:
#   ./scripts/prometheus_ptt_diagnostic.sh
#   ./scripts/prometheus_ptt_diagnostic.sh --trace 20260608-143022-what-time-xx01
#   ./scripts/prometheus_ptt_diagnostic.sh --last N   # show last N turns (default: 1)
#
# Reads: ~/.jarvis/logs/YYYY-MM-DD.jsonl  (today's date)
# Log field: "kind" (not "event")
# Covers: PTT capture → standalone STT → tool routing → result
#
# Requires: jq ≥ 1.6

set -euo pipefail

LOG_DATE="$(date +%F)"
LOG_FILE="${JARVIS_ACTIVITY_LOG:-$HOME/.jarvis/logs/${LOG_DATE}.jsonl}"
LAST_N=1
TARGET_TRACE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --trace) TARGET_TRACE="$2"; shift 2 ;;
        --last)  LAST_N="$2";       shift 2 ;;
        *)       shift ;;
    esac
done

# ── Colour helpers ────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

ok()   { echo -e "${GREEN}  ✓${RESET} $*"; }
fail() { echo -e "${RED}  ✗${RESET} $*"; }
info() { echo -e "${CYAN}  →${RESET} $*"; }
warn() { echo -e "${YELLOW}  !${RESET} $*"; }

if ! command -v jq &>/dev/null; then
    echo "Error: jq is required. Install with: sudo apt install jq" >&2
    exit 1
fi

if [[ ! -f "$LOG_FILE" ]]; then
    warn "Log file not found: $LOG_FILE"
    warn "Start Prometheus first, then try a PTT command."
    exit 0
fi

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║           PROMETHEUS PTT DIAGNOSTIC                      ║${RESET}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════════╝${RESET}"
echo -e "  Log:  $LOG_FILE"
echo -e "  Date: $LOG_DATE"
echo ""

# ── Extract real turn trace IDs ───────────────────────────────────────────────
# Filter: non-empty, not test traces (-test-), not readiness probes (readiness-)
TRACE_IDS=$(
    jq -r 'select(.kind == "user_turn_started") | .trace_id // empty' "$LOG_FILE" 2>/dev/null \
    | grep -v '^$' || true
)
TRACE_IDS=$(echo "$TRACE_IDS" | grep -v -- '-test-' | grep -v '^readiness-' | tail -"$LAST_N" || true)

if [[ -n "$TARGET_TRACE" ]]; then
    TRACE_IDS="$TARGET_TRACE"
fi

if [[ -z "$TRACE_IDS" ]]; then
    warn "No real PTT turns found for $LOG_DATE in $LOG_FILE"
    warn "Trigger a PTT command and re-run this script."
    exit 0
fi

# ── Process each trace ────────────────────────────────────────────────────────
process_trace() {
    local trace_id="$1"

    # Pull all log lines for this trace
    local events
    events=$(grep -F "$trace_id" "$LOG_FILE" 2>/dev/null || true)

    if [[ -z "$events" ]]; then
        warn "No events found for trace $trace_id"
        return
    fi

    echo -e "${BOLD}── Trace: $trace_id ${RESET}"

    # ── PTT start ────────────────────────────────────────────────────────────
    local capture_started
    capture_started=$(echo "$events" | jq -r 'select(.kind == "ptt_audio_capture_started") | .ts // "?"' | head -1)
    if [[ -n "$capture_started" && "$capture_started" != "?" ]]; then
        ok "ptt_audio_capture_started @ $capture_started"
    else
        fail "ptt_audio_capture_started NOT found"
    fi

    # ── PTT released ─────────────────────────────────────────────────────────
    local ptt_released_ts
    ptt_released_ts=$(echo "$events" | jq -r 'select(.kind == "ptt_released") | .ts // "?"' | head -1)
    if [[ -n "$ptt_released_ts" && "$ptt_released_ts" != "?" ]]; then
        ok "ptt_released @ $ptt_released_ts"
    else
        warn "ptt_released not logged (only in ptt_release path)"
    fi

    # ── Local audio captured ─────────────────────────────────────────────────
    local stopped_bytes stopped_chunks stopped_ms
    stopped_bytes=$(echo "$events" | jq -r 'select(.kind == "ptt_audio_capture_stopped") | .bytes // 0' | head -1)
    stopped_chunks=$(echo "$events" | jq -r 'select(.kind == "ptt_audio_capture_stopped") | .chunks // 0' | head -1)
    stopped_ms=$(echo "$events" | jq -r 'select(.kind == "ptt_audio_capture_stopped") | .duration_ms // 0' | head -1)

    if [[ -n "$stopped_bytes" && "$stopped_bytes" != "0" ]]; then
        ok "local audio captured: ${stopped_bytes} bytes / ${stopped_chunks} chunks / ${stopped_ms}ms"
    else
        fail "ptt_audio_capture_stopped NOT found (or 0 bytes)"
    fi

    # ── Commit attempt vs skip ────────────────────────────────────────────────
    local commit_attempt commit_skipped
    commit_attempt=$(echo "$events" | jq -c 'select(.kind == "user_turn_commit_attempt")' | head -1)
    commit_skipped=$(echo "$events" | jq -c 'select(.kind == "user_turn_commit_skipped")' | head -1)

    if [[ -n "$commit_attempt" ]]; then
        local stt_mode
        stt_mode=$(echo "$commit_attempt" | jq -r '.stt_mode // "unknown"')
        ok "user_turn_commit_attempt: stt_mode=$stt_mode"
    elif [[ -n "$commit_skipped" ]]; then
        local skip_reason skip_bytes
        skip_reason=$(echo "$commit_skipped" | jq -r '.reason // "?"')
        skip_bytes=$(echo "$commit_skipped" | jq -r '.bytes // 0')
        fail "user_turn_commit_skipped: reason=$skip_reason bytes=$skip_bytes"
    fi

    # ── Standalone STT ───────────────────────────────────────────────────────
    local stt_started stt_model stt_completed stt_failed
    stt_started=$(echo "$events" | jq -r 'select(.kind == "stt_transcription_started") | .ts // ""' | head -1)
    stt_model=$(echo "$events" | jq -r 'select(.kind == "stt_transcription_started") | .model // ""' | head -1)
    stt_completed=$(echo "$events" | jq -r 'select(.kind == "stt_transcription_completed") | .ts // ""' | head -1)
    stt_failed=$(echo "$events" | jq -r 'select(.kind == "stt_transcription_failed") | .error // ""' | head -3)

    if [[ -n "$stt_started" ]]; then
        ok "stt_transcription_started @ $stt_started (model=$stt_model)"
    else
        fail "stt_transcription_started NOT found"
    fi

    if [[ -n "$stt_completed" ]]; then
        local stt_dur stt_preview
        stt_dur=$(echo "$events" | jq -r 'select(.kind == "stt_transcription_completed") | .duration_ms // "?"' | head -1)
        stt_preview=$(echo "$events" | jq -r 'select(.kind == "stt_transcription_completed") | .preview // ""' | head -1)
        ok "stt_transcription_completed @ $stt_completed (${stt_dur}ms) preview=\"$stt_preview\""
    else
        if [[ -n "$stt_failed" ]]; then
            fail "stt_transcription_failed: $stt_failed"
        else
            fail "stt_transcription_completed NOT found"
        fi
    fi

    # ── Transcript ───────────────────────────────────────────────────────────
    local transcript transcript_source
    transcript=$(echo "$events" | jq -r 'select(.kind == "input_transcript_completed") | .transcript // ""' | head -1)
    transcript_source=$(echo "$events" | jq -r 'select(.kind == "input_transcript_completed") | .source // ""' | head -1)
    if [[ -n "$transcript" ]]; then
        ok "input_transcript_completed (source=$transcript_source): \"$transcript\""
    else
        fail "input_transcript_completed NOT found"
    fi

    # ── Direct tool override ──────────────────────────────────────────────────
    local direct_override direct_action
    direct_override=$(echo "$events" | jq -c 'select(.kind == "direct_tool_override")' | head -1)
    direct_action=$(echo "$direct_override" | jq -r '.payload.action // ""' 2>/dev/null || true)
    if [[ -n "$direct_override" && -n "$direct_action" ]]; then
        ok "direct_tool_override → action=$direct_action"
    elif [[ -n "$transcript" ]]; then
        info "direct_tool_override not triggered (LLM/chat path or no match)"
    else
        fail "direct_tool_override NOT found"
    fi

    # ── Tool execution ────────────────────────────────────────────────────────
    local tool_exec_action tool_exec_status
    tool_exec_action=$(echo "$events" | jq -r 'select(.kind == "tool_execute") | .payload.action // ""' | head -1)
    tool_exec_status=$(echo "$events" | jq -r 'select(.kind == "tool_result") | .status // ""' | head -1)

    if [[ -n "$tool_exec_action" ]]; then
        ok "tool_execute: action=$tool_exec_action"
    elif [[ -n "$direct_override" ]]; then
        fail "tool_execute NOT found (direct_tool_override fired but tool_execute missing)"
    fi

    if [[ -n "$tool_exec_status" ]]; then
        ok "tool_result: status=$tool_exec_status"
    elif [[ -n "$tool_exec_action" ]]; then
        fail "tool_result NOT found"
    fi

    # ── Realtime API errors ───────────────────────────────────────────────────
    local api_errors
    api_errors=$(echo "$events" | jq -r 'select(.kind == "realtime_api_error") | .message // ""' | head -5)
    if [[ -n "$api_errors" ]]; then
        warn "realtime_api_error: $api_errors"
    fi

    # ── Summary ───────────────────────────────────────────────────────────────
    echo ""
    if [[ -n "$transcript" && (-n "$direct_action" || -n "$tool_exec_action") && -n "$tool_exec_status" ]]; then
        echo -e "${GREEN}${BOLD}  TURN COMPLETE: audio → STT → tool → result ✓${RESET}"
    elif [[ -n "$commit_skipped" ]]; then
        echo -e "${RED}${BOLD}  TURN DROPPED: insufficient audio (hold PTT longer)${RESET}"
    elif [[ -n "$stt_failed" ]]; then
        echo -e "${RED}${BOLD}  TURN STALLED: STT failed — check API key and network${RESET}"
    elif [[ -z "$transcript" ]]; then
        echo -e "${YELLOW}${BOLD}  TURN STALLED: STT did not produce a transcript${RESET}"
        echo "  → Check stt_transcription_failed events and API key"
    elif [[ -z "$tool_exec_action" ]]; then
        echo -e "${YELLOW}${BOLD}  TURN PARTIAL: transcript arrived but no tool was routed${RESET}"
    else
        echo -e "${YELLOW}${BOLD}  TURN PARTIAL: tool ran but no result status logged${RESET}"
    fi
    echo ""
}

# ── Run ───────────────────────────────────────────────────────────────────────
while IFS= read -r trace; do
    [[ -z "$trace" ]] && continue
    process_trace "$trace"
done <<< "$TRACE_IDS"

echo -e "${BOLD}══════════════════════════════════════════════════════════${RESET}"
echo ""
echo "  To follow live:  tail -f $LOG_FILE | jq '.'"
echo "  To search trace: $0 --trace <trace_id>"
echo "  Trace debugger:  python3 tools/prometheus_trace_debug.py --last 1"
echo ""

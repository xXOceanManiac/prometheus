#!/usr/bin/env bash
# prometheus_ptt_diagnostic.sh — Summarise the latest PTT turn from today's activity log.
#
# Usage:
#   ./scripts/prometheus_ptt_diagnostic.sh
#   ./scripts/prometheus_ptt_diagnostic.sh --trace 20260608-143022-what-time-xx01
#   ./scripts/prometheus_ptt_diagnostic.sh --last N   # show last N turns (default: 1)
#
# Reads: ~/.jarvis/activity.jsonl
# Outputs: per-turn trace showing audio bytes/chunks, commit/skip, transcript, tools.
#
# Requires: jq ≥ 1.6

set -euo pipefail

LOG_FILE="${JARVIS_ACTIVITY_LOG:-$HOME/.jarvis/activity.jsonl}"
TODAY="$(date +%Y%m%d)"
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
echo -e "  Date: $TODAY"
echo ""

# ── Extract all turn trace IDs for today ─────────────────────────────────────
# A "turn" starts with user_turn_started; extract unique trace IDs
TRACE_IDS=$(
    grep -F "$TODAY" "$LOG_FILE" 2>/dev/null \
    | jq -r 'select(.event == "user_turn_started") | .payload.trace_id // empty' \
    | tail -"$LAST_N"
)

if [[ -n "$TARGET_TRACE" ]]; then
    TRACE_IDS="$TARGET_TRACE"
fi

if [[ -z "$TRACE_IDS" ]]; then
    warn "No PTT turns found for today ($TODAY) in $LOG_FILE"
    warn "Trigger a PTT command and re-run this script."
    exit 0
fi

# ── Process each trace ────────────────────────────────────────────────────────
process_trace() {
    local trace_id="$1"

    # Pull all log lines for this trace (plus untraced lines we may need)
    local events
    events=$(grep -F "$trace_id" "$LOG_FILE" 2>/dev/null || true)

    if [[ -z "$events" ]]; then
        warn "No events found for trace $trace_id"
        return
    fi

    echo -e "${BOLD}── Trace: $trace_id ${RESET}"

    # ── PTT start ────────────────────────────────────────────────────────────
    local capture_started
    capture_started=$(echo "$events" | jq -r 'select(.event == "ptt_audio_capture_started") | .ts // "?"' | head -1)
    if [[ -n "$capture_started" && "$capture_started" != "?" ]]; then
        ok "ptt_audio_capture_started @ $capture_started"
    else
        fail "ptt_audio_capture_started NOT found"
    fi

    # ── Audio frames ─────────────────────────────────────────────────────────
    local append_count
    append_count=$(echo "$events" | jq -r 'select(.event == "realtime_audio_append_sent")' | wc -l)
    if [[ "$append_count" -gt 0 ]]; then
        local last_bytes
        last_bytes=$(echo "$events" | jq -r 'select(.event == "realtime_audio_append_sent") | .payload.bytes_so_far // 0' | tail -1)
        local last_chunks
        last_chunks=$(echo "$events" | jq -r 'select(.event == "realtime_audio_append_sent") | .payload.chunks_so_far // 0' | tail -1)
        ok "realtime_audio_append_sent: $append_count log events, last=$last_chunks chunks / $last_bytes bytes"
    else
        warn "realtime_audio_append_sent: 0 log events (audio may have been very short)"
    fi

    # ── Capture stopped ───────────────────────────────────────────────────────
    local stopped_bytes stopped_chunks stopped_ms
    stopped_bytes=$(echo "$events" | jq -r 'select(.event == "ptt_audio_capture_stopped") | .payload.bytes // 0' | head -1)
    stopped_chunks=$(echo "$events" | jq -r 'select(.event == "ptt_audio_capture_stopped") | .payload.chunks // 0' | head -1)
    stopped_ms=$(echo "$events" | jq -r 'select(.event == "ptt_audio_capture_stopped") | .payload.duration_ms // 0' | head -1)

    if [[ -n "$stopped_bytes" ]]; then
        ok "ptt_audio_capture_stopped: ${stopped_bytes} bytes / ${stopped_chunks} chunks / ${stopped_ms}ms"
    else
        fail "ptt_audio_capture_stopped NOT found"
    fi

    # ── Commit attempt vs skip ────────────────────────────────────────────────
    local commit_attempt
    commit_attempt=$(echo "$events" | jq -r 'select(.event == "user_turn_commit_attempt") | .payload' | head -1)
    local commit_skipped
    commit_skipped=$(echo "$events" | jq -r 'select(.event == "user_turn_commit_skipped") | .payload' | head -1)
    local skipped_active
    skipped_active=$(echo "$events" | jq -r 'select(.event == "response_create_skipped_active") | .payload.context // ""' | head -1)

    if [[ -n "$commit_attempt" ]]; then
        ok "user_turn_commit_attempt: $(echo "$commit_attempt" | jq -c '.')"
    elif [[ -n "$commit_skipped" ]]; then
        fail "user_turn_commit_skipped: $(echo "$commit_skipped" | jq -c '.')"
    elif [[ -n "$skipped_active" ]]; then
        fail "response_create_skipped_active: context=$skipped_active (interrupt() not clearing _response_active?)"
    else
        fail "No commit attempt, skip, or active-response block found — turn may have been dropped"
    fi

    # ── Transcript ───────────────────────────────────────────────────────────
    local transcript
    transcript=$(echo "$events" | jq -r 'select(.event == "input_transcript_completed") | .payload.transcript // ""' | head -1)
    if [[ -n "$transcript" ]]; then
        ok "input_transcript_completed: \"$transcript\""
    else
        fail "input_transcript_completed NOT found — transcription never arrived"
    fi

    # ── Tool routing ──────────────────────────────────────────────────────────
    local direct_override
    direct_override=$(echo "$events" | jq -r 'select(.event == "direct_tool_override") | .payload.action // ""' | head -1)
    local tool_execute
    tool_execute=$(echo "$events" | jq -r 'select(.event == "tool_execute") | .payload.action // .payload.payload.action // ""' | head -1)
    local tool_result
    tool_result=$(echo "$events" | jq -r 'select(.event == "tool_result") | .payload.status // ""' | head -1)

    if [[ -n "$direct_override" ]]; then
        ok "direct_tool_override → action=$direct_override"
    elif [[ -n "$tool_execute" ]]; then
        info "tool_execute (LLM path) → action=$tool_execute"
    else
        fail "No tool routing found (direct_tool_override or tool_execute)"
    fi

    if [[ -n "$tool_result" ]]; then
        ok "tool_result: status=$tool_result"
    fi

    # ── Realtime API errors ───────────────────────────────────────────────────
    local api_errors
    api_errors=$(echo "$events" | jq -r 'select(.event == "realtime_api_error") | .payload.error // ""' | head -5)
    if [[ -n "$api_errors" ]]; then
        fail "realtime_api_error: $api_errors"
    fi

    # ── Summary ───────────────────────────────────────────────────────────────
    echo ""
    if [[ -n "$transcript" && (-n "$direct_override" || -n "$tool_execute") && -n "$tool_result" ]]; then
        echo -e "${GREEN}${BOLD}  TURN COMPLETE: transcript → routing → tool result ✓${RESET}"
    elif [[ -n "$commit_skipped" ]]; then
        echo -e "${RED}${BOLD}  TURN DROPPED: insufficient audio (check mic and PTT hold time)${RESET}"
    elif [[ -n "$skipped_active" ]]; then
        echo -e "${RED}${BOLD}  TURN DROPPED: interrupt() did not clear _response_active${RESET}"
    elif [[ -z "$transcript" ]]; then
        echo -e "${YELLOW}${BOLD}  TURN STALLED: audio committed but no transcript arrived${RESET}"
        echo "  → Check Realtime API connection and audio quality"
    else
        echo -e "${YELLOW}${BOLD}  TURN PARTIAL: transcript arrived but no tool was routed${RESET}"
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
echo ""

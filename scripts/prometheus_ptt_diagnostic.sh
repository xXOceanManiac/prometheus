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
# Outputs: per-turn trace showing audio bytes/chunks, commit/skip, transcript, tools.
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
    | grep -v '^$' \
    | grep -v -- '-test-' \
    | grep -v '^readiness-' \
    | tail -"$LAST_N"
)

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
        warn "ptt_released not found (added in Pass 11; check main.py _commit_turn)"
    fi

    # ── Audio frames ─────────────────────────────────────────────────────────
    local append_count
    append_count=$(echo "$events" | jq -r 'select(.kind == "realtime_audio_append_sent")' | wc -l)
    if [[ "$append_count" -gt 0 ]]; then
        local last_bytes last_chunks
        last_bytes=$(echo "$events" | jq -r 'select(.kind == "realtime_audio_append_sent") | .bytes_so_far // 0' | tail -1)
        last_chunks=$(echo "$events" | jq -r 'select(.kind == "realtime_audio_append_sent") | .chunks_so_far // 0' | tail -1)
        ok "realtime_audio_append_sent: $append_count log events, last=$last_chunks chunks / $last_bytes bytes"
    else
        warn "realtime_audio_append_sent: 0 log events (audio may have been very short)"
    fi

    # ── Capture stopped ───────────────────────────────────────────────────────
    local stopped_bytes stopped_chunks stopped_ms
    stopped_bytes=$(echo "$events" | jq -r 'select(.kind == "ptt_audio_capture_stopped") | .bytes // 0' | head -1)
    stopped_chunks=$(echo "$events" | jq -r 'select(.kind == "ptt_audio_capture_stopped") | .chunks // 0' | head -1)
    stopped_ms=$(echo "$events" | jq -r 'select(.kind == "ptt_audio_capture_stopped") | .duration_ms // 0' | head -1)

    if [[ -n "$stopped_bytes" && "$stopped_bytes" != "0" ]]; then
        ok "ptt_audio_capture_stopped: ${stopped_bytes} bytes / ${stopped_chunks} chunks / ${stopped_ms}ms"
    else
        fail "ptt_audio_capture_stopped NOT found (or 0 bytes)"
    fi

    # ── Commit attempt vs skip ────────────────────────────────────────────────
    local commit_attempt commit_skipped skipped_active
    commit_attempt=$(echo "$events" | jq -c 'select(.kind == "user_turn_commit_attempt")' | head -1)
    commit_skipped=$(echo "$events" | jq -c 'select(.kind == "user_turn_commit_skipped")' | head -1)
    skipped_active=$(echo "$events" | jq -r 'select(.kind == "response_create_skipped_active") | .context // ""' | head -1)

    if [[ -n "$commit_attempt" ]]; then
        ok "user_turn_commit_attempt: $(echo "$commit_attempt" | jq -c '{bytes,chunks,duration_ms} // .')"
    elif [[ -n "$commit_skipped" ]]; then
        fail "user_turn_commit_skipped: $(echo "$commit_skipped" | jq -c '.')"
    elif [[ -n "$skipped_active" ]]; then
        fail "response_create_skipped_active: context=$skipped_active"
    else
        fail "No commit attempt, skip, or active-response block found"
    fi

    # ── Transcript ───────────────────────────────────────────────────────────
    local transcript
    transcript=$(echo "$events" | jq -r 'select(.kind == "input_transcript_completed") | .transcript // ""' | head -1)
    if [[ -n "$transcript" ]]; then
        ok "input_transcript_completed: \"$transcript\""
    else
        fail "input_transcript_completed NOT found — transcription never arrived"
    fi

    # ── Tool routing ──────────────────────────────────────────────────────────
    local direct_override tool_exec tool_result
    direct_override=$(echo "$events" | jq -r 'select(.kind == "direct_tool_override") | .action // ""' | head -1)
    tool_exec=$(echo "$events" | jq -r 'select(.kind == "tool_execute") | .action // ""' | head -1)
    tool_result=$(echo "$events" | jq -r 'select(.kind == "tool_result") | .status // ""' | head -1)

    if [[ -n "$direct_override" ]]; then
        ok "direct_tool_override → action=$direct_override"
    elif [[ -n "$tool_exec" ]]; then
        info "tool_execute (LLM path) → action=$tool_exec"
    else
        fail "No tool routing found (direct_tool_override or tool_execute)"
    fi

    if [[ -n "$tool_result" ]]; then
        ok "tool_result: status=$tool_result"
    fi

    # ── Realtime API errors ───────────────────────────────────────────────────
    local api_errors
    api_errors=$(echo "$events" | jq -r 'select(.kind == "realtime_api_error") | .error // .message // ""' | head -5)
    if [[ -n "$api_errors" ]]; then
        fail "realtime_api_error: $api_errors"
    fi

    # ── Session payload audit ─────────────────────────────────────────────────
    local payload_blocked
    payload_blocked=$(grep -F "realtime_payload_blocked" "$LOG_FILE" 2>/dev/null | jq -r '.reason // ""' | head -3)
    if [[ -n "$payload_blocked" ]]; then
        fail "realtime_payload_blocked: $payload_blocked"
    fi

    local unknown_param
    unknown_param=$(grep -F "unknown_parameter" "$LOG_FILE" 2>/dev/null | jq -r '.message // ""' | head -3)
    if [[ -n "$unknown_param" ]]; then
        fail "unknown_parameter error: $unknown_param"
    fi

    # ── Summary ───────────────────────────────────────────────────────────────
    echo ""
    if [[ -n "$transcript" && (-n "$direct_override" || -n "$tool_exec") && -n "$tool_result" ]]; then
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

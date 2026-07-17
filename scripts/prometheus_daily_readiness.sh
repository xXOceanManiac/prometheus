#!/usr/bin/env bash
# prometheus_daily_readiness.sh — Daily readiness gate runner for Prometheus
#
# Usage:
#   ./scripts/prometheus_daily_readiness.sh
#   ./scripts/prometheus_daily_readiness.sh --verbose
#   ./scripts/prometheus_daily_readiness.sh --gate boot_config
#
# Each gate answers one yes/no question: "Can I trust this for daily use?"
# Gates are scored 0 or 1. Total score / total gates = readiness rating 0–5.
# (Score ≥ 4.5 → 5-star, ≥ 3.5 → 4-star, etc.)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
REPORT_DIR="$PROJECT_ROOT/../reports"

PYTHON="/usr/bin/python3.12"
SITE_PACKAGES="$PROJECT_ROOT/.venv/lib/python3.12/site-packages"
PYTHONPATH_EXPORT="$PROJECT_ROOT:$SITE_PACKAGES"

export PYTHONPATH="$PYTHONPATH_EXPORT"

VERBOSE="${1:-}"
SINGLE_GATE="${2:-}"

GATE_PASS=0
GATE_TOTAL=0
FAILED_GATES=()
GATE_RESULTS=()

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

# ── Run a pytest target and return 0/1 ───────────────────────────────────────
run_gate() {
    local gate_name="$1"
    local pytest_args="${2:-}"
    local label="${3:-$gate_name}"

    GATE_TOTAL=$((GATE_TOTAL + 1))

    if [[ -n "$SINGLE_GATE" && "$gate_name" != "$SINGLE_GATE" ]]; then
        return 0
    fi

    echo ""
    echo -e "${BOLD}── Gate: $label ${RESET}"

    local output
    local exit_code=0
    # shellcheck disable=SC2086
    output=$("$PYTHON" -m pytest $pytest_args -q --tb=no --no-header 2>&1) || exit_code=$?

    if [[ $exit_code -eq 0 ]]; then
        GATE_PASS=$((GATE_PASS + 1))
        local summary
        summary=$(echo "$output" | tail -1)
        ok "$summary"
        GATE_RESULTS+=("PASS|$label")
    else
        fail "$label FAILED"
        if [[ "$VERBOSE" == "--verbose" || "$VERBOSE" == "-v" ]]; then
            echo "$output" | grep -E "FAILED|ERROR|assert" | head -20
        else
            echo "$output" | tail -3
        fi
        FAILED_GATES+=("$label")
        GATE_RESULTS+=("FAIL|$label")
    fi
}

# ── Header ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║         PROMETHEUS DAILY READINESS CHECK                 ║${RESET}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════════╝${RESET}"
echo -e "  Date: $(date '+%Y-%m-%d %H:%M %Z')"
echo -e "  Project: $PROJECT_ROOT"

cd "$PROJECT_ROOT"

# ── Gate 1: Boot / Config ─────────────────────────────────────────────────────
run_gate "boot_config" \
    "tests/acceptance/test_daily_readiness.py::TestGateBootConfig tests/test_import_integrity.py" \
    "1. Boot / Config (imports clean, required keys present)"

# ── Gate 2: Vault / Memory ────────────────────────────────────────────────────
run_gate "vault_memory" \
    "tests/acceptance/test_daily_readiness.py::TestGateVaultMemory tests/test_vault_config.py" \
    "2. Vault / Memory (memory subsystem stable, vault accessible)"

# ── Gate 3: Trace Observability ───────────────────────────────────────────────
run_gate "trace_observability" \
    "tests/acceptance/test_daily_readiness.py::TestGateTraceObservability tests/acceptance/test_trace_id_propagation.py tests/test_trace_ids.py" \
    "3. Trace Observability (every tool call logged with trace_id)"

# ── Gate 4: Tool Truth Contract ───────────────────────────────────────────────
run_gate "tool_truth" \
    "tests/acceptance/test_daily_readiness.py::TestGateToolTruth tests/acceptance/test_no_false_success_claims.py tests/test_tool_truth_contract.py" \
    "4. Tool Truth Contract (ok ≠ verified, accepted_unverified never claims done)"

# ── Gate 5: HA Verification ───────────────────────────────────────────────────
run_gate "ha_verification" \
    "tests/acceptance/test_daily_readiness.py::TestGateHAVerification tests/test_ha_verification.py" \
    "5. HA Verification (state mismatches never produce verified_success)"

# ── Gate 6: Time Correctness ──────────────────────────────────────────────────
run_gate "time_correctness" \
    "tests/acceptance/test_daily_readiness.py::TestGateTimeCorrectness tests/test_pass7_time_browser.py" \
    "6. Time Correctness (time phrases route deterministically, date in response)"

# ── Gate 7: Calendar Routines ─────────────────────────────────────────────────
run_gate "calendar_routines" \
    "tests/acceptance/test_daily_readiness.py::TestGateCalendarRoutines tests/test_calendar_event_triggers.py" \
    "7. Calendar Routines (trigger engine runs without live Google API)"

# ── Gate 8: Morning Routine ───────────────────────────────────────────────────
run_gate "morning_routine" \
    "tests/acceptance/test_daily_readiness.py::TestGateMorningRoutine tests/test_morning_routine.py" \
    "8. Morning Routine (HA calls run even when speech/Realtime fails)"

# ── Gate 9: HUD State Writer ──────────────────────────────────────────────────
run_gate "hud_state" \
    "tests/acceptance/test_daily_readiness.py::TestGateHUDState tests/test_hud_state_writer.py" \
    "9. HUD State Writer (visual state file written atomically)"

# ── Gate 10: Reactive By Default ──────────────────────────────────────────────
run_gate "reactive_by_default" \
    "tests/acceptance/test_daily_readiness.py::TestGateReactiveByDefault" \
    "10. Reactive By Default (no proactive speech or model-call machinery)"

# ── Gate 11: False Success Prevention ────────────────────────────────────────
run_gate "false_success_prevention" \
    "tests/acceptance/test_daily_readiness.py::TestGateFalseSuccessPrevention tests/test_truthful_wording.py tests/test_verification.py" \
    "11. False Success Prevention (accepted_unverified/verified_success never swapped)"

# ── Score ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}══════════════════════════════════════════════════════════${RESET}"

STARS=0
if   [[ $GATE_PASS -eq $GATE_TOTAL ]]; then STARS=5
elif (( GATE_PASS * 10 >= GATE_TOTAL * 9 )); then STARS=4
elif (( GATE_PASS * 10 >= GATE_TOTAL * 7 )); then STARS=3
elif (( GATE_PASS * 10 >= GATE_TOTAL * 5 )); then STARS=2
elif (( GATE_PASS * 10 >= GATE_TOTAL * 3 )); then STARS=1
fi

STAR_STR=""
for ((i=0; i<STARS; i++)); do STAR_STR+="★"; done
for ((i=STARS; i<5; i++)); do STAR_STR+="☆"; done

echo ""
echo -e "  ${BOLD}Readiness Score: ${GATE_PASS}/${GATE_TOTAL} gates  ${STAR_STR}${RESET}"
echo ""

if [[ ${#FAILED_GATES[@]} -eq 0 ]]; then
    echo -e "${GREEN}${BOLD}  All gates passed. Prometheus is ready for daily use.${RESET}"
else
    echo -e "${RED}${BOLD}  Failed gates:${RESET}"
    for g in "${FAILED_GATES[@]}"; do
        echo -e "${RED}    ✗ $g${RESET}"
    done
    echo ""
    echo -e "${YELLOW}  See the full report at:${RESET}"
    echo -e "  $REPORT_DIR/prometheus_daily_readiness.md"
fi

echo ""
echo -e "${BOLD}══════════════════════════════════════════════════════════${RESET}"
echo ""

# Exit non-zero if any gates failed
[[ ${#FAILED_GATES[@]} -eq 0 ]] && exit 0 || exit 1

# Calendar Execution and Show Logs Fix — Session 3 Report

**Date:** 2026-05-14  
**Session:** 3

---

## Summary

Two major capability areas were implemented and verified in this session:

1. **Show Logs root-cause fix** — logs now display immediately when the user asks, without requiring a second question
2. **Lumen Calendar Write Executor** — strict approval-gated pipeline for executing Lumen calendar proposals against Google Calendar

---

## Part A: Show Logs Fix

### Root Cause

`show_logs` was already in `FOLLOWUP_ACTIONS`, had a handler in `tools.py`, and had response synthesis. The missing piece: **no direct intent override phrases** in `prometheus/core/intent_overrides.py`. Without these, when the user said "show logs", the LLM responded conversationally instead of routing to `desktop_action`.

### Changes

**`prometheus/core/intent_overrides.py`**
- Added `_SHOW_LOGS_PHRASES` tuple (19 phrases covering "show logs", "check logs", "view logs", "recent logs", "pull up the logs", "what happened recently", etc.)
- Added resolver block before `_WEB_SEARCH_KEYWORDS` check — returns `{"action": "show_logs", "request_text": transcript}`

**`tools.py`** (`show_logs` handler)
- Updated return data to include all fields expected by the synthesizer: `logs_dir`, `files_found`, `latest_file`, `lines_returned`, `entries`, `message`

**`prometheus/execution/response_synthesizer.py`**
- Added `_show_logs()` formatter: reads up to 15 most recent entries, highlights errors/warnings, instructs concise natural delivery
- Added `is_synthesized_action()` function that unifies calendar, executor, and show_logs checks
- Replaced separate per-action elif blocks in `realtime_client.py` with single `is_synthesized_action` call

**`prometheus/infra/log_viewer.py`**
- Added `_main()` CLI with `--latest`, `--list`, `--tail FILENAME [N]` subcommands

### Verification

```
$ python -m prometheus.infra.log_viewer --latest
[returns real entries from ~/.jarvis/logs/2026-05-14.jsonl]
```

Audit checks: `show_logs:direct_intent_override` ✓, `show_logs:check_logs_phrase` ✓, `show_logs:synthesizer_handles_show_logs` ✓

---

## Part B: Lumen Calendar Write Executor

### Architecture

Four-stage pipeline (all guarded):

```
reviewed/lumen_calendar/   →   approved/lumen_calendar/
      (dry-run review)              (explicit approval)
           ↓                              ↓
   execute_approved_calendar_request()
           ↓                    ↓
completed/lumen_calendar/   failed/lumen_calendar/
```

### New Files

**`prometheus/agents/lumen_calendar_executor.py`**  
Public API:
- `list_reviewed_calendar_requests()` — lists requests ready for approval
- `load_reviewed_calendar_request(request_id)` — loads a reviewed proposal
- `approve_calendar_request(request_id, approved_by)` — writes approval record after 6-point validation
- `execute_approved_calendar_request(request_id)` — executes with 7 precondition checks
- `execute_calendar_operation(op, config, service)` — routes to create/update/delete adapter
- `write_calendar_execution_result(request_id, result)` — writes to COMPLETED or FAILED
- `get_request_status(request_id)` — returns current state across all dirs

**`prometheus/infra/paths.py`** additions:
- `APPROVED_LUMEN_DIR`, `COMPLETED_LUMEN_DIR`, `FAILED_LUMEN_DIR`
- `ensure_lumen_executor_dirs()`

### Approval Preconditions (all must pass)

1. Reviewed result exists (`reviewed/lumen_calendar/reviewed_{id}.json`)
2. `all_dry_run: true` in reviewed result
3. No failures in reviewed result (`all_success: true` or no errors)
4. Pending proposal exists with operations
5. All operations have `requires_prometheus_approval: true`
6. All operations have `dry_run: true` in the reviewed result

Approval record written to `approved/lumen_calendar/approved_{id}.json`.

### Execution Preconditions (all must pass before any API call)

1. Approval record exists and `approved: true`
2. `all_dry_run: true` in the reviewed result
3. Pending proposal exists with operations
4. `config.enabled = true` (`GOOGLE_CALENDAR_ENABLED=true`)
5. `config.dry_run = false` (`GOOGLE_CALENDAR_DRY_RUN` must be absent or false)
6. All operations pass `_validate_operations()` (suspicious key check, required fields, supported type)
7. Service builds successfully

All operations validated before any execute. Any single failure writes to FAILED, no partial execution.

### Security

- `_SUSPICIOUS_KEYS` blocklist: `command`, `shell`, `subprocess`, `exec`, `eval`, `url`, `token`, `api_key`, `home_assistant`, `ha_service`
- No subprocess, no shell execution, no HA integration
- No `--execute-all` command
- `calendar_execute_approved_request` tool requires `confirmed=True` in payload
- Executable write types limited to: `create_event`, `update_event`, `delete_event`
- Non-write types (`read_events`, `find_availability`, `suggest_schedule_change`) are skipped with explanation

### Tool Registry

| Tool | Risk | Required Slots |
|------|------|----------------|
| `calendar_list_reviewed_requests` | none | — |
| `calendar_approve_request` | medium | `request_id` |
| `calendar_execute_approved_request` | high | `request_id`, `confirmed` |

All three are in `FOLLOWUP_ACTIONS` and handled by `response_synthesizer.py`.

### CLI

```bash
# List requests awaiting approval
python -m prometheus.agents.lumen_calendar_executor --list-reviewed

# Approve a reviewed request
python -m prometheus.agents.lumen_calendar_executor --approve req-ed21a337167d

# Execute an approved request (blocked in dry-run mode)
python -m prometheus.agents.lumen_calendar_executor --execute-approved req-ed21a337167d

# Check status of any request
python -m prometheus.agents.lumen_calendar_executor --status req-ed21a337167d
```

### Smoke Test Results

```
--list-reviewed    → {"ok": true, "count": 13, "requests": [...]}
--approve          → {"ok": true, "approved": true, "request_id": "req-ed21a337167d", ...}
--execute-approved → {"success": false, "reason": "Calendar execution is blocked: 
                      GOOGLE_CALENDAR_ENABLED is not true..."}  (exit 1, correctly blocked)
```

---

## Test Results

| Suite | Result |
|-------|--------|
| `pytest` (full suite) | **817 passed**, 1 skipped |
| `audit_prometheus.py` | **241/241 passed** |
| `score_contextual_intent.py` | **100%** all categories |
| `score_workflows.py` | **100%** all targets met |

### New Tests (`tests/test_lumen_calendar_executor.py`)

37 tests across 6 classes:
- `TestModuleImport` — imports and function presence
- `TestListReviewed` — empty dir, missing dir, returns list of dicts
- `TestApproveCalendarRequest` — 8 cases including all approval guard failures + success path
- `TestExecuteApproved` — 7 cases including all execution guard failures + adapter call verification
- `TestExecuteCalendarOperation` — create/update/delete routing + read/find skipping
- `TestSafety` — no subprocess, no HA calls, no execute-all, suspicious key blocking, risk classification, confirmed requirement

---

## Constraints Upheld

- No passive autonomy added
- No Home Assistant calendar coupling
- Lumen cannot call Google Calendar directly
- Approval requirements not weakened
- Prometheus safety/risk layers not bypassed
- No subprocess/shell for Show Logs
- No token/credential secrets exposed
- No live calendar writes during tests (GOOGLE_CALENDAR_DRY_RUN=true blocks all)

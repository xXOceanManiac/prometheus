# Natural-Language Calendar Creation — Implementation Report

**Date:** 2026-05-14  
**Feature:** NL calendar creation with explicit confirmation gate  
**Status:** Complete — 962 tests passing, 265/265 audit checks passing

---

## What Was Built

A full natural-language calendar creation flow that allows the user to say things like:

- "Schedule a focus block tomorrow at 2"
- "Add a workout this afternoon"
- "Block off time on Friday morning for deep work"
- "Put a standup on my calendar at 10am"

Prometheus parses the request, optionally searches for availability (when time is a window like "morning" or "afternoon"), proposes the event to the user in a single sentence, and only executes the calendar write after explicit user confirmation.

---

## Hard Constraints Enforced

Every constraint from the original spec is enforced:

| Constraint | Enforced |
|---|---|
| No passive scheduling | ✓ `parse_and_propose()` never writes to calendar |
| No recurring events | ✓ Only `create_event` operation type in NL flow |
| No calendar update/delete | ✓ Module only produces `create_event` operations |
| No Home Assistant | ✓ Audit check: `calendar_create_flow:no_home_assistant_calls` |
| No direct GCal calls | ✓ Audit check: `calendar_create_flow:no_direct_gcal_calls` |
| No bypass of approved executor | ✓ Routes through `execute_approved_calendar_request()` |
| Requires explicit confirmation | ✓ File-based confirmation with 24-hour expiry |
| DRY_RUN=false required | ✓ Executor blocks with clear message |
| No passive autonomy | ✓ `parse_and_propose` does not call executor |

---

## Architecture

### Files Added

**`prometheus/agents/calendar_create_flow.py`**  
Core module. No direct Google Calendar API calls. Routes through the existing approved executor pipeline.

Functions:
- `parse_calendar_create_request(user_message, now)` → structured draft dict
- `parse_and_propose(user_request)` → pending confirmation or needs_input/no_availability
- `has_pending_calendar_confirmation()` → bool (filesystem check, used by intent overrides)
- `get_most_recent_pending_confirmation()` → Optional[dict]
- `confirm_pending_calendar_confirmation()` → writes reviewed + approval files, calls executor
- `cancel_pending_calendar_confirmation()` → marks pending file as canceled

**`tests/test_calendar_create_flow.py`**  
111 tests across 15 test classes.

### Files Modified

| File | Change |
|---|---|
| `prometheus/infra/paths.py` | Added `PENDING_CALENDAR_CONFIRMATIONS_DIR`, `ensure_calendar_confirmation_dir()` |
| `tools.py` | Added 3 actions to `ACTION_ENUM`, `_execute_calendar_create_flow()` method, dispatch |
| `prometheus/core/intent_overrides.py` | Added `_CALENDAR_CREATE_PHRASES`, `_CALENDAR_CONFIRM_PHRASES`, `_CALENDAR_CANCEL_PHRASES`; routing in `resolve_direct_intent()` |
| `prometheus/core/tool_followups.py` | Added 3 new actions to `FOLLOWUP_ACTIONS` |
| `prometheus/execution/response_synthesizer.py` | Added `_CALENDAR_CREATE_ACTIONS`, `_calendar_create_flow()` formatter, updated `is_synthesized_action()` |
| `prometheus/execution/tool_capability_registry.py` | Added 3 `ToolCapability` entries |
| `tests/audit_prometheus.py` | Added `section_calendar_create_flow()` (Section 16, 20 checks) |

---

## Flow Diagram

```
User: "Schedule a focus block tomorrow at 2"
        ↓
resolve_direct_intent() → detects "schedule a " → calendar_create_proposal
        ↓
_execute_calendar_create_flow("calendar_create_proposal", {...})
        ↓
parse_and_propose("schedule a focus block tomorrow at 2")
  - parse: title="Focus Block", date="2026-05-15", time=14:00, duration=90min
  - build operation: {operation_type: "create_event", dry_run: true, requires_prometheus_approval: true}
  - write pending confirmation file (24h expiry)
  - return: {status: "pending", human_summary: "I can add 'Focus Block' tomorrow from 2:00–3:30 PM. Confirm?"}
        ↓
_calendar_create_flow("calendar_create_proposal", data)
  → "Say exactly: 'I can add 'Focus Block' tomorrow from 2:00–3:30 PM. Confirm?'"
        ↓
User: "yes"   (pending confirmation exists in filesystem)
        ↓
resolve_direct_intent() → has_pending_calendar_confirmation() → True → calendar_confirm_create
        ↓
confirm_pending_calendar_confirmation()
  - load pending confirmation
  - write reviewed file (REVIEWED_LUMEN_DIR/reviewed_req-nlcal-*.json)
  - write approval file (APPROVED_LUMEN_DIR/approved_req-nlcal-*.json)
  - call execute_approved_calendar_request(request_id)
    → checks GOOGLE_CALENDAR_ENABLED=true (blocks if false)
    → checks GOOGLE_CALENDAR_DRY_RUN=false (blocks if true, with clear message)
    → validates operation
    → calls Google Calendar API
  - mark pending file as "confirmed"
  - return {success: true, title: "Focus Block", start_time: "2026-05-15T14:00:00"}
        ↓
"Confirmed. 'Focus Block' has been added to your calendar at 14:00."
```

---

## Parsing Logic

### Title extraction
1. "called/named/titled X" pattern (explicit naming)
2. "block off ... for X" pattern
3. Verb + article stripping, stop at date/time keywords

### Date resolution
- "today", "tomorrow" → current or next date
- Weekday name → nearest upcoming occurrence (never same day)
- "next monday" → next Monday (even if today is Monday+7)

### Time resolution
- Bare hour heuristic: 1–7 → PM, 8–12 → AM
- Explicit am/pm overrides heuristic
- Window keywords (morning/afternoon/evening/tonight) → availability search

### Duration defaults by title keyword
- focus/focus block/deep work/heads down → 90 min
- workout/exercise/gym/run/jog → 60 min
- standup/check-in/sync → 30 min
- everything else → 60 min

### Availability search (window-based times)
When time is a window (e.g. "afternoon"), calls `calendar_find_free_blocks()` with the window's hour range (12:00–17:00 for afternoon), takes the first free block ≥ duration_minutes.

---

## Pending Confirmation System

Files stored at: `RUNTIME_ROOT/pending/calendar_confirmations/pending_cal_confirm_{id}.json`

Structure:
```json
{
  "confirmation_id": "hex16",
  "created_at": "ISO",
  "expires_at": "ISO (+24h)",
  "user_request": "original user message",
  "draft": {...parsed fields...},
  "proposed_operation": {
    "operation_type": "create_event",
    "title": "...",
    "start_time": "YYYY-MM-DDTHH:MM:SS",
    "end_time": "YYYY-MM-DDTHH:MM:SS",
    "calendar_id": "primary",
    "requires_prometheus_approval": true,
    "dry_run": true
  },
  "human_summary": "I can add '...' ... Confirm?",
  "status": "pending"
}
```

Status transitions: `pending` → `confirmed` | `failed` | `canceled`

---

## Intent Override Routing

Confirm/cancel phrases are **context-aware** — they only route to the calendar flow when `has_pending_calendar_confirmation()` returns True (filesystem check). Without a pending confirmation, "yes" and "cancel" fall through to the LLM normally.

This prevents accidental interception of unrelated "yes" responses.

---

## Test Results

| Suite | Count | Result |
|---|---|---|
| `test_calendar_create_flow.py` | 111 tests | All pass |
| Full test suite | 962 tests, 1 skipped | All pass |
| Audit | 265/265 checks | All pass |
| `score_contextual_intent.py` | 99 examples | 100% |
| `score_workflows.py` | 143 examples | 100% |

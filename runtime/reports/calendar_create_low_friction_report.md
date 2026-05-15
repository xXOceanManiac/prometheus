# Calendar Create — Low-Friction Direct Execution Report

**Date:** 2026-05-15  
**Feature:** Auto-execute low-risk NL calendar creates without requiring a second verbal "yes"  
**Status:** Complete — 1023 tests passing, 273/273 audit checks passing

---

## What Was Built

Prometheus now executes fully-specified, low-risk calendar create requests directly in the same turn — no second verbal confirmation required. The user says it, Prometheus creates it and confirms immediately.

Direct-create examples:
- "Schedule a focus block tomorrow at 2" → done, 'Focus Block' added 2:00–3:30 PM
- "Add a workout tomorrow at 4" → done, 'Workout' added 4:00–5:00 PM
- "Put church meeting on my calendar Sunday at 10" → done, 'Church Meeting' added 10:00–11:00 AM
- "Block off 90 minutes for work tomorrow at 2" → done, 'Work' added 2:00–3:30 PM
- "Create an event called call Knox Friday at 3" → done, 'Call Knox' added 3:00–4:00 PM

---

## Decision Logic

### Auto-execute (no confirmation needed)
All of these must be true:
- All fields present (title, date, time)
- Exact time (not window-based like "afternoon")
- Duration ≤ 240 minutes (4 hours)
- No recurring indicators (every, weekly, daily, repeat)
- Not all-day or multi-day
- No " with " (proxy for invite attendees)
- No update/reschedule/delete keywords
- Not sleep hours (before 6 AM or at/after 11 PM)
- No conflict with existing events

### Still ask clarification
- Missing date, time, or title
- Vague: "schedule a meeting", "add something tomorrow"

### Still require confirmation (pending file written, await yes/no)
- Window-based times ("this afternoon", "tomorrow morning")
- Event with attendees (" with " in text)
- Duration > 4 hours
- Recurring events
- All-day / multi-day
- Sleep hours
- Overlapping event detected (conflict status)

---

## Architecture Changes

### New functions in `prometheus/agents/calendar_create_flow.py`

**`_extract_explicit_duration(text)`**  
Extracts stated duration from text: "90 minutes" → 90, "1 hour" → 60, "half an hour" → 30. Overrides title-based defaults.

**`should_auto_execute_calendar_create(draft, user_message) → tuple[bool, str]`**  
Classifies a parsed request as safe to direct-create or requiring confirmation. Returns `(True, "")` or `(False, reason)`.

**`_check_conflict(draft) → Optional[str]`**  
Queries the calendar for overlapping events. Returns the conflicting event title or None. Any failure returns None (fail open — proceed with create).

**`_direct_create_calendar_event(user_request, draft) → dict`**  
Executes the create immediately without a pending confirmation file. Uses `approval_mode: "direct_user_command"`. All env gates (GOOGLE_CALENDAR_ENABLED, DRY_RUN) still enforced by the executor. Returns `status: "executed" | "blocked" | "failed"`.

### Updated `parse_and_propose` flow

```
Missing fields → "needs_input"
Window-based → availability search:
  No slot → "no_availability"
  Slot found → write pending confirmation → "pending" (still asks confirm)
Exact time:
  should_auto_execute=False → write pending confirmation → "pending"
  should_auto_execute=True:
    conflict found → write pending confirmation → "conflict"
    no conflict → _direct_create_calendar_event → "executed" | "blocked" | "failed"
```

### Parser improvements

**`_extract_time_hint` now handles:**
- `"4pm tomorrow"` — bare am/pm without "at"
- `"tomorrow 2"` — bare number immediately after a date word
- `"at 2"` — original "at X" pattern (still highest priority)

**`_extract_explicit_duration` added:**  
"block off 90 minutes for work tomorrow at 2" → duration_minutes=90 (overrides title-based default)

### Intent override expansion (`prometheus/core/intent_overrides.py`)

**`text.startswith("schedule ")` prefix check** — catches "schedule church meeting", "schedule my sprint review", etc. without matching "my schedule today" (which is a calendar read phrase).

**Compound "on my calendar" check** — catches "put church meeting on my calendar Sunday at 10", "add team lunch to my calendar Friday", etc. Checks that text starts with a create verb AND contains "on my calendar" / "to my calendar" / "on the calendar".

**New phrases added:** `make a meeting`, `make a call`, `make a session`, `make a sync`, `make an appointment`, `log a `, `plan a `

### Response synthesizer (`prometheus/execution/response_synthesizer.py`)

Added handlers for new statuses in `_calendar_create_flow`:
- `"executed"` → "Done. 'Focus Block' has been added to your calendar for tomorrow from 14:00 to 15:30. No filler."
- `"blocked"` → Explains DRY_RUN gate clearly
- `"failed"` → Reports executor error with title
- `"conflict"` → Reports overlapping event name, repeats proposal, asks confirm or cancel

---

## Hard Constraints Maintained

| Constraint | Status |
|---|---|
| No passive automation | ✓ All creates are in response to explicit user requests |
| No Home Assistant | ✓ No HA calls added |
| No direct GCal API calls | ✓ Routes through executor; no googleapiclient or build_google_calendar_service |
| GOOGLE_CALENDAR_ENABLED required | ✓ Executor gate unchanged |
| GOOGLE_CALENDAR_DRY_RUN=false required | ✓ Executor gate unchanged; returns "blocked" status with clear message |
| No executor bypass | ✓ All writes go through execute_approved_calendar_request |
| Confirmation system preserved | ✓ Window-based, high-risk, conflict cases still use pending confirmation flow |
| No recurring events | ✓ Recurring keywords → pending (never auto-execute) |
| No delete/update via NL | ✓ Update/reschedule keywords → pending |
| Sleep hours protected | ✓ Before 6 AM or at/after 11 PM → pending |

---

## Test Results

| Suite | Count | Result |
|---|---|---|
| `test_calendar_create_flow.py` | 172 tests | All pass |
| Full test suite | 1023 tests, 1 skipped | All pass |
| Audit | 273/273 checks | All pass |
| `score_contextual_intent.py` | 99 examples | 100% |
| `score_workflows.py` | 143 examples | 100% |

### New test sections added (61 new tests)

- Section 16: Explicit duration extraction (10 tests)
- Section 17: Updated time hint extraction (5 tests)
- Section 18: `should_auto_execute_calendar_create` criteria (11 tests)
- Section 19: `_check_conflict` (6 tests)
- Section 20: `_direct_create_calendar_event` (6 tests)
- Section 21: `parse_and_propose` auto-execute integration (8 tests)
- Section 22: Parser improvements end-to-end (7 tests)
- Section 23: Response synthesizer new statuses (5 tests)
- Section 15 expanded: Intent routing additions (3 tests)

### Updated existing tests

- `test_returns_pending_for_complete_request` → `test_returns_executed_for_complete_request`
- `test_propose_does_not_write_to_calendar` → tests window+no-slot case (correct invariant)
- `test_no_passive_scheduling` → tests missing-field requests (correct invariant)
- `test_no_direct_gcal_api_calls_in_source` → updated forbidden pattern list (excludes our `_direct_create_calendar_event` function name)

---

## New Audit Checks (Section 16 additions, 8 checks)

| Check | Description |
|---|---|
| `auto_execute_fn_callable` | `should_auto_execute_calendar_create` exports and runs |
| `auto_execute_low_risk_ok` | Returns True for "schedule a focus block tomorrow at 2" |
| `auto_execute_blocks_sleep_hours` | Returns False for 4 AM events |
| `auto_execute_blocks_recurring` | Returns False for "every monday" |
| `direct_create_uses_req_direct_prefix` | request_id starts with "req-direct-" |
| `explicit_duration_extraction` | `_extract_explicit_duration` extracts correctly |
| `compound_on_my_calendar_routes_to_proposal` | "put church meeting on my calendar" → proposal |
| `synthesizer_handles_executed_status` | Synthesizer produces useful text for "executed" |

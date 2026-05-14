# Calendar Read Tools — Session Report

**Generated:** 2026-05-14  
**Session scope:** Read-only Google Calendar integration for Prometheus_Main tool/action layer

---

## Files Changed

| File | Change |
|------|--------|
| `prometheus/agents/calendar_read_tools.py` | **Created** — 7 read-only calendar functions + CLI |
| `prometheus/execution/tool_capability_registry.py` | Added 7 calendar `ToolCapability` entries (risk=none) |
| `prometheus/core/intent_overrides.py` | Added 5 calendar phrase groups + resolve logic |
| `tools.py` | Added 7 actions to `ACTION_ENUM`, new params to schemas, `_execute_calendar_read()` method |
| `planner/planner.py` | Added 5 rule-based calendar patterns to `_rule_based()` |
| `tests/test_calendar_read_tools.py` | **Created** — 76 unit tests |
| `tests/audit_prometheus.py` | Added Section 10 (43 calendar audit checks), updated `known_handled` |
| `tests/test_tool_capability_registry.py` | Added 7 calendar tools to required set |

---

## Tools Added

| Tool | Description | Risk |
|------|-------------|------|
| `calendar_list_upcoming` | List events over next N days | none |
| `calendar_get_today` | List today's events | none |
| `calendar_get_tomorrow` | List tomorrow's events | none |
| `calendar_get_date` | List events for a specific YYYY-MM-DD | none |
| `calendar_next_event` | Get next upcoming timed event | none |
| `calendar_summarize_day` | Structured day summary with first/last event | none |
| `calendar_find_free_blocks` | Find free time gaps ≥ N minutes (timed events only) | none |

---

## Tool Registry Integration

All 7 tools are registered in `prometheus/execution/tool_capability_registry.py` with:
- `risk = "none"`
- `safe_when = ["always safe — read-only"]`
- `supports_verification = True`
- Appropriate `workflow_tags` (mostly `["resume_mission"]`)

Accessible via `TOOL_CAPABILITIES`, `get_tool()`, `tools_for_workflow()`, and `verifiable_tools()`.

---

## Planner/Action Mapping Added

Rule-based patterns in `planner/planner.py` `_rule_based()`:

| Pattern | Routes to | Confidence |
|---------|-----------|-----------|
| `calendar today`, `what's on my calendar today`, `what do i have today` | `calendar_get_today` | 0.92 |
| `calendar tomorrow`, `what do i have tomorrow`, `tomorrow's schedule` | `calendar_get_tomorrow` | 0.92 |
| `what's my next event`, `next meeting`, `what's coming up` | `calendar_next_event` | 0.92 |
| `summarize my day`, `how does my day look`, `daily summary` | `calendar_summarize_day` | 0.92 |
| `do i have a free hour`, `when am i free`, `free blocks today` | `calendar_find_free_blocks` | 0.90 |

---

## Direct Intent Overrides Added

In `prometheus/core/intent_overrides.py`, 5 phrase groups placed **before** the `_WEB_SEARCH_KEYWORDS` fallback to prevent "today"/"tomorrow" from routing to web search:

- `_CALENDAR_TODAY_PHRASES` → `calendar_get_today`
- `_CALENDAR_TOMORROW_PHRASES` → `calendar_get_tomorrow`
- `_CALENDAR_NEXT_EVENT_PHRASES` → `calendar_next_event`
- `_CALENDAR_SUMMARIZE_PHRASES` → `calendar_summarize_day`
- `_CALENDAR_FREE_BLOCK_PHRASES` → `calendar_find_free_blocks` (with today's date + minimum_minutes=60 pre-filled)

---

## Safety Classification

- All 7 calendar tools: `risk = "none"` — read-only, no approval required
- No write tools registered (confirmed by audit check)
- No Home Assistant calls (confirmed by audit + test)
- No subprocess/shell execution (confirmed by audit + test)
- Google Calendar `create_calendar_event`, `update_calendar_event`, `delete_calendar_event` are NOT called by any calendar read tool
- Lumen proposals are NOT executed — Lumen remains a sibling calendar-controller agent
- No passive scheduler or autonomy introduced

---

## Test Commands Run

```bash
# Full test suite
.venv/bin/python3 -m pytest -x -q
# Result: 729 passed, 1 skipped, 1 warning — all green

# Calendar-specific tests
.venv/bin/python3 -m pytest tests/test_calendar_read_tools.py -x -q
# Result: 76 passed

# Full audit
.venv/bin/python3 tests/audit_prometheus.py
# Result: 204/204 passed

# Contextual intent eval
.venv/bin/python3 tests/score_contextual_intent.py
# Result: All examples passed ✓

# Workflow eval
.venv/bin/python3 tests/score_workflows.py
# Result: ALL TARGETS MET (100% classification, 0% wrong workflow, 0% dangerous false exec)
```

---

## Audit Results (Section 10 — Calendar Read Tools)

All 43 calendar audit checks passed:
- Module imports cleanly
- All 7 functions exist
- Disabled calendar returns error dict (no crash)
- Invalid date strings return error
- Free-block algorithm finds gaps with mocked events
- Day summary has all required fields
- Next event separates timed vs all-day
- Output is JSON-serializable
- No Home Assistant calls in module source
- No subprocess calls in module source
- All 7 tools in registry with risk=none
- No write tools in registry
- All 5 intent override phrases route correctly
- ToolRegistry dispatches all calendar actions without crash
- Disabled calendar returns graceful ToolResult from ToolRegistry

---

## Live Smoke Test Results

Tested against authenticated Google Calendar (token at `calendar_token.json`):

```bash
python3 -m prometheus.agents.calendar_read_tools --today
# Returns 2 real events for 2026-05-14:
# - "Knox getting permit" at 16:00
# - "BocaRunClub" at 18:30 (Red Reef Park, Boca Raton)

python3 -m prometheus.agents.calendar_read_tools --tomorrow
# Returns 1 all-day event: "review" on 2026-05-15

python3 -m prometheus.agents.calendar_read_tools --next
# Returns next_timed_event: "Knox getting permit" at 2026-05-14T16:00

python3 -m prometheus.agents.calendar_read_tools --free-blocks 2026-05-14
# Returns 3 free blocks:
#   08:00–16:00 (480 min), 17:00–18:30 (90 min), 19:30–22:00 (150 min)
```

---

## Confirmations

- **No calendar writes added** — `create_calendar_event`, `update_calendar_event`, `delete_calendar_event` not called
- **No Lumen proposals executed** — Lumen remains a sibling calendar-controller agent
- **No Home Assistant behavior added** — confirmed by source inspection and tests
- **No passive scheduler or autonomy added**
- **No subprocess/shell execution introduced**

---

## Known Next Step

**Approved calendar write execution layer** — when Prometheus needs to create/update/delete events, a separate write-tool module (`calendar_write_tools.py`) should be built with:
- Dry-run by default (config-gated)
- Confirmation required for every write
- Integration with Lumen proposal approval flow
- No writes without `confirmed=True` in payload (same pattern as `git_commit`)

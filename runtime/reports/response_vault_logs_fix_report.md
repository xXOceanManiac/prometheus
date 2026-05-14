# Response Synthesis / Vault Diagnostics / Show Logs Fix — Session Report

**Generated:** 2026-05-14  
**Session scope:** Three usability fixes — post-tool spoken responses, vault diagnostic path discovery, show_logs rewrite using Python file I/O

---

## Problem Summary

| Issue | Root Cause |
|-------|-----------|
| Calendar tools execute silently | 7 calendar actions missing from `FOLLOWUP_ACTIONS` — no LLM followup was triggered |
| Vault shows "inactive" in diagnostics | Fallback path was `~/Tates_Brain` (underscore), but vault lives at `~/Desktop/Tates Brain` (space); `vault_path` in config was empty |
| Show Logs does nothing useful | Handler fell through to `journalctl` subprocess for non-file sources; no Python log file path was wired |

---

## Files Changed

| File | Change |
|------|--------|
| `prometheus/execution/response_synthesizer.py` | **Created** — `synthesize_tool_response()` + 7 calendar formatters |
| `prometheus/core/tool_followups.py` | Added 7 calendar actions to `FOLLOWUP_ACTIONS` |
| `realtime_client.py` | Import synthesizer; added `elif is_calendar_action()` in both `_run_direct_tool` and `_handle_tool_call` |
| `prometheus/infra/paths.py` | Added `JARVIS_LOGS_DIR = JARVIS_STATE_DIR / "logs"` |
| `prometheus/infra/log_viewer.py` | **Created** — `list_log_files()`, `read_log_tail()`, `read_latest_log_tail()` |
| `tools.py` (show_logs handler) | Rewritten — uses `log_viewer`, no subprocess/journalctl |
| `tools.py` (vault diagnostic) | Multi-candidate path scan, new `{active, path, exists, readable, reason, checked_at}` fields |
| `jarvis_desktop_hud.py` | Vault lambda updated: `d.get("active", d.get("db_exists", False))` |
| `tests/test_response_synthesizer.py` | **Created** — 50 unit tests |
| `tests/test_log_viewer.py` | **Created** — 50 unit tests |
| `tests/audit_prometheus.py` | Added `section_response_vault_logs()` — 18 audit checks |

---

## Fix 1: Post-Tool Response Synthesis

### What changed

**`prometheus/core/tool_followups.py`**  
All 7 calendar actions added to `FOLLOWUP_ACTIONS`:
```
calendar_list_upcoming, calendar_get_today, calendar_get_tomorrow,
calendar_get_date, calendar_next_event, calendar_summarize_day,
calendar_find_free_blocks
```

**`prometheus/execution/response_synthesizer.py`** (new module)  
- `synthesize_tool_response(action, result, original_user_message=None) -> str`
- `is_calendar_action(action) -> bool`
- 7 private formatters: `_event_list`, `_upcoming`, `_next_event`, `_day_summary`, `_free_blocks`
- Returns `response_instructions` string passed to `_guarded_response_create`
- Formats event times as HH:MM, handles all-day events, caps at 10 events / 5 free blocks

**`realtime_client.py`**  
- Import: `from prometheus.execution.response_synthesizer import synthesize_tool_response, is_calendar_action`
- Added `elif is_calendar_action(action):` block in `_run_direct_tool` (line ~645)
- Added `elif is_calendar_action(tool_action):` block in `_handle_tool_call` (line ~1113)

### Example outputs

- `calendar_get_today` with 2 events → "The user has 2 event(s) today:\n- Knox getting permit at 16:00\n- BocaRunClub at 18:30\nRead them naturally..."
- `calendar_get_today` with no events → "Tell the user they have nothing scheduled today."
- `calendar_next_event` → "Next event: Knox getting permit at 16:00. Also: review all day on 2026-05-15."
- `calendar_find_free_blocks` with 3 blocks → "Free blocks of at least 60 min on 2026-05-14:\n- 08:00–16:00 (480 min)\n..."

---

## Fix 2: Vault Diagnostics

### Root cause

Config `vault_path` is empty string. Old fallback: `~/Tates_Brain` (underscore).  
Actual vault location: `/home/tatel/Desktop/Tates Brain/data/memory.db` (space, not underscore).

### What changed

`tools.py` `run_diagnostics()` vault block now:
1. Reads `CONFIG.get("vault_path")` first
2. Falls back to `VAULT_PATH` env var
3. Scans candidate list:
   - `~/Desktop/Tates Brain`
   - `~/Desktop/Tates_Brain`
   - `~/Tates_Brain`
   - `~/Tates Brain`
4. Uses the first candidate with a `data/memory.db` that exists
5. Returns enriched dict:
   ```json
   {
     "active": true,
     "db_exists": true,
     "path": "/home/tatel/Desktop/Tates Brain",
     "exists": true,
     "readable": true,
     "chunk_count": 32521,
     "last_indexed": "2026-05-13T...",
     "reason": "",
     "checked_at": "2026-05-14T..."
   }
   ```

HUD lambda updated from `d.get("db_exists", False)` to `d.get("active", d.get("db_exists", False))` — backward compatible.

---

## Fix 3: Show Logs

### Root cause

- Handler defaulted to `journalctl` subprocess for non-file-path sources
- Prometheus writes JSONL logs to `~/.jarvis/logs/YYYY-MM-DD.jsonl`
- The handler had no knowledge of this path; no file I/O path to Prometheus logs

### What changed

**`prometheus/infra/paths.py`**  
Added: `JARVIS_LOGS_DIR = JARVIS_STATE_DIR / "logs"` (→ `~/.jarvis/logs`)

**`prometheus/infra/log_viewer.py`** (new module)
- `list_log_files()` — lists `.jsonl` files in `JARVIS_LOGS_DIR`, newest first
- `read_log_tail(filename, tail_lines=50)` — reads tail of a named log file; validates path stays inside `JARVIS_LOGS_DIR`; raises `ValueError` on path traversal
- `read_latest_log_tail(tail_lines=50)` — reads today's or most recent log file
- `_format_jsonl(lines)` — converts JSONL records to `HH:MM:SS  kind | key=value` human-readable lines
- No subprocess, no shell, no `journalctl`

**`tools.py` `show_logs` handler**  
Rewritten to use `log_viewer`:
- `source` param: if provided, reads that filename from `JARVIS_LOGS_DIR`; path traversal attempt returns `ok=False`
- No source: reads latest log via `read_latest_log_tail()`
- No log files found: returns `ok=True` with clear "No log files found" message
- No `journalctl`, no subprocess anywhere in the handler

---

## Test Results

```bash
# New tests
.venv/bin/python3 -m pytest tests/test_response_synthesizer.py tests/test_log_viewer.py -x -q
# Result: 50 passed

# Full suite
.venv/bin/python3 -m pytest -x -q
# Result: 779 passed, 1 skipped, 1 warning — all green

# Full audit
.venv/bin/python3 tests/audit_prometheus.py
# Result: 222/222 passed

# Contextual intent eval
.venv/bin/python3 tests/score_contextual_intent.py
# Result: All examples passed ✓

# Workflow eval
.venv/bin/python3 tests/score_workflows.py
# Result: ALL TARGETS MET
```

---

## Audit Results (section_response_vault_logs — 18 checks)

All 18 checks passed:
- `response_synthesizer` imports cleanly
- All 7 calendar actions in `_CALENDAR_ACTIONS` set
- All 7 actions return `str` from `synthesize_tool_response`
- Failed result path returns string
- Unknown action fallback returns string
- `calendar_get_today` no-events says "nothing"
- `calendar_next_event` no-events says "no upcoming"
- All 7 in `FOLLOWUP_ACTIONS`
- `realtime_client.py` imports synthesizer and uses `is_calendar_action`
- `log_viewer` imports cleanly
- `list_log_files()` returns `[]` for missing directory
- Path traversal attempt raises `ValueError`
- `log_viewer.py` contains no subprocess import
- `show_logs` handler contains no `journalctl`
- `JARVIS_LOGS_DIR` exists in `paths.py`
- `tools.py` vault block has candidate scan including "Tates Brain"
- `tools.py` vault block has `active`, `path`, `readable`, `checked_at` fields
- HUD vault lambda uses `active` with `db_exists` fallback

---

## Confirmations

- **No calendar writes added**
- **No Lumen proposals executed**
- **No passive automation added**
- **No Home Assistant behavior changed**
- **No safety/audit checks weakened**
- **No subprocess added** — `show_logs` now pure Python file I/O
- **All existing tests pass** — 779/779

# Google Calendar OAuth & Lumen Router Report

**Generated:** 2026-05-14  
**Session purpose:** Advance Lumen to a connected Prometheus-controlled calendar system

---

## Files Created / Changed

### Created
- `prometheus/agents/lumen_calendar_context.py` — Google Calendar → Lumen event conversion (~55 lines)
- `prometheus/agents/lumen_calendar_router.py` — Dry-run review router for Lumen proposals (~160 lines)
- `tests/test_lumen_calendar_context.py` — 35 tests
- `tests/test_lumen_calendar_router.py` — 44 tests

### Modified
- `prometheus/integrations/google_calendar.py` — added dotenv CLI fix, `authorize_google_calendar()`, `list_upcoming_calendar_events()`, `--auth`, `--list-upcoming` commands
- `prometheus/infra/paths.py` — added `REVIEWED_LUMEN_DIR`, `SECRETS_DIR`, `ensure_lumen_router_dirs()`
- `tests/test_import_integrity.py` — added 3 new import tests for new modules
- `tests/audit_prometheus.py` — added `section_lumen_calendar_context()` (5 checks), `section_lumen_calendar_router()` (9 checks), 4 new google_calendar checks

---

## Architecture

```
prometheus/agents/lumen_calendar_context.py
├── google_event_to_lumen_event_dict()   — GoogleCalendarEvent → dict
├── google_events_to_lumen_event_dicts() — list[GoogleCalendarEvent] → list[dict]
└── build_calendar_context_summary()     — list[GoogleCalendarEvent] → context summary dict

prometheus/agents/lumen_calendar_router.py
├── load_pending_lumen_proposal()         — load one pending proposal by request_id
├── write_lumen_review_result()           — persist review to runtime/reviewed/lumen_calendar/
├── list_reviewed_lumen_calendar_proposals() — list all review results
├── review_lumen_proposal_dry_run()       — dry-run review one proposal
├── review_pending_lumen_proposals_dry_run() — dry-run review all pending proposals
└── CLI: --dry-run-all | --dry-run-request REQUEST_ID | --list-reviewed

prometheus/integrations/google_calendar.py (additions)
├── list_upcoming_calendar_events()       — read upcoming events (read-only)
├── authorize_google_calendar()           — OAuth bootstrap (explicit-only)
└── CLI: --auth | --list-upcoming [DAYS]
```

---

## .env Fix

The Google Calendar CLI `_main()` now loads `Prometheus_Main/.env` via dotenv
before reading `GOOGLE_CALENDAR_*` env vars:

```python
def _main(argv):
    try:
        from dotenv import load_dotenv
        from prometheus.infra.paths import PROJECT_ROOT
        load_dotenv(PROJECT_ROOT / ".env", override=False)
    except ImportError:
        pass
    ...
```

`override=False` means existing env vars take precedence (test isolation preserved).
The dotenv load is in `_main()` only, not in `load_google_calendar_config()`, to
avoid interfering with test isolation.

**Result:** `python3 -m prometheus.integrations.google_calendar --config` now shows
`credentials_path: "<set>"` / `token_path: "<set>"` without manually sourcing `.env`.

---

## OAuth Bootstrap

`authorize_google_calendar(config, allow_interactive_auth=True)` runs the OAuth flow
once to generate a token file. It is:

- Never called automatically — only via explicit `--auth` CLI command
- Never called at import time
- Guarded by `allow_interactive_auth=True` argument (explicit opt-in)

```bash
python3 -m prometheus.integrations.google_calendar --auth
```

This will open a browser window for Google OAuth consent, then save the token to
`GOOGLE_CALENDAR_TOKEN_PATH`.

**Prerequisites before running `--auth`:**
1. Download `credentials.json` from Google Cloud Console
2. Set `GOOGLE_CALENDAR_CREDENTIALS_PATH` in `.env`
3. Set `GOOGLE_CALENDAR_TOKEN_PATH` in `.env`
4. Set `GOOGLE_CALENDAR_ENABLED=true` in `.env`

---

## Read-Only Calendar Access

`list_upcoming_calendar_events(service, config, days_ahead=7)` reads events from
now through `days_ahead` days. It calls `list_calendar_events()` with auto-computed
time bounds. No writes.

```bash
python3 -m prometheus.integrations.google_calendar --list-upcoming 7
```

Returns JSON array of events. Requires `enabled=True` and a valid token.

---

## Lumen Calendar Context

Three pure conversion functions with no network calls, no API calls, no filesystem writes:

| Function | Input | Output |
|---|---|---|
| `google_event_to_lumen_event_dict()` | `GoogleCalendarEvent` | `dict` |
| `google_events_to_lumen_event_dicts()` | `list[GoogleCalendarEvent]` | `list[dict]` |
| `build_calendar_context_summary()` | `list[GoogleCalendarEvent]` | context summary `dict` |

Context summary includes: `event_count`, `events`, `earliest_start`, `latest_end`.

---

## Lumen Proposal Router

The router reads pending proposals from `runtime/pending/lumen_calendar/`,
runs `dry_run_calendar_operation()` on each operation, and writes results to
`runtime/reviewed/lumen_calendar/`.

### Review result structure
```json
{
  "request_id": "req-156113ed410c",
  "reviewed_at": "2026-05-14T05:25:30+00:00",
  "proposal_reason": "Schedule focus block",
  "source": "lumen",
  "operation_count": 1,
  "all_dry_run": true,
  "approved": false,
  "results": [
    {
      "operation_index": 0,
      "operation_type": "create_event",
      "success": true,
      "dry_run": true,
      "message": "[DRY RUN] Would create event 'Focus Block' at 2026-05-15T14:00.",
      "calendar_id": "primary",
      "event_id": null
    }
  ]
}
```

`approved` is always `false` — the router presents proposals for review, not approval.
Approval requires a separate explicit step (not built in this session).

---

## Safety Confirmations

- **No live Google Calendar API calls** — all router reviews use `dry_run_calendar_operation()` only
- **No `create_calendar_event / update_calendar_event / delete_calendar_event` in router** — confirmed by audit
- **No `build_google_calendar_service` in router** — confirmed by audit
- **OAuth never runs automatically** — guarded by explicit `--auth` CLI command only
- **No subprocess/shell execution** — confirmed by audit in all new modules
- **No Home Assistant calls** — confirmed by audit in all new modules
- **Lumen proposals never auto-approved** — `approved: false` always
- **No Lumen ingestion validation weakened** — existing 9 checks all still pass

---

## New Paths

| Path | Purpose |
|---|---|
| `runtime/reviewed/lumen_calendar/` | Dry-run review results |
| `runtime/secrets/` | Credentials and tokens (gitignored) |

Both directories are gitignored. `runtime/secrets/` is the expected location for
`credentials.json` and `token.json` per the `.env` configuration.

---

## Test Commands Run

```bash
python3 -m pytest tests/                                           # 617 passed
python3 tests/audit_prometheus.py                                  # 157/157 passed
python3 tests/score_contextual_intent.py                           # 100%
python3 tests/score_workflows.py                                   # All targets met
python3 -m prometheus.integrations.google_calendar --config        # shows <set> paths
python3 -m prometheus.integrations.google_calendar --dry-run-create-sample
python3 -m prometheus.agents.lumen_ingestion --list-pending        # 13 pending proposals
python3 -m prometheus.agents.lumen_calendar_router --dry-run-all   # 13 reviews written
python3 -m prometheus.agents.lumen_calendar_router --dry-run-request req-156113ed410c
python3 -m prometheus.agents.lumen_calendar_router --list-reviewed  # 13 reviews returned
```

---

## Test Results

| Suite | Result |
|---|---|
| `pytest tests/` | **617 passed, 0 failed** (+59 new tests) |
| `audit_prometheus.py` | **157/157 passed** (+18 new checks) |
| `score_contextual_intent.py` | All examples passed — 100% |
| `score_workflows.py` | All targets met — 100% classification |

35 new tests in `test_lumen_calendar_context.py`, 44 new tests in `test_lumen_calendar_router.py`,
3 new import integrity tests.

---

## New Audit Checks (18 total)

### google_calendar additions (4 new)
- `google_calendar:auth_function_exists` ✓
- `google_calendar:auth_not_at_import` ✓
- `google_calendar:list_upcoming_exists` ✓
- `google_calendar:dotenv_in_cli` ✓

### lumen_calendar_context (5 new)
- `lumen_calendar_context:module_imports` ✓
- `lumen_calendar_context:event_to_dict` ✓
- `lumen_calendar_context:empty_summary` ✓
- `lumen_calendar_context:multi_event_summary` ✓
- `lumen_calendar_context:no_api_calls` ✓

### lumen_calendar_router (9 new)
- `lumen_calendar_router:module_imports` ✓
- `lumen_calendar_router:load_missing_returns_none` ✓
- `lumen_calendar_router:missing_review_has_dry_run` ✓
- `lumen_calendar_router:review_all_returns_list` ✓
- `lumen_calendar_router:list_reviewed_returns_list` ✓
- `lumen_calendar_router:no_live_write_calls` ✓
- `lumen_calendar_router:no_subprocess` ✓
- `lumen_calendar_router:no_home_assistant` ✓
- `lumen_calendar_router:no_auto_approval` ✓

---

## Known Next Step

**Approval execution flow:**  
Build a Prometheus approval layer that:
1. Presents reviewed proposals to the user (voice or explicit command)
2. On explicit approval, calls `create/update/delete_calendar_event()` with `enabled=True, dry_run=False`
3. Archives the approved proposal and writes result to `runtime/reports/`

Before that step, Google Calendar auth must be completed:
- Set `GOOGLE_CALENDAR_ENABLED=true` in `.env`
- Run `python3 -m prometheus.integrations.google_calendar --auth` once to generate token

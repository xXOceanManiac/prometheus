# Lumen Ingestion Report

**Generated:** 2026-05-14  
**Session purpose:** Add Lumen outbox ingestion to Prometheus_Main

---

## Files Changed

### Modified
- `prometheus/infra/paths.py` — added Lumen ecosystem path constants and `ensure_lumen_ingestion_dirs()`
- `tests/test_import_integrity.py` — added `TestPrometheusAgents` class with import and path tests
- `tests/audit_prometheus.py` — added `section_lumen_ingestion()` with 9 audit checks

### Created
- `prometheus/agents/lumen_ingestion.py` — ingestion agent module
- `tests/test_lumen_ingestion.py` — 33 tests for ingestion behavior

---

## Ingestion Architecture

```
Lumen/runtime/outbox/lumen_calendar_request_*.json
        │
        ▼
prometheus/agents/lumen_ingestion.py
  ingest_lumen_outbox_once()
        │
        ├─ validate_lumen_calendar_request(payload)
        │       valid ──► PendingCalendarProposal
        │                      │
        │                      ▼
        │             Prometheus_Main/runtime/pending/lumen_calendar/pending_<id>.json
        │                      + move original → Lumen/runtime/accepted/
        │
        └─ invalid ──► move original → Lumen/runtime/rejected/
```

Prometheus_Main is the authority. No calendar operation executes during ingestion.

---

## New Path Constants (prometheus/infra/paths.py)

| Constant | Path |
|----------|------|
| `PROMETHEUS_ECOSYSTEM_ROOT` | `PROJECT_ROOT.parent` = `/home/tatel/Desktop/PROMETHEUS` |
| `LUMEN_ROOT` | `PROMETHEUS_ECOSYSTEM_ROOT / "Lumen"` |
| `LUMEN_OUTBOX_DIR` | `LUMEN_ROOT / "runtime" / "outbox"` |
| `LUMEN_ACCEPTED_DIR` | `LUMEN_ROOT / "runtime" / "accepted"` |
| `LUMEN_REJECTED_DIR` | `LUMEN_ROOT / "runtime" / "rejected"` |
| `LUMEN_ARCHIVE_DIR` | `LUMEN_ROOT / "runtime" / "archive"` |
| `PENDING_LUMEN_DIR` | `RUNTIME_ROOT / "pending" / "lumen_calendar"` |

---

## Validation Rules

A Lumen request is accepted when ALL of the following hold:
- Payload is a dict
- `source == "lumen"`
- `request_id` is a non-empty string
- `requires_prometheus_approval is True`
- `created_at` is a non-empty string
- `operations` is a non-empty list
- Every operation: `requires_prometheus_approval is True`
- Every operation: `dry_run is True`
- Every operation: `operation_type` ∈ `{create_event, update_event, delete_event, read_events, find_availability, suggest_schedule_change}`
- No operation contains suspicious keys: `command, shell, subprocess, exec, eval, url, token, api_key, home_assistant, ha_service`

---

## Accepted/Rejected/Archive Folder Behavior

| Event | Original file moves to |
|-------|----------------------|
| Validation passes | `Lumen/runtime/accepted/` |
| Invalid JSON or failed validation | `Lumen/runtime/rejected/` |
| `LUMEN_ARCHIVE_DIR` | Available for future archival rotation |

Pending proposals write to: `Prometheus_Main/runtime/pending/lumen_calendar/pending_<request_id>.json`

---

## CLI Commands

```bash
# From Prometheus_Main/:
python -m prometheus.agents.lumen_ingestion --ingest-once
python -m prometheus.agents.lumen_ingestion --list-pending
```

---

## Test Results

| Suite | Result |
|-------|--------|
| `pytest tests/` | **505 passed, 0 failed** (1 unrelated collection warning) |
| `tests/audit_prometheus.py` | **129/129 passed** (9 new lumen_ingestion checks) |
| `score_contextual_intent.py` | All examples passed — 100% |
| `score_workflows.py` | All targets met — 100% classification |

33 new tests in `test_lumen_ingestion.py`, 2 new import integrity tests.

---

## End-to-End Validation

- Created 1 fresh Lumen outbox request via `lumen.cli --sample-create-event-request`
- Ran `--ingest-once` → 13 requests accepted (all from previous sessions)
- Lumen outbox now empty, 13 files in `Lumen/runtime/accepted/`
- 13 pending proposals in `Prometheus_Main/runtime/pending/lumen_calendar/`
- `--list-pending` returns all 13 proposals correctly

---

## Safety Confirmations

- **No calendar operations execute** — ingestion only validates, archives, and writes JSON proposals
- **No Google Calendar API calls added** — confirmed by audit and source inspection
- **No Home Assistant calls added** — confirmed by audit and source inspection
- **No shell/system execution added** — confirmed by audit and source inspection
- **`dry_run=True` enforced** — any operation with `dry_run=False` is rejected
- **`requires_prometheus_approval=True` enforced** — rejected if False at request or operation level

---

## Known Next Step

**Google Calendar adapter / approved calendar tool routing:**  
Prometheus_Main reads `runtime/pending/lumen_calendar/pending_*.json`, presents proposals for approval, then routes approved operations to a future Google Calendar adapter that will execute reads/writes against the real API.

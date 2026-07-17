"""
lumen_calendar_executor.py — Approved execution layer for Lumen calendar proposals.

Flow:
  Reviewed proposal → approve_calendar_request() → execute_approved_calendar_request()
                       ↓                            ↓
  APPROVED_LUMEN_DIR/  approval record    COMPLETED_LUMEN_DIR/ or FAILED_LUMEN_DIR/

Safety rules enforced here:
- Must have a written approval record before any execution.
- Must have a reviewed dry-run result (all_dry_run=true).
- GOOGLE_CALENDAR_ENABLED must be true.
- GOOGLE_CALENDAR_DRY_RUN must be false for real writes; blocked with clear message if true.
- Only create_event / update_event / delete_event are executable write types.
- All operations validated before any execution begins.
- No subprocess/shell execution.
- No Home Assistant calls.
- Lumen source files are never modified.
- No "execute all" batch command exists.
"""
from __future__ import annotations

import dataclasses
import json
import re as _re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from prometheus.infra.paths import (
    PENDING_LUMEN_DIR,
    REVIEWED_LUMEN_DIR,
    APPROVED_LUMEN_DIR,
    COMPLETED_LUMEN_DIR,
    FAILED_LUMEN_DIR,
    ensure_lumen_executor_dirs,
)
from prometheus.integrations.google_calendar import (
    GoogleCalendarConfig,
    GoogleCalendarResult,
    build_google_calendar_service,
    create_calendar_event,
    update_calendar_event,
    delete_calendar_event,
    load_google_calendar_config,
)

# ── Constants ─────────────────────────────────────────────────────────────────

_EXECUTABLE_WRITE_TYPES = frozenset({"create_event", "update_event", "delete_event"})
_NON_WRITE_TYPES = frozenset({"read_events", "find_availability", "suggest_schedule_change"})

_SUSPICIOUS_KEYS = frozenset({
    "command", "shell", "subprocess", "exec", "eval",
    "url", "token", "api_key", "home_assistant", "ha_service",
})


# ── Datetime helpers ──────────────────────────────────────────────────────────

def _parse_naive_dt(dt_str: str) -> datetime:
    """Parse a datetime string for comparison, stripping timezone offset.

    Handles: YYYY-MM-DDTHH:MM, YYYY-MM-DDTHH:MM:SS, same with Z/±HH:MM suffix.
    Raises ValueError if unparseable.
    """
    # Strip timezone suffix
    normalized = _re.sub(r'(Z|[+-]\d{2}:\d{2})$', '', dt_str.strip())
    # Add seconds if missing (HH:MM → HH:MM:SS)
    if _re.match(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$', normalized):
        normalized += ':00'
    return datetime.fromisoformat(normalized)


# ── Data loading ──────────────────────────────────────────────────────────────

def list_reviewed_calendar_requests() -> list[dict]:
    """Return all review result dicts from REVIEWED_LUMEN_DIR, newest first."""
    if not REVIEWED_LUMEN_DIR.exists():
        return []
    results: list[dict] = []
    for p in sorted(REVIEWED_LUMEN_DIR.glob("reviewed_*.json"), reverse=True):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            results.append(data)
        except (OSError, json.JSONDecodeError):
            continue
    return results


def load_reviewed_calendar_request(request_id: str) -> Optional[dict]:
    """Load a single reviewed request by request_id. Returns None if not found."""
    if not REVIEWED_LUMEN_DIR.exists():
        return None
    request_id = request_id.strip()
    for p in REVIEWED_LUMEN_DIR.glob("reviewed_*.json"):
        if request_id in p.name:
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
    return None


def _load_pending_proposal_raw(request_id: str) -> Optional[dict]:
    """Load the raw pending proposal dict (with operations list)."""
    if not PENDING_LUMEN_DIR.exists():
        return None
    request_id = request_id.strip()
    for p in PENDING_LUMEN_DIR.glob("pending_*.json"):
        if request_id in p.name:
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
    return None


def _load_approval_record(request_id: str) -> Optional[dict]:
    """Load an approval record for the given request_id."""
    if not APPROVED_LUMEN_DIR.exists():
        return None
    request_id = request_id.strip()
    for p in APPROVED_LUMEN_DIR.glob("approved_*.json"):
        if request_id in p.name:
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
    return None


# ── Approval ──────────────────────────────────────────────────────────────────

def approve_calendar_request(
    request_id: str,
    approved_by: str = "user",
) -> dict:
    """
    Write an approval record for a reviewed Lumen calendar request.

    Rules:
    - request must exist in REVIEWED_LUMEN_DIR
    - review must have all_dry_run=true (it was reviewed as a safe dry-run)
    - all review results must have success=true
    - original pending proposal must exist in PENDING_LUMEN_DIR
    - all operations must have requires_prometheus_approval=true
    - all operations must have dry_run=true in the original proposal

    Does NOT execute any calendar operation.
    """
    ensure_lumen_executor_dirs()
    request_id = request_id.strip()

    # Load reviewed result
    reviewed = load_reviewed_calendar_request(request_id)
    if reviewed is None:
        return {
            "ok": False,
            "approved": False,
            "request_id": request_id,
            "reason": f"Reviewed request not found: {request_id!r}",
        }

    # Must be a dry-run review
    if not reviewed.get("all_dry_run", False):
        return {
            "ok": False,
            "approved": False,
            "request_id": request_id,
            "reason": "Review record does not indicate all_dry_run=true. Cannot approve.",
        }

    # All review results must have succeeded
    results = reviewed.get("results", [])
    failed = [r for r in results if not r.get("success")]
    if failed:
        return {
            "ok": False,
            "approved": False,
            "request_id": request_id,
            "reason": f"{len(failed)} operation(s) failed dry-run review. Cannot approve.",
        }

    # Load pending proposal
    pending = _load_pending_proposal_raw(request_id)
    if pending is None:
        return {
            "ok": False,
            "approved": False,
            "request_id": request_id,
            "reason": f"Pending proposal not found for {request_id!r}. Cannot approve.",
        }

    operations = pending.get("operations", [])
    if not operations:
        return {
            "ok": False,
            "approved": False,
            "request_id": request_id,
            "reason": "Pending proposal has no operations.",
        }

    # All operations must require Prometheus approval and have dry_run=true
    for i, op in enumerate(operations):
        if not op.get("requires_prometheus_approval", False):
            return {
                "ok": False,
                "approved": False,
                "request_id": request_id,
                "reason": f"Operation {i} does not require Prometheus approval.",
            }
        if not op.get("dry_run", False):
            return {
                "ok": False,
                "approved": False,
                "request_id": request_id,
                "reason": f"Operation {i} does not have dry_run=true in original proposal.",
            }

    # Write approval record
    reviewed_path = str(REVIEWED_LUMEN_DIR / f"reviewed_{request_id}.json")
    approval = {
        "request_id": request_id,
        "approved": True,
        "approved_by": approved_by,
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "reviewed_path": reviewed_path,
        "operation_count": len(operations),
        "explicit_user_approval_required": True,
    }
    dest = APPROVED_LUMEN_DIR / f"approved_{request_id}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(approval, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "approved": True,
        "request_id": request_id,
        "approved_by": approved_by,
        "operation_count": len(operations),
        "approval_path": str(dest),
    }


# ── Execution ─────────────────────────────────────────────────────────────────

def _validate_operations(operations: list[dict]) -> tuple[bool, str]:
    """
    Validate all operations before executing any.

    Returns (ok, error_message). If ok=False, no operations should execute.
    """
    for i, op in enumerate(operations):
        op_type = op.get("operation_type", "")

        # Check for suspicious keys
        suspicious = _SUSPICIOUS_KEYS & set(op.keys())
        if suspicious:
            return False, f"Operation {i} contains suspicious keys: {suspicious}"

        if op_type in _NON_WRITE_TYPES:
            # read_events / find_availability / suggest_schedule_change are skipped
            continue

        if op_type not in _EXECUTABLE_WRITE_TYPES:
            return False, f"Operation {i} has unsupported type: {op_type!r}"

        if op_type == "create_event":
            if not op.get("title"):
                return False, f"Operation {i} (create_event) missing required field: title"
            if not op.get("start_time"):
                return False, f"Operation {i} (create_event) missing required field: start_time"
            if not op.get("end_time"):
                return False, f"Operation {i} (create_event) missing required field: end_time"
            try:
                start_dt = _parse_naive_dt(op["start_time"])
                end_dt = _parse_naive_dt(op["end_time"])
            except ValueError as exc:
                return False, f"Operation {i} (create_event): unparseable datetime — {exc}"
            if end_dt <= start_dt:
                return False, (
                    f"Operation {i} (create_event): end_time must be after start_time "
                    f"(got start={op['start_time']!r}, end={op['end_time']!r})"
                )

        elif op_type == "update_event":
            if not op.get("event_id"):
                return False, f"Operation {i} (update_event) missing required field: event_id"
            update_fields = {k for k in ("title", "start_time", "end_time", "location", "description")
                             if op.get(k) is not None}
            if not update_fields:
                return False, f"Operation {i} (update_event) has no fields to update"

        elif op_type == "delete_event":
            if not op.get("event_id"):
                return False, f"Operation {i} (delete_event) missing required field: event_id"

    return True, ""


def execute_calendar_operation(
    operation: dict,
    config: GoogleCalendarConfig,
    service: Any,
) -> GoogleCalendarResult:
    """Execute a single calendar write operation via the adapter."""
    op_type = operation.get("operation_type", "")
    cal_id = operation.get("calendar_id") or config.default_calendar_id

    if op_type in _NON_WRITE_TYPES:
        return GoogleCalendarResult(
            success=True,
            dry_run=False,
            operation_type=op_type,
            calendar_id=cal_id,
            event_id=None,
            message=f"Operation type '{op_type}' is read-only and was skipped (not a write).",
            event=None,
            raw=operation,
        )

    if op_type == "create_event":
        return create_calendar_event(
            service=service,
            config=config,
            title=operation["title"],
            start_time=operation["start_time"],
            end_time=operation["end_time"],
            calendar_id=cal_id or None,
            location=operation.get("location"),
            description=operation.get("description"),
        )

    if op_type == "update_event":
        return update_calendar_event(
            service=service,
            config=config,
            event_id=operation["event_id"],
            calendar_id=cal_id or None,
            title=operation.get("title"),
            start_time=operation.get("start_time"),
            end_time=operation.get("end_time"),
            location=operation.get("location"),
            description=operation.get("description"),
        )

    if op_type == "delete_event":
        return delete_calendar_event(
            service=service,
            config=config,
            event_id=operation["event_id"],
            calendar_id=cal_id or None,
        )

    raise ValueError(f"Unhandled operation type: {op_type!r}")


def write_calendar_execution_result(request_id: str, result: dict) -> Path:
    """Write execution result to COMPLETED or FAILED dir depending on success."""
    if result.get("success"):
        dest = COMPLETED_LUMEN_DIR / f"completed_{request_id}.json"
    else:
        dest = FAILED_LUMEN_DIR / f"failed_{request_id}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return dest


def execute_approved_calendar_request(request_id: str) -> dict:
    """
    Execute an approved Lumen calendar request against the live Google Calendar API.

    Pre-conditions (all checked before any network call):
    - Approval record exists in APPROVED_LUMEN_DIR.
    - Reviewed result exists in REVIEWED_LUMEN_DIR.
    - Pending proposal exists in PENDING_LUMEN_DIR.
    - GOOGLE_CALENDAR_ENABLED=true.
    - GOOGLE_CALENDAR_DRY_RUN=false (blocked with clear message if true).
    - All operations pass validation.

    Writes result to COMPLETED_LUMEN_DIR on success, FAILED_LUMEN_DIR on failure.
    """
    ensure_lumen_executor_dirs()
    request_id = request_id.strip()
    executed_at = datetime.now(timezone.utc).isoformat()

    def _fail(reason: str) -> dict:
        result = {
            "request_id": request_id,
            "executed_at": executed_at,
            "success": False,
            "reason": reason,
            "operation_count": 0,
            "operation_results": [],
        }
        write_calendar_execution_result(request_id, result)
        return result

    # 1. Load approval record
    approval = _load_approval_record(request_id)
    if approval is None:
        return _fail(f"No approval record found for {request_id!r}. Run approve first.")

    if not approval.get("approved"):
        return _fail(f"Approval record for {request_id!r} does not have approved=true.")

    # 2. Load reviewed result
    reviewed = load_reviewed_calendar_request(request_id)
    if reviewed is None:
        return _fail(f"Reviewed result not found for {request_id!r}.")

    if not reviewed.get("all_dry_run"):
        return _fail("Reviewed result missing all_dry_run=true. Refusing to execute.")

    # 3. Load operations from original_operations preserved during dry-run review
    operations = reviewed.get("original_operations")
    if operations is None:
        return _fail(
            "Reviewed request is missing original_operations. "
            "Rerun dry-run review before execution: "
            f"python -m prometheus.calendar.lumen_router --dry-run-request {request_id}"
        )
    if not isinstance(operations, list) or len(operations) == 0:
        return _fail("Reviewed request has no operations in original_operations.")

    # 4. Load and check config
    config = load_google_calendar_config()
    if not config.enabled:
        return _fail(
            "Calendar execution is blocked: GOOGLE_CALENDAR_ENABLED is not true. "
            "Set GOOGLE_CALENDAR_ENABLED=true in your environment to enable writes."
        )
    if config.dry_run:
        return _fail(
            "Calendar execution is blocked because GOOGLE_CALENDAR_DRY_RUN=true. "
            "Set GOOGLE_CALENDAR_DRY_RUN=false to allow live writes."
        )

    # 5. Validate all operations before executing any
    valid, validation_error = _validate_operations(operations)
    if not valid:
        return _fail(f"Operation validation failed: {validation_error}")

    # 6. Build service
    try:
        service = build_google_calendar_service(config, allow_interactive_auth=False)
    except Exception as exc:
        return _fail(f"Failed to build Google Calendar service: {exc}")

    # 7. Execute operations in order
    op_results: list[dict] = []
    overall_success = True
    for i, op in enumerate(operations):
        op_type = op.get("operation_type", "")
        try:
            gcal_result = execute_calendar_operation(op, config, service)
            op_results.append({
                "operation_index": i,
                "operation_type": op_type,
                "success": gcal_result.success,
                "dry_run": gcal_result.dry_run,
                "message": gcal_result.message,
                "event_id": gcal_result.event_id,
                "calendar_id": gcal_result.calendar_id,
            })
            if not gcal_result.success:
                overall_success = False
        except Exception as exc:
            status_code = None
            error_detail = str(exc)
            try:
                if hasattr(exc, 'resp') and hasattr(exc, 'content'):
                    status_code = getattr(exc.resp, 'status', None)
                    raw_content = exc.content
                    if isinstance(raw_content, bytes):
                        raw_content = raw_content.decode('utf-8', errors='replace')
                    try:
                        err_body = json.loads(raw_content)
                        error_detail = err_body.get('error', {}).get('message', str(exc))
                    except (json.JSONDecodeError, AttributeError):
                        error_detail = raw_content[:300]
            except Exception:
                pass
            msg = (
                f"HTTP {status_code}: {error_detail}"
                if status_code else f"Exception: {error_detail}"
            )
            op_results.append({
                "operation_index": i,
                "operation_type": op_type,
                "success": False,
                "dry_run": False,
                "message": msg,
                "status_code": status_code,
                "event_id": None,
                "calendar_id": op.get("calendar_id", config.default_calendar_id),
            })
            overall_success = False

    result = {
        "request_id": request_id,
        "executed_at": executed_at,
        "success": overall_success,
        "operation_count": len(operations),
        "operation_results": op_results,
        "message": (
            f"Executed {len(operations)} operation(s) successfully."
            if overall_success
            else "One or more operations failed during execution."
        ),
    }
    write_calendar_execution_result(request_id, result)
    return result


# ── Status helper ─────────────────────────────────────────────────────────────

def get_request_status(request_id: str) -> dict:
    """Return a summary of the current state of a calendar request."""
    request_id = request_id.strip()
    pending = _load_pending_proposal_raw(request_id)
    reviewed = load_reviewed_calendar_request(request_id)
    approval = _load_approval_record(request_id)

    # Check completed
    completed: Optional[dict] = None
    for d in (COMPLETED_LUMEN_DIR, FAILED_LUMEN_DIR):
        if d.exists():
            for p in d.glob(f"*{request_id}*.json"):
                try:
                    completed = json.loads(p.read_text(encoding="utf-8"))
                    break
                except (OSError, json.JSONDecodeError):
                    pass

    return {
        "request_id": request_id,
        "pending": pending is not None,
        "reviewed": reviewed is not None,
        "approved": approval is not None and approval.get("approved", False),
        "executed": completed is not None,
        "execution_success": completed.get("success") if completed else None,
        "executed_at": completed.get("executed_at") if completed else None,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def _main(argv: list[str] | None = None) -> None:
    from prometheus.integrations.google_calendar import _load_project_dotenv
    _load_project_dotenv()

    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print(
            "Usage:\n"
            "  python -m prometheus.calendar.lumen_executor --list-reviewed\n"
            "  python -m prometheus.calendar.lumen_executor --approve REQUEST_ID\n"
            "  python -m prometheus.calendar.lumen_executor --execute-approved REQUEST_ID\n"
            "  python -m prometheus.calendar.lumen_executor --status REQUEST_ID"
        )
        sys.exit(1)

    cmd = args[0]

    if cmd == "--list-reviewed":
        requests = list_reviewed_calendar_requests()
        print(json.dumps({
            "ok": True,
            "count": len(requests),
            "requests": [
                {
                    "request_id": r.get("request_id"),
                    "reviewed_at": r.get("reviewed_at"),
                    "operation_count": r.get("operation_count"),
                    "all_dry_run": r.get("all_dry_run"),
                    "proposal_reason": r.get("proposal_reason"),
                }
                for r in requests
            ],
        }, indent=2))

    elif cmd == "--approve":
        if len(args) < 2:
            print(json.dumps({"ok": False, "error": "--approve requires REQUEST_ID"}))
            sys.exit(1)
        result = approve_calendar_request(args[1])
        print(json.dumps(result, indent=2))
        if not result.get("ok"):
            sys.exit(1)

    elif cmd == "--execute-approved":
        if len(args) < 2:
            print(json.dumps({"ok": False, "error": "--execute-approved requires REQUEST_ID"}))
            sys.exit(1)
        result = execute_approved_calendar_request(args[1])
        print(json.dumps(result, indent=2))
        if not result.get("success"):
            sys.exit(1)

    elif cmd == "--status":
        if len(args) < 2:
            print(json.dumps({"ok": False, "error": "--status requires REQUEST_ID"}))
            sys.exit(1)
        result = get_request_status(args[1])
        print(json.dumps(result, indent=2))

    else:
        print(json.dumps({"ok": False, "error": f"Unknown command: {cmd!r}"}))
        sys.exit(1)


if __name__ == "__main__":
    _main()

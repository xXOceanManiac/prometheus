"""
prometheus/agents/lumen_calendar_router.py — Dry-run review router for Lumen calendar proposals.

Reads pending Lumen calendar proposals from runtime/pending/lumen_calendar/,
performs dry-run review via the Google Calendar adapter, and writes results to
runtime/reviewed/lumen_calendar/.

NO live calendar writes occur here. All reviews are dry-run only.
NO Google API calls are made. Only dry_run_calendar_operation() is called.
NO subprocess/shell execution.
"""
from __future__ import annotations

import dataclasses
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from prometheus.agents.lumen_ingestion import (
    PendingCalendarProposal,
    list_pending_lumen_calendar_proposals,
)
from prometheus.integrations.google_calendar import (
    GoogleCalendarConfig,
    dry_run_calendar_operation,
    load_google_calendar_config,
)
from prometheus.infra.paths import (
    PENDING_LUMEN_DIR,
    REVIEWED_LUMEN_DIR,
    ensure_lumen_router_dirs,
)


# ── Proposal loading ──────────────────────────────────────────────────────────

def load_pending_lumen_proposal(request_id: str) -> Optional[PendingCalendarProposal]:
    """Load a single pending proposal by request_id. Returns None if not found."""
    if not PENDING_LUMEN_DIR.exists():
        return None
    candidate = PENDING_LUMEN_DIR / f"pending_{request_id}.json"
    if not candidate.exists():
        # Also search by partial match
        matches = list(PENDING_LUMEN_DIR.glob(f"pending_*{request_id}*.json"))
        if not matches:
            return None
        candidate = matches[0]
    try:
        data = json.loads(candidate.read_text(encoding="utf-8"))
        return PendingCalendarProposal(**data)
    except (OSError, json.JSONDecodeError, TypeError):
        return None


# ── Review result writing ─────────────────────────────────────────────────────

def write_lumen_review_result(request_id: str, result: dict) -> Path:
    """Write a review result to runtime/reviewed/lumen_calendar/."""
    ensure_lumen_router_dirs()
    dest = REVIEWED_LUMEN_DIR / f"reviewed_{request_id}.json"
    dest.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return dest


def list_reviewed_lumen_calendar_proposals() -> list[dict]:
    """Return all written review results from runtime/reviewed/lumen_calendar/."""
    if not REVIEWED_LUMEN_DIR.exists():
        return []
    results = []
    for p in sorted(REVIEWED_LUMEN_DIR.glob("reviewed_*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            results.append(data)
        except (OSError, json.JSONDecodeError):
            continue
    return results


# ── Dry-run review ────────────────────────────────────────────────────────────

def review_lumen_proposal_dry_run(
    proposal_id_or_request_id: str,
    config: Optional[GoogleCalendarConfig] = None,
    write_result: bool = True,
) -> dict:
    """
    Dry-run review a single pending Lumen proposal.

    Runs dry_run_calendar_operation() on each operation. Does NOT call Google API.
    Returns a review result dict. If write_result=True, persists to reviewed/.
    """
    if config is None:
        config = load_google_calendar_config()

    proposal = load_pending_lumen_proposal(proposal_id_or_request_id)
    if proposal is None:
        return {
            "request_id": proposal_id_or_request_id,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "error": f"Proposal not found: {proposal_id_or_request_id!r}",
            "all_dry_run": True,
            "results": [],
        }

    reviewed_at = datetime.now(timezone.utc).isoformat()
    op_results = []
    for i, operation in enumerate(proposal.operations):
        gcal_result = dry_run_calendar_operation(operation, config)
        op_results.append({
            "operation_index": i,
            "operation_type": operation.get("operation_type", "unknown"),
            "success": gcal_result.success,
            "dry_run": gcal_result.dry_run,
            "message": gcal_result.message,
            "calendar_id": gcal_result.calendar_id,
            "event_id": gcal_result.event_id,
        })

    review = {
        "request_id": proposal.request_id,
        "reviewed_at": reviewed_at,
        "proposal_reason": proposal.reason,
        "source": proposal.source,
        "operation_count": proposal.operation_count,
        "all_dry_run": True,
        "approved": False,
        "results": op_results,
    }

    if write_result:
        write_lumen_review_result(proposal.request_id, review)

    return review


def review_pending_lumen_proposals_dry_run(
    config: Optional[GoogleCalendarConfig] = None,
    write_results: bool = True,
) -> list[dict]:
    """
    Dry-run review all pending Lumen proposals.

    Returns a list of review result dicts. Persists each result to reviewed/
    if write_results=True.
    """
    if config is None:
        config = load_google_calendar_config()

    proposals = list_pending_lumen_calendar_proposals()
    if not proposals:
        return []

    return [
        review_lumen_proposal_dry_run(
            p.request_id,
            config=config,
            write_result=write_results,
        )
        for p in proposals
    ]


# ── CLI entry point ───────────────────────────────────────────────────────────

def _main(argv: list[str] | None = None) -> None:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print(
            "Usage: python -m prometheus.agents.lumen_calendar_router "
            "--dry-run-all | --dry-run-request REQUEST_ID | --list-reviewed"
        )
        sys.exit(1)

    cmd = args[0]

    if cmd == "--dry-run-all":
        results = review_pending_lumen_proposals_dry_run()
        print(json.dumps(results, indent=2))

    elif cmd == "--dry-run-request":
        if len(args) < 2:
            print("Usage: --dry-run-request REQUEST_ID", file=sys.stderr)
            sys.exit(1)
        result = review_lumen_proposal_dry_run(args[1])
        print(json.dumps(result, indent=2))

    elif cmd == "--list-reviewed":
        reviews = list_reviewed_lumen_calendar_proposals()
        print(json.dumps(reviews, indent=2))

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    _main()

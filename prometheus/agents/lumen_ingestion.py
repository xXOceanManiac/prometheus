"""
prometheus/agents/lumen_ingestion.py — Lumen calendar-request ingestion agent.

Discovers, validates, and archives Lumen outbox requests into Prometheus pending
proposals. Does NOT execute calendar operations or call any external API.
"""
from __future__ import annotations

import dataclasses
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from prometheus.infra.paths import (
    LUMEN_OUTBOX_DIR,
    LUMEN_ACCEPTED_DIR,
    LUMEN_REJECTED_DIR,
    PENDING_LUMEN_DIR,
    ensure_lumen_ingestion_dirs,
)

_VALID_OPERATION_TYPES = frozenset({
    "create_event",
    "update_event",
    "delete_event",
    "read_events",
    "find_availability",
    "suggest_schedule_change",
})

_SUSPICIOUS_KEYS = frozenset({
    "command",
    "shell",
    "subprocess",
    "exec",
    "eval",
    "url",
    "token",
    "api_key",
    "home_assistant",
    "ha_service",
})


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class LumenIngestionResult:
    source_path: str
    request_id: Optional[str]
    status: str
    reason: str
    operation_count: int
    destination_path: Optional[str]


@dataclass
class PendingCalendarProposal:
    request_id: str
    source: str
    reason: str
    operation_count: int
    operations: list
    created_at: str
    ingested_at: str
    source_path: str


# ── Validation ────────────────────────────────────────────────────────────────

def validate_lumen_calendar_request(payload: dict) -> tuple[bool, str]:
    if not isinstance(payload, dict):
        return False, "Payload is not a dict."
    if payload.get("source") != "lumen":
        return False, f"source must be 'lumen', got {payload.get('source')!r}."
    request_id = payload.get("request_id", "")
    if not isinstance(request_id, str) or not request_id.strip():
        return False, "request_id must be a non-empty string."
    if not payload.get("requires_prometheus_approval", False):
        return False, "requires_prometheus_approval must be True."
    created_at = payload.get("created_at", "")
    if not isinstance(created_at, str) or not created_at.strip():
        return False, "created_at must be a non-empty string."
    operations = payload.get("operations")
    if not isinstance(operations, list) or len(operations) == 0:
        return False, "operations must be a non-empty list."
    for i, op in enumerate(operations):
        if not isinstance(op, dict):
            return False, f"Operation[{i}] is not a dict."
        if not op.get("requires_prometheus_approval", False):
            return False, f"Operation[{i}] requires_prometheus_approval must be True."
        if not op.get("dry_run", False):
            return False, f"Operation[{i}] dry_run must be True."
        op_type = op.get("operation_type", "")
        if op_type not in _VALID_OPERATION_TYPES:
            return False, (
                f"Operation[{i}] has unsupported operation_type {op_type!r}. "
                f"Allowed: {sorted(_VALID_OPERATION_TYPES)}."
            )
        for key in op:
            if key in _SUSPICIOUS_KEYS:
                return False, f"Operation[{i}] contains suspicious key {key!r}."
    return True, "OK"


# ── Ingestion ─────────────────────────────────────────────────────────────────

def ingest_lumen_outbox_once() -> list[LumenIngestionResult]:
    ensure_lumen_ingestion_dirs()

    if not LUMEN_OUTBOX_DIR.exists():
        return []

    results: list[LumenIngestionResult] = []
    candidates = sorted(LUMEN_OUTBOX_DIR.glob("lumen_calendar_request_*.json"))

    for src_path in candidates:
        try:
            raw = src_path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            dest = LUMEN_REJECTED_DIR / src_path.name
            try:
                src_path.rename(dest)
            except OSError:
                dest = src_path
            results.append(LumenIngestionResult(
                source_path=str(src_path),
                request_id=None,
                status="rejected",
                reason=f"Could not read/parse JSON: {exc}",
                operation_count=0,
                destination_path=str(dest),
            ))
            continue

        ok, reason = validate_lumen_calendar_request(payload)
        request_id = payload.get("request_id") if isinstance(payload, dict) else None
        operations = payload.get("operations", []) if isinstance(payload, dict) else []

        if not ok:
            dest = LUMEN_REJECTED_DIR / src_path.name
            try:
                src_path.rename(dest)
            except OSError:
                dest = src_path
            results.append(LumenIngestionResult(
                source_path=str(src_path),
                request_id=request_id,
                status="rejected",
                reason=reason,
                operation_count=len(operations) if isinstance(operations, list) else 0,
                destination_path=str(dest),
            ))
            continue

        ingested_at = datetime.now(timezone.utc).isoformat()
        proposal = PendingCalendarProposal(
            request_id=request_id,
            source=payload.get("source", "lumen"),
            reason=payload.get("reason", ""),
            operation_count=len(operations),
            operations=operations,
            created_at=payload.get("created_at", ""),
            ingested_at=ingested_at,
            source_path=str(src_path),
        )
        proposal_filename = f"pending_{request_id}.json"
        proposal_path = PENDING_LUMEN_DIR / proposal_filename
        proposal_path.write_text(
            json.dumps(dataclasses.asdict(proposal), indent=2),
            encoding="utf-8",
        )

        accepted_dest = LUMEN_ACCEPTED_DIR / src_path.name
        try:
            src_path.rename(accepted_dest)
        except OSError:
            accepted_dest = src_path

        results.append(LumenIngestionResult(
            source_path=str(src_path),
            request_id=request_id,
            status="accepted",
            reason="Validated and stored as pending proposal.",
            operation_count=len(operations),
            destination_path=str(proposal_path),
        ))

    return results


def list_pending_lumen_calendar_proposals() -> list[PendingCalendarProposal]:
    if not PENDING_LUMEN_DIR.exists():
        return []
    proposals: list[PendingCalendarProposal] = []
    for p in sorted(PENDING_LUMEN_DIR.glob("pending_*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            proposals.append(PendingCalendarProposal(**data))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
    return proposals


# ── CLI entry point ───────────────────────────────────────────────────────────

def _main(argv: list[str] | None = None) -> None:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("Usage: python -m prometheus.agents.lumen_ingestion --ingest-once | --list-pending")
        sys.exit(1)

    cmd = args[0]
    if cmd == "--ingest-once":
        results = ingest_lumen_outbox_once()
        print(json.dumps([dataclasses.asdict(r) for r in results], indent=2))
    elif cmd == "--list-pending":
        proposals = list_pending_lumen_calendar_proposals()
        print(json.dumps([dataclasses.asdict(p) for p in proposals], indent=2))
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    _main()

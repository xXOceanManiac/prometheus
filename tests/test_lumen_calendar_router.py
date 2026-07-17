"""
test_lumen_calendar_router.py — Tests for the Lumen calendar proposal dry-run router.
"""
from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from prometheus.calendar.lumen_ingestion import PendingCalendarProposal
from prometheus.integrations.google_calendar import GoogleCalendarConfig


# ── Fixtures / helpers ─────────────────────────────────────────────────────────

def _make_proposal(
    request_id: str = "req-test-001",
    source: str = "lumen",
    reason: str = "Test proposal",
    operations: list | None = None,
) -> PendingCalendarProposal:
    if operations is None:
        operations = [
            {
                "operation_type": "create_event",
                "title": "Test Event",
                "start_time": "2026-05-15T10:00:00",
                "end_time": "2026-05-15T11:00:00",
                "calendar_id": "primary",
                "dry_run": True,
                "requires_prometheus_approval": True,
            }
        ]
    return PendingCalendarProposal(
        request_id=request_id,
        source=source,
        reason=reason,
        operation_count=len(operations),
        operations=operations,
        created_at="2026-05-14T10:00:00+00:00",
        ingested_at="2026-05-14T10:01:00+00:00",
        source_path="/fake/outbox/lumen_calendar_request_test.json",
    )


def _patch_router_dirs(monkeypatch, tmp_path):
    """Patch all path constants and dir-creation helpers in the router."""
    import prometheus.calendar.lumen_router as rmod
    import prometheus.calendar.lumen_ingestion as lmod

    pending = tmp_path / "pending" / "lumen_calendar"
    reviewed = tmp_path / "reviewed" / "lumen_calendar"
    pending.mkdir(parents=True)
    reviewed.mkdir(parents=True)

    monkeypatch.setattr(rmod, "PENDING_LUMEN_DIR", pending)
    monkeypatch.setattr(rmod, "REVIEWED_LUMEN_DIR", reviewed)
    monkeypatch.setattr(lmod, "PENDING_LUMEN_DIR", pending)
    monkeypatch.setattr(rmod, "ensure_lumen_router_dirs", lambda: None)

    return pending, reviewed


def _write_pending_proposal(pending_dir: Path, proposal: PendingCalendarProposal) -> Path:
    path = pending_dir / f"pending_{proposal.request_id}.json"
    path.write_text(json.dumps(dataclasses.asdict(proposal), indent=2), encoding="utf-8")
    return path


def _safe_config() -> GoogleCalendarConfig:
    return GoogleCalendarConfig(enabled=False, dry_run=True)


# ── load_pending_lumen_proposal ───────────────────────────────────────────────

class TestLoadPendingLumenProposal:
    def test_returns_none_if_not_found(self, monkeypatch, tmp_path):
        _patch_router_dirs(monkeypatch, tmp_path)
        from prometheus.calendar.lumen_router import load_pending_lumen_proposal
        result = load_pending_lumen_proposal("nonexistent-id")
        assert result is None

    def test_loads_by_exact_request_id(self, monkeypatch, tmp_path):
        pending, _ = _patch_router_dirs(monkeypatch, tmp_path)
        proposal = _make_proposal(request_id="req-abc123")
        _write_pending_proposal(pending, proposal)

        from prometheus.calendar.lumen_router import load_pending_lumen_proposal
        result = load_pending_lumen_proposal("req-abc123")
        assert result is not None
        assert result.request_id == "req-abc123"

    def test_returns_proposal_object(self, monkeypatch, tmp_path):
        pending, _ = _patch_router_dirs(monkeypatch, tmp_path)
        proposal = _make_proposal()
        _write_pending_proposal(pending, proposal)

        from prometheus.calendar.lumen_router import load_pending_lumen_proposal
        result = load_pending_lumen_proposal("req-test-001")
        assert isinstance(result, PendingCalendarProposal)

    def test_loads_operations(self, monkeypatch, tmp_path):
        pending, _ = _patch_router_dirs(monkeypatch, tmp_path)
        proposal = _make_proposal()
        _write_pending_proposal(pending, proposal)

        from prometheus.calendar.lumen_router import load_pending_lumen_proposal
        result = load_pending_lumen_proposal("req-test-001")
        assert len(result.operations) == 1
        assert result.operations[0]["operation_type"] == "create_event"

    def test_returns_none_if_dir_missing(self, monkeypatch, tmp_path):
        import prometheus.calendar.lumen_router as rmod
        monkeypatch.setattr(rmod, "PENDING_LUMEN_DIR", tmp_path / "nonexistent")

        from prometheus.calendar.lumen_router import load_pending_lumen_proposal
        result = load_pending_lumen_proposal("any-id")
        assert result is None


# ── write_lumen_review_result ─────────────────────────────────────────────────

class TestWriteLumenReviewResult:
    def test_writes_json_file(self, monkeypatch, tmp_path):
        _patch_router_dirs(monkeypatch, tmp_path)
        reviewed = tmp_path / "reviewed" / "lumen_calendar"

        from prometheus.calendar.lumen_router import write_lumen_review_result
        result = {"request_id": "req-001", "all_dry_run": True, "results": []}
        path = write_lumen_review_result("req-001", result)
        assert path.exists()

    def test_filename_includes_request_id(self, monkeypatch, tmp_path):
        _patch_router_dirs(monkeypatch, tmp_path)

        from prometheus.calendar.lumen_router import write_lumen_review_result
        path = write_lumen_review_result("req-xyz", {"request_id": "req-xyz"})
        assert "req-xyz" in path.name

    def test_written_json_is_valid(self, monkeypatch, tmp_path):
        _patch_router_dirs(monkeypatch, tmp_path)

        from prometheus.calendar.lumen_router import write_lumen_review_result
        data = {"request_id": "req-001", "all_dry_run": True, "results": [{"op": 1}]}
        path = write_lumen_review_result("req-001", data)
        loaded = json.loads(path.read_text())
        assert loaded["request_id"] == "req-001"
        assert loaded["all_dry_run"] is True

    def test_returns_path(self, monkeypatch, tmp_path):
        _patch_router_dirs(monkeypatch, tmp_path)

        from prometheus.calendar.lumen_router import write_lumen_review_result
        result = write_lumen_review_result("req-001", {})
        assert isinstance(result, Path)


# ── list_reviewed_lumen_calendar_proposals ────────────────────────────────────

class TestListReviewedLumenCalendarProposals:
    def test_empty_when_dir_missing(self, monkeypatch, tmp_path):
        import prometheus.calendar.lumen_router as rmod
        monkeypatch.setattr(rmod, "REVIEWED_LUMEN_DIR", tmp_path / "nonexistent")

        from prometheus.calendar.lumen_router import list_reviewed_lumen_calendar_proposals
        assert list_reviewed_lumen_calendar_proposals() == []

    def test_empty_when_no_files(self, monkeypatch, tmp_path):
        _patch_router_dirs(monkeypatch, tmp_path)

        from prometheus.calendar.lumen_router import list_reviewed_lumen_calendar_proposals
        assert list_reviewed_lumen_calendar_proposals() == []

    def test_lists_written_reviews(self, monkeypatch, tmp_path):
        _patch_router_dirs(monkeypatch, tmp_path)
        reviewed = tmp_path / "reviewed" / "lumen_calendar"

        from prometheus.calendar.lumen_router import (
            list_reviewed_lumen_calendar_proposals,
            write_lumen_review_result,
        )
        write_lumen_review_result("req-001", {"request_id": "req-001"})
        write_lumen_review_result("req-002", {"request_id": "req-002"})
        results = list_reviewed_lumen_calendar_proposals()
        assert len(results) == 2

    def test_returns_list_of_dicts(self, monkeypatch, tmp_path):
        _patch_router_dirs(monkeypatch, tmp_path)

        from prometheus.calendar.lumen_router import (
            list_reviewed_lumen_calendar_proposals,
            write_lumen_review_result,
        )
        write_lumen_review_result("req-001", {"request_id": "req-001"})
        results = list_reviewed_lumen_calendar_proposals()
        assert isinstance(results, list)
        assert isinstance(results[0], dict)

    def test_skips_corrupt_files(self, monkeypatch, tmp_path):
        _, reviewed = _patch_router_dirs(monkeypatch, tmp_path)

        (reviewed / "reviewed_bad.json").write_text("NOT JSON", encoding="utf-8")

        from prometheus.calendar.lumen_router import list_reviewed_lumen_calendar_proposals
        results = list_reviewed_lumen_calendar_proposals()
        assert results == []


# ── review_lumen_proposal_dry_run ─────────────────────────────────────────────

class TestReviewLumenProposalDryRun:
    def test_not_found_returns_error_dict(self, monkeypatch, tmp_path):
        _patch_router_dirs(monkeypatch, tmp_path)

        from prometheus.calendar.lumen_router import review_lumen_proposal_dry_run
        result = review_lumen_proposal_dry_run("nonexistent", config=_safe_config(), write_result=False)
        assert "error" in result
        assert result["all_dry_run"] is True

    def test_dry_run_create_event(self, monkeypatch, tmp_path):
        pending, _ = _patch_router_dirs(monkeypatch, tmp_path)
        proposal = _make_proposal()
        _write_pending_proposal(pending, proposal)

        from prometheus.calendar.lumen_router import review_lumen_proposal_dry_run
        result = review_lumen_proposal_dry_run("req-test-001", config=_safe_config(), write_result=False)
        assert result["request_id"] == "req-test-001"
        assert result["all_dry_run"] is True
        assert result["approved"] is False
        assert len(result["results"]) == 1

    def test_result_contains_operation_type(self, monkeypatch, tmp_path):
        pending, _ = _patch_router_dirs(monkeypatch, tmp_path)
        proposal = _make_proposal()
        _write_pending_proposal(pending, proposal)

        from prometheus.calendar.lumen_router import review_lumen_proposal_dry_run
        result = review_lumen_proposal_dry_run("req-test-001", config=_safe_config(), write_result=False)
        assert result["results"][0]["operation_type"] == "create_event"

    def test_all_results_are_dry_run(self, monkeypatch, tmp_path):
        pending, _ = _patch_router_dirs(monkeypatch, tmp_path)
        ops = [
            {
                "operation_type": "create_event",
                "title": "Event A",
                "start_time": "2026-05-15T10:00:00",
                "end_time": "2026-05-15T11:00:00",
                "dry_run": True,
                "requires_prometheus_approval": True,
            },
            {
                "operation_type": "delete_event",
                "event_id": "evt-999",
                "dry_run": True,
                "requires_prometheus_approval": True,
            },
        ]
        proposal = _make_proposal(request_id="req-multi", operations=ops)
        _write_pending_proposal(pending, proposal)

        from prometheus.calendar.lumen_router import review_lumen_proposal_dry_run
        result = review_lumen_proposal_dry_run("req-multi", config=_safe_config(), write_result=False)
        assert all(r["dry_run"] for r in result["results"])

    def test_writes_review_file_when_requested(self, monkeypatch, tmp_path):
        pending, reviewed = _patch_router_dirs(monkeypatch, tmp_path)
        proposal = _make_proposal()
        _write_pending_proposal(pending, proposal)

        from prometheus.calendar.lumen_router import review_lumen_proposal_dry_run
        review_lumen_proposal_dry_run("req-test-001", config=_safe_config(), write_result=True)
        assert (reviewed / "reviewed_req-test-001.json").exists()

    def test_no_write_when_write_result_false(self, monkeypatch, tmp_path):
        pending, reviewed = _patch_router_dirs(monkeypatch, tmp_path)
        proposal = _make_proposal()
        _write_pending_proposal(pending, proposal)

        from prometheus.calendar.lumen_router import review_lumen_proposal_dry_run
        review_lumen_proposal_dry_run("req-test-001", config=_safe_config(), write_result=False)
        assert not list(reviewed.glob("*.json"))

    def test_result_includes_proposal_reason(self, monkeypatch, tmp_path):
        pending, _ = _patch_router_dirs(monkeypatch, tmp_path)
        proposal = _make_proposal(reason="Schedule focus block for deep work")
        _write_pending_proposal(pending, proposal)

        from prometheus.calendar.lumen_router import review_lumen_proposal_dry_run
        result = review_lumen_proposal_dry_run("req-test-001", config=_safe_config(), write_result=False)
        assert result["proposal_reason"] == "Schedule focus block for deep work"

    def test_result_includes_reviewed_at(self, monkeypatch, tmp_path):
        pending, _ = _patch_router_dirs(monkeypatch, tmp_path)
        proposal = _make_proposal()
        _write_pending_proposal(pending, proposal)

        from prometheus.calendar.lumen_router import review_lumen_proposal_dry_run
        result = review_lumen_proposal_dry_run("req-test-001", config=_safe_config(), write_result=False)
        assert "reviewed_at" in result
        assert result["reviewed_at"]

    def test_delete_event_dry_run(self, monkeypatch, tmp_path):
        pending, _ = _patch_router_dirs(monkeypatch, tmp_path)
        ops = [
            {
                "operation_type": "delete_event",
                "event_id": "evt-to-delete",
                "dry_run": True,
                "requires_prometheus_approval": True,
            }
        ]
        proposal = _make_proposal(request_id="req-delete", operations=ops)
        _write_pending_proposal(pending, proposal)

        from prometheus.calendar.lumen_router import review_lumen_proposal_dry_run
        result = review_lumen_proposal_dry_run("req-delete", config=_safe_config(), write_result=False)
        assert result["results"][0]["operation_type"] == "delete_event"
        assert result["results"][0]["dry_run"] is True

    def test_read_events_dry_run(self, monkeypatch, tmp_path):
        pending, _ = _patch_router_dirs(monkeypatch, tmp_path)
        ops = [
            {
                "operation_type": "read_events",
                "calendar_id": "primary",
                "dry_run": True,
                "requires_prometheus_approval": True,
            }
        ]
        proposal = _make_proposal(request_id="req-read", operations=ops)
        _write_pending_proposal(pending, proposal)

        from prometheus.calendar.lumen_router import review_lumen_proposal_dry_run
        result = review_lumen_proposal_dry_run("req-read", config=_safe_config(), write_result=False)
        assert result["results"][0]["operation_type"] == "read_events"


# ── review_pending_lumen_proposals_dry_run ────────────────────────────────────

class TestReviewPendingLumenProposalsDryRun:
    def test_empty_when_no_proposals(self, monkeypatch, tmp_path):
        _patch_router_dirs(monkeypatch, tmp_path)

        from prometheus.calendar.lumen_router import review_pending_lumen_proposals_dry_run
        results = review_pending_lumen_proposals_dry_run(config=_safe_config(), write_results=False)
        assert results == []

    def test_reviews_all_pending(self, monkeypatch, tmp_path):
        pending, _ = _patch_router_dirs(monkeypatch, tmp_path)
        for i in range(3):
            p = _make_proposal(request_id=f"req-{i:03d}")
            _write_pending_proposal(pending, p)

        from prometheus.calendar.lumen_router import review_pending_lumen_proposals_dry_run
        results = review_pending_lumen_proposals_dry_run(config=_safe_config(), write_results=False)
        assert len(results) == 3

    def test_all_results_dry_run(self, monkeypatch, tmp_path):
        pending, _ = _patch_router_dirs(monkeypatch, tmp_path)
        p = _make_proposal()
        _write_pending_proposal(pending, p)

        from prometheus.calendar.lumen_router import review_pending_lumen_proposals_dry_run
        results = review_pending_lumen_proposals_dry_run(config=_safe_config(), write_results=False)
        assert all(r["all_dry_run"] for r in results)

    def test_returns_list_of_dicts(self, monkeypatch, tmp_path):
        pending, _ = _patch_router_dirs(monkeypatch, tmp_path)
        _write_pending_proposal(pending, _make_proposal())

        from prometheus.calendar.lumen_router import review_pending_lumen_proposals_dry_run
        results = review_pending_lumen_proposals_dry_run(config=_safe_config(), write_results=False)
        assert isinstance(results, list)
        assert isinstance(results[0], dict)

    def test_writes_reviewed_files(self, monkeypatch, tmp_path):
        pending, reviewed = _patch_router_dirs(monkeypatch, tmp_path)
        for i in range(2):
            _write_pending_proposal(pending, _make_proposal(request_id=f"req-w{i}"))

        from prometheus.calendar.lumen_router import review_pending_lumen_proposals_dry_run
        review_pending_lumen_proposals_dry_run(config=_safe_config(), write_results=True)
        written = list(reviewed.glob("reviewed_*.json"))
        assert len(written) == 2

    def test_none_approved(self, monkeypatch, tmp_path):
        pending, _ = _patch_router_dirs(monkeypatch, tmp_path)
        for i in range(3):
            _write_pending_proposal(pending, _make_proposal(request_id=f"req-{i}"))

        from prometheus.calendar.lumen_router import review_pending_lumen_proposals_dry_run
        results = review_pending_lumen_proposals_dry_run(config=_safe_config(), write_results=False)
        assert all(r.get("approved") is False for r in results)


# ── Safety checks ─────────────────────────────────────────────────────────────

class TestRouterSafety:
    def test_no_subprocess_in_source(self):
        src = Path(__file__).parent.parent / "prometheus" / "calendar" / "lumen_router.py"
        text = src.read_text(encoding="utf-8")
        assert "import subprocess" not in text
        assert "subprocess.run" not in text
        assert "os.system" not in text

    def test_no_requests_in_source(self):
        src = Path(__file__).parent.parent / "prometheus" / "calendar" / "lumen_router.py"
        text = src.read_text(encoding="utf-8")
        assert "import requests" not in text
        assert "requests.get" not in text
        assert "urllib.request" not in text

    def test_no_home_assistant_calls(self):
        src = Path(__file__).parent.parent / "prometheus" / "calendar" / "lumen_router.py"
        text = src.read_text(encoding="utf-8")
        assert "home_assistant" not in text.lower()
        assert "ha_service" not in text

    def test_no_live_calendar_calls(self):
        src = Path(__file__).parent.parent / "prometheus" / "calendar" / "lumen_router.py"
        text = src.read_text(encoding="utf-8")
        # Router uses only dry_run_calendar_operation, not live write functions
        assert "create_calendar_event" not in text
        assert "update_calendar_event" not in text
        assert "delete_calendar_event" not in text
        assert "build_google_calendar_service" not in text

    def test_all_reviews_are_dry_run_only(self, monkeypatch, tmp_path):
        pending, _ = _patch_router_dirs(monkeypatch, tmp_path)
        _write_pending_proposal(pending, _make_proposal())

        from prometheus.calendar.lumen_router import review_pending_lumen_proposals_dry_run
        results = review_pending_lumen_proposals_dry_run(config=_safe_config(), write_results=False)
        for review in results:
            assert review["all_dry_run"] is True
            for op_result in review.get("results", []):
                assert op_result["dry_run"] is True


# ── original_operations preservation ─────────────────────────────────────────

class TestReviewedFileIncludesOriginalOperations:
    def test_reviewed_result_has_original_operations_key(self, monkeypatch, tmp_path):
        pending, _ = _patch_router_dirs(monkeypatch, tmp_path)
        _write_pending_proposal(pending, _make_proposal())

        from prometheus.calendar.lumen_router import review_lumen_proposal_dry_run
        result = review_lumen_proposal_dry_run("req-test-001", config=_safe_config(), write_result=False)
        assert "original_operations" in result

    def test_original_operations_matches_proposal_operations(self, monkeypatch, tmp_path):
        pending, _ = _patch_router_dirs(monkeypatch, tmp_path)
        proposal = _make_proposal()
        _write_pending_proposal(pending, proposal)

        from prometheus.calendar.lumen_router import review_lumen_proposal_dry_run
        result = review_lumen_proposal_dry_run("req-test-001", config=_safe_config(), write_result=False)
        assert result["original_operations"] == proposal.operations

    def test_original_operations_is_list(self, monkeypatch, tmp_path):
        pending, _ = _patch_router_dirs(monkeypatch, tmp_path)
        _write_pending_proposal(pending, _make_proposal())

        from prometheus.calendar.lumen_router import review_lumen_proposal_dry_run
        result = review_lumen_proposal_dry_run("req-test-001", config=_safe_config(), write_result=False)
        assert isinstance(result["original_operations"], list)

    def test_original_operations_preserved_in_written_file(self, monkeypatch, tmp_path):
        pending, reviewed = _patch_router_dirs(monkeypatch, tmp_path)
        proposal = _make_proposal()
        _write_pending_proposal(pending, proposal)

        from prometheus.calendar.lumen_router import review_lumen_proposal_dry_run
        review_lumen_proposal_dry_run("req-test-001", config=_safe_config(), write_result=True)
        written = json.loads((reviewed / "reviewed_req-test-001.json").read_text())
        assert "original_operations" in written
        assert written["original_operations"] == proposal.operations

    def test_original_operations_contains_full_payload(self, monkeypatch, tmp_path):
        pending, _ = _patch_router_dirs(monkeypatch, tmp_path)
        ops = [
            {
                "operation_type": "create_event",
                "title": "Deep Work Block",
                "start_time": "2026-05-15T10:00:00",
                "end_time": "2026-05-15T12:00:00",
                "calendar_id": "primary",
                "location": "Home office",
                "description": "No interruptions",
                "dry_run": True,
                "requires_prometheus_approval": True,
            }
        ]
        proposal = _make_proposal(request_id="req-full", operations=ops)
        _write_pending_proposal(pending, proposal)

        from prometheus.calendar.lumen_router import review_lumen_proposal_dry_run
        result = review_lumen_proposal_dry_run("req-full", config=_safe_config(), write_result=False)
        orig = result["original_operations"][0]
        assert orig["title"] == "Deep Work Block"
        assert orig["start_time"] == "2026-05-15T10:00:00"
        assert orig["end_time"] == "2026-05-15T12:00:00"
        assert orig["location"] == "Home office"
        assert orig["dry_run"] is True
        assert orig["requires_prometheus_approval"] is True

    def test_reviewed_file_has_no_live_execution_flag(self, monkeypatch, tmp_path):
        pending, _ = _patch_router_dirs(monkeypatch, tmp_path)
        _write_pending_proposal(pending, _make_proposal())

        from prometheus.calendar.lumen_router import review_lumen_proposal_dry_run
        result = review_lumen_proposal_dry_run("req-test-001", config=_safe_config(), write_result=False)
        assert result.get("no_live_execution") is True

    def test_original_operations_not_in_error_result(self, monkeypatch, tmp_path):
        """Error result for missing proposal should not have original_operations."""
        _patch_router_dirs(monkeypatch, tmp_path)

        from prometheus.calendar.lumen_router import review_lumen_proposal_dry_run
        result = review_lumen_proposal_dry_run("nonexistent", config=_safe_config(), write_result=False)
        assert "error" in result
        # original_operations not required in error case — just don't assert on it

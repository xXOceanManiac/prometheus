"""
test_lumen_calendar_executor.py — Tests for prometheus/agents/lumen_calendar_executor.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _write_reviewed(reviewed_dir: Path, request_id: str, data: dict) -> None:
    reviewed_dir.mkdir(parents=True, exist_ok=True)
    (reviewed_dir / f"reviewed_{request_id}.json").write_text(json.dumps(data), encoding="utf-8")


def _write_pending(pending_dir: Path, request_id: str, data: dict) -> None:
    pending_dir.mkdir(parents=True, exist_ok=True)
    (pending_dir / f"pending_{request_id}.json").write_text(json.dumps(data), encoding="utf-8")


def _make_reviewed(request_id: str, all_dry_run: bool = True, all_success: bool = True) -> dict:
    return {
        "request_id": request_id,
        "reviewed_at": "2026-05-14T05:00:00+00:00",
        "proposal_reason": "Test",
        "source": "lumen",
        "operation_count": 1,
        "all_dry_run": all_dry_run,
        "approved": False,
        "results": [
            {
                "operation_index": 0,
                "operation_type": "create_event",
                "success": all_success,
                "dry_run": True,
                "message": "[DRY RUN] Would create event 'Test'.",
                "calendar_id": "primary",
                "event_id": None,
            }
        ],
    }


def _make_pending(request_id: str, op_type: str = "create_event",
                   requires_approval: bool = True, dry_run: bool = True) -> dict:
    op = {
        "operation_id": f"op-{request_id[:8]}",
        "operation_type": op_type,
        "calendar_id": "primary",
        "requires_prometheus_approval": requires_approval,
        "dry_run": dry_run,
    }
    if op_type == "create_event":
        op.update({"title": "Test event", "start_time": "2026-05-15T14:00", "end_time": "2026-05-15T15:00"})
    elif op_type in ("update_event", "delete_event"):
        op["event_id"] = "test-event-id-123"
        if op_type == "update_event":
            op["title"] = "Updated title"
    return {
        "request_id": request_id,
        "source": "lumen",
        "reason": "Test",
        "operation_count": 1,
        "operations": [op],
        "created_at": "2026-05-14T04:00:00+00:00",
    }


# ── Module import ─────────────────────────────────────────────────────────────

class TestModuleImport:
    def test_imports_cleanly(self):
        from prometheus.agents import lumen_calendar_executor  # noqa

    def test_functions_exist(self):
        from prometheus.agents.lumen_calendar_executor import (
            list_reviewed_calendar_requests,
            load_reviewed_calendar_request,
            approve_calendar_request,
            execute_approved_calendar_request,
            execute_calendar_operation,
            write_calendar_execution_result,
            get_request_status,
        )
        for fn in [list_reviewed_calendar_requests, load_reviewed_calendar_request,
                   approve_calendar_request, execute_approved_calendar_request,
                   execute_calendar_operation, write_calendar_execution_result, get_request_status]:
            assert callable(fn)


# ── list_reviewed_calendar_requests ──────────────────────────────────────────

class TestListReviewed:
    def test_empty_dir_returns_empty_list(self, tmp_path):
        fake = tmp_path / "reviewed" / "lumen_calendar"
        with patch("prometheus.agents.lumen_calendar_executor.REVIEWED_LUMEN_DIR", fake):
            from prometheus.agents import lumen_calendar_executor as ex
            assert ex.list_reviewed_calendar_requests() == []

    def test_missing_dir_returns_empty_list(self, tmp_path):
        fake = tmp_path / "nonexistent"
        with patch("prometheus.agents.lumen_calendar_executor.REVIEWED_LUMEN_DIR", fake):
            from prometheus.agents import lumen_calendar_executor as ex
            assert ex.list_reviewed_calendar_requests() == []

    def test_returns_reviewed_requests(self, tmp_path):
        reviewed_dir = tmp_path / "reviewed"
        _write_reviewed(reviewed_dir, "req-abc123", _make_reviewed("req-abc123"))
        _write_reviewed(reviewed_dir, "req-def456", _make_reviewed("req-def456"))
        with patch("prometheus.agents.lumen_calendar_executor.REVIEWED_LUMEN_DIR", reviewed_dir):
            from prometheus.agents import lumen_calendar_executor as ex
            requests = ex.list_reviewed_calendar_requests()
        assert len(requests) == 2

    def test_returns_list_of_dicts(self, tmp_path):
        reviewed_dir = tmp_path / "reviewed"
        _write_reviewed(reviewed_dir, "req-abc123", _make_reviewed("req-abc123"))
        with patch("prometheus.agents.lumen_calendar_executor.REVIEWED_LUMEN_DIR", reviewed_dir):
            from prometheus.agents import lumen_calendar_executor as ex
            requests = ex.list_reviewed_calendar_requests()
        assert isinstance(requests[0], dict)


# ── approve_calendar_request ──────────────────────────────────────────────────

class TestApproveCalendarRequest:
    def test_approve_fails_if_reviewed_missing(self, tmp_path):
        reviewed_dir = tmp_path / "reviewed"
        reviewed_dir.mkdir(parents=True)
        approved_dir = tmp_path / "approved"
        with (
            patch("prometheus.agents.lumen_calendar_executor.REVIEWED_LUMEN_DIR", reviewed_dir),
            patch("prometheus.agents.lumen_calendar_executor.APPROVED_LUMEN_DIR", approved_dir),
            patch("prometheus.agents.lumen_calendar_executor.COMPLETED_LUMEN_DIR", tmp_path / "completed"),
            patch("prometheus.agents.lumen_calendar_executor.FAILED_LUMEN_DIR", tmp_path / "failed"),
        ):
            from prometheus.agents import lumen_calendar_executor as ex
            result = ex.approve_calendar_request("req-nonexistent")
        assert not result["ok"]
        assert not result["approved"]
        assert "not found" in result["reason"].lower()

    def test_approve_fails_if_not_all_dry_run(self, tmp_path):
        reviewed_dir = tmp_path / "reviewed"
        pending_dir = tmp_path / "pending"
        approved_dir = tmp_path / "approved"
        reviewed = _make_reviewed("req-abc", all_dry_run=False)
        _write_reviewed(reviewed_dir, "req-abc", reviewed)
        _write_pending(pending_dir, "req-abc", _make_pending("req-abc"))
        with (
            patch("prometheus.agents.lumen_calendar_executor.REVIEWED_LUMEN_DIR", reviewed_dir),
            patch("prometheus.agents.lumen_calendar_executor.PENDING_LUMEN_DIR", pending_dir),
            patch("prometheus.agents.lumen_calendar_executor.APPROVED_LUMEN_DIR", approved_dir),
            patch("prometheus.agents.lumen_calendar_executor.COMPLETED_LUMEN_DIR", tmp_path / "completed"),
            patch("prometheus.agents.lumen_calendar_executor.FAILED_LUMEN_DIR", tmp_path / "failed"),
        ):
            from prometheus.agents import lumen_calendar_executor as ex
            result = ex.approve_calendar_request("req-abc")
        assert not result["ok"]
        assert "all_dry_run" in result["reason"]

    def test_approve_fails_if_review_had_failures(self, tmp_path):
        reviewed_dir = tmp_path / "reviewed"
        pending_dir = tmp_path / "pending"
        approved_dir = tmp_path / "approved"
        reviewed = _make_reviewed("req-abc", all_dry_run=True, all_success=False)
        _write_reviewed(reviewed_dir, "req-abc", reviewed)
        _write_pending(pending_dir, "req-abc", _make_pending("req-abc"))
        with (
            patch("prometheus.agents.lumen_calendar_executor.REVIEWED_LUMEN_DIR", reviewed_dir),
            patch("prometheus.agents.lumen_calendar_executor.PENDING_LUMEN_DIR", pending_dir),
            patch("prometheus.agents.lumen_calendar_executor.APPROVED_LUMEN_DIR", approved_dir),
            patch("prometheus.agents.lumen_calendar_executor.COMPLETED_LUMEN_DIR", tmp_path / "completed"),
            patch("prometheus.agents.lumen_calendar_executor.FAILED_LUMEN_DIR", tmp_path / "failed"),
        ):
            from prometheus.agents import lumen_calendar_executor as ex
            result = ex.approve_calendar_request("req-abc")
        assert not result["ok"]
        assert "failed" in result["reason"].lower()

    def test_approve_fails_if_pending_missing(self, tmp_path):
        reviewed_dir = tmp_path / "reviewed"
        pending_dir = tmp_path / "pending"
        pending_dir.mkdir(parents=True)
        approved_dir = tmp_path / "approved"
        _write_reviewed(reviewed_dir, "req-abc", _make_reviewed("req-abc"))
        with (
            patch("prometheus.agents.lumen_calendar_executor.REVIEWED_LUMEN_DIR", reviewed_dir),
            patch("prometheus.agents.lumen_calendar_executor.PENDING_LUMEN_DIR", pending_dir),
            patch("prometheus.agents.lumen_calendar_executor.APPROVED_LUMEN_DIR", approved_dir),
            patch("prometheus.agents.lumen_calendar_executor.COMPLETED_LUMEN_DIR", tmp_path / "completed"),
            patch("prometheus.agents.lumen_calendar_executor.FAILED_LUMEN_DIR", tmp_path / "failed"),
        ):
            from prometheus.agents import lumen_calendar_executor as ex
            result = ex.approve_calendar_request("req-abc")
        assert not result["ok"]
        assert "not found" in result["reason"].lower()

    def test_approve_fails_if_op_no_requires_approval(self, tmp_path):
        reviewed_dir = tmp_path / "reviewed"
        pending_dir = tmp_path / "pending"
        approved_dir = tmp_path / "approved"
        _write_reviewed(reviewed_dir, "req-abc", _make_reviewed("req-abc"))
        _write_pending(pending_dir, "req-abc", _make_pending("req-abc", requires_approval=False))
        with (
            patch("prometheus.agents.lumen_calendar_executor.REVIEWED_LUMEN_DIR", reviewed_dir),
            patch("prometheus.agents.lumen_calendar_executor.PENDING_LUMEN_DIR", pending_dir),
            patch("prometheus.agents.lumen_calendar_executor.APPROVED_LUMEN_DIR", approved_dir),
            patch("prometheus.agents.lumen_calendar_executor.COMPLETED_LUMEN_DIR", tmp_path / "completed"),
            patch("prometheus.agents.lumen_calendar_executor.FAILED_LUMEN_DIR", tmp_path / "failed"),
        ):
            from prometheus.agents import lumen_calendar_executor as ex
            result = ex.approve_calendar_request("req-abc")
        assert not result["ok"]
        assert "approval" in result["reason"].lower()

    def test_approve_fails_if_op_not_dry_run(self, tmp_path):
        reviewed_dir = tmp_path / "reviewed"
        pending_dir = tmp_path / "pending"
        approved_dir = tmp_path / "approved"
        _write_reviewed(reviewed_dir, "req-abc", _make_reviewed("req-abc"))
        _write_pending(pending_dir, "req-abc", _make_pending("req-abc", dry_run=False))
        with (
            patch("prometheus.agents.lumen_calendar_executor.REVIEWED_LUMEN_DIR", reviewed_dir),
            patch("prometheus.agents.lumen_calendar_executor.PENDING_LUMEN_DIR", pending_dir),
            patch("prometheus.agents.lumen_calendar_executor.APPROVED_LUMEN_DIR", approved_dir),
            patch("prometheus.agents.lumen_calendar_executor.COMPLETED_LUMEN_DIR", tmp_path / "completed"),
            patch("prometheus.agents.lumen_calendar_executor.FAILED_LUMEN_DIR", tmp_path / "failed"),
        ):
            from prometheus.agents import lumen_calendar_executor as ex
            result = ex.approve_calendar_request("req-abc")
        assert not result["ok"]
        assert "dry_run" in result["reason"]

    def test_approve_writes_approval_record(self, tmp_path):
        reviewed_dir = tmp_path / "reviewed"
        pending_dir = tmp_path / "pending"
        approved_dir = tmp_path / "approved"
        _write_reviewed(reviewed_dir, "req-abc", _make_reviewed("req-abc"))
        _write_pending(pending_dir, "req-abc", _make_pending("req-abc"))
        with (
            patch("prometheus.agents.lumen_calendar_executor.REVIEWED_LUMEN_DIR", reviewed_dir),
            patch("prometheus.agents.lumen_calendar_executor.PENDING_LUMEN_DIR", pending_dir),
            patch("prometheus.agents.lumen_calendar_executor.APPROVED_LUMEN_DIR", approved_dir),
            patch("prometheus.agents.lumen_calendar_executor.COMPLETED_LUMEN_DIR", tmp_path / "completed"),
            patch("prometheus.agents.lumen_calendar_executor.FAILED_LUMEN_DIR", tmp_path / "failed"),
        ):
            from prometheus.agents import lumen_calendar_executor as ex
            result = ex.approve_calendar_request("req-abc")
        assert result["ok"]
        assert result["approved"]
        approval_file = approved_dir / "approved_req-abc.json"
        assert approval_file.exists()
        approval = json.loads(approval_file.read_text())
        assert approval["approved"] is True
        assert approval["explicit_user_approval_required"] is True

    def test_approve_does_not_execute_google_api(self, tmp_path):
        reviewed_dir = tmp_path / "reviewed"
        pending_dir = tmp_path / "pending"
        approved_dir = tmp_path / "approved"
        _write_reviewed(reviewed_dir, "req-abc", _make_reviewed("req-abc"))
        _write_pending(pending_dir, "req-abc", _make_pending("req-abc"))
        with (
            patch("prometheus.agents.lumen_calendar_executor.REVIEWED_LUMEN_DIR", reviewed_dir),
            patch("prometheus.agents.lumen_calendar_executor.PENDING_LUMEN_DIR", pending_dir),
            patch("prometheus.agents.lumen_calendar_executor.APPROVED_LUMEN_DIR", approved_dir),
            patch("prometheus.agents.lumen_calendar_executor.COMPLETED_LUMEN_DIR", tmp_path / "completed"),
            patch("prometheus.agents.lumen_calendar_executor.FAILED_LUMEN_DIR", tmp_path / "failed"),
            patch("prometheus.agents.lumen_calendar_executor.create_calendar_event") as mock_create,
        ):
            from prometheus.agents import lumen_calendar_executor as ex
            ex.approve_calendar_request("req-abc")
        mock_create.assert_not_called()


# ── execute_approved_calendar_request ────────────────────────────────────────

class TestExecuteApproved:
    def _patch_dirs(self, tmp_path):
        return {
            "REVIEWED_LUMEN_DIR": tmp_path / "reviewed",
            "PENDING_LUMEN_DIR": tmp_path / "pending",
            "APPROVED_LUMEN_DIR": tmp_path / "approved",
            "COMPLETED_LUMEN_DIR": tmp_path / "completed",
            "FAILED_LUMEN_DIR": tmp_path / "failed",
        }

    def test_fails_if_approval_missing(self, tmp_path):
        reviewed_dir = tmp_path / "reviewed"
        _write_reviewed(reviewed_dir, "req-abc", _make_reviewed("req-abc"))
        with patch.multiple("prometheus.agents.lumen_calendar_executor", **self._patch_dirs(tmp_path)):
            # re-write dirs after patch.multiple resolves
            (tmp_path / "reviewed").mkdir(parents=True, exist_ok=True)
            _write_reviewed(tmp_path / "reviewed", "req-abc", _make_reviewed("req-abc"))
            from prometheus.agents import lumen_calendar_executor as ex
            result = ex.execute_approved_calendar_request("req-abc")
        assert not result["success"]
        assert "approval" in result["reason"].lower()

    def test_fails_if_calendar_enabled_false(self, tmp_path):
        reviewed_dir = tmp_path / "reviewed"
        pending_dir = tmp_path / "pending"
        approved_dir = tmp_path / "approved"
        _write_reviewed(reviewed_dir, "req-abc", _make_reviewed("req-abc"))
        _write_pending(pending_dir, "req-abc", _make_pending("req-abc"))
        approval = {"request_id": "req-abc", "approved": True, "approved_by": "user",
                    "approved_at": "2026-05-14T...", "operation_count": 1,
                    "explicit_user_approval_required": True}
        approved_dir.mkdir(parents=True)
        (approved_dir / "approved_req-abc.json").write_text(json.dumps(approval))

        mock_config = MagicMock()
        mock_config.enabled = False
        mock_config.dry_run = True

        with (
            patch("prometheus.agents.lumen_calendar_executor.REVIEWED_LUMEN_DIR", reviewed_dir),
            patch("prometheus.agents.lumen_calendar_executor.PENDING_LUMEN_DIR", pending_dir),
            patch("prometheus.agents.lumen_calendar_executor.APPROVED_LUMEN_DIR", approved_dir),
            patch("prometheus.agents.lumen_calendar_executor.COMPLETED_LUMEN_DIR", tmp_path / "completed"),
            patch("prometheus.agents.lumen_calendar_executor.FAILED_LUMEN_DIR", tmp_path / "failed"),
            patch("prometheus.agents.lumen_calendar_executor.load_google_calendar_config", return_value=mock_config),
        ):
            from prometheus.agents import lumen_calendar_executor as ex
            result = ex.execute_approved_calendar_request("req-abc")
        assert not result["success"]
        assert "GOOGLE_CALENDAR_ENABLED" in result["reason"]

    def test_fails_if_dry_run_true(self, tmp_path):
        reviewed_dir = tmp_path / "reviewed"
        pending_dir = tmp_path / "pending"
        approved_dir = tmp_path / "approved"
        _write_reviewed(reviewed_dir, "req-abc", _make_reviewed("req-abc"))
        _write_pending(pending_dir, "req-abc", _make_pending("req-abc"))
        approval = {"request_id": "req-abc", "approved": True, "approved_by": "user",
                    "approved_at": "2026-05-14T...", "operation_count": 1,
                    "explicit_user_approval_required": True}
        approved_dir.mkdir(parents=True)
        (approved_dir / "approved_req-abc.json").write_text(json.dumps(approval))

        mock_config = MagicMock()
        mock_config.enabled = True
        mock_config.dry_run = True
        mock_config.default_calendar_id = "primary"

        with (
            patch("prometheus.agents.lumen_calendar_executor.REVIEWED_LUMEN_DIR", reviewed_dir),
            patch("prometheus.agents.lumen_calendar_executor.PENDING_LUMEN_DIR", pending_dir),
            patch("prometheus.agents.lumen_calendar_executor.APPROVED_LUMEN_DIR", approved_dir),
            patch("prometheus.agents.lumen_calendar_executor.COMPLETED_LUMEN_DIR", tmp_path / "completed"),
            patch("prometheus.agents.lumen_calendar_executor.FAILED_LUMEN_DIR", tmp_path / "failed"),
            patch("prometheus.agents.lumen_calendar_executor.load_google_calendar_config", return_value=mock_config),
        ):
            from prometheus.agents import lumen_calendar_executor as ex
            result = ex.execute_approved_calendar_request("req-abc")
        assert not result["success"]
        assert "GOOGLE_CALENDAR_DRY_RUN" in result["reason"]

    def test_validates_all_ops_before_executing(self, tmp_path):
        """If validation fails, no operations should execute."""
        reviewed_dir = tmp_path / "reviewed"
        pending_dir = tmp_path / "pending"
        approved_dir = tmp_path / "approved"

        # Create a pending request with a create_event missing title
        bad_op = {
            "operation_id": "op-bad",
            "operation_type": "create_event",
            "calendar_id": "primary",
            "requires_prometheus_approval": True,
            "dry_run": True,
            # Missing title/start_time/end_time
        }
        bad_pending = {
            "request_id": "req-bad",
            "source": "lumen",
            "reason": "Test",
            "operation_count": 1,
            "operations": [bad_op],
        }
        reviewed = _make_reviewed("req-bad")
        _write_reviewed(reviewed_dir, "req-bad", reviewed)
        _write_pending(pending_dir, "req-bad", bad_pending)
        approval = {"request_id": "req-bad", "approved": True, "approved_by": "user",
                    "approved_at": "2026-05-14T...", "operation_count": 1,
                    "explicit_user_approval_required": True}
        approved_dir.mkdir(parents=True)
        (approved_dir / "approved_req-bad.json").write_text(json.dumps(approval))

        mock_config = MagicMock()
        mock_config.enabled = True
        mock_config.dry_run = False
        mock_config.default_calendar_id = "primary"
        mock_config.timezone = "America/New_York"

        with (
            patch("prometheus.agents.lumen_calendar_executor.REVIEWED_LUMEN_DIR", reviewed_dir),
            patch("prometheus.agents.lumen_calendar_executor.PENDING_LUMEN_DIR", pending_dir),
            patch("prometheus.agents.lumen_calendar_executor.APPROVED_LUMEN_DIR", approved_dir),
            patch("prometheus.agents.lumen_calendar_executor.COMPLETED_LUMEN_DIR", tmp_path / "completed"),
            patch("prometheus.agents.lumen_calendar_executor.FAILED_LUMEN_DIR", tmp_path / "failed"),
            patch("prometheus.agents.lumen_calendar_executor.load_google_calendar_config", return_value=mock_config),
            patch("prometheus.agents.lumen_calendar_executor.create_calendar_event") as mock_create,
        ):
            from prometheus.agents import lumen_calendar_executor as ex
            result = ex.execute_approved_calendar_request("req-bad")
        assert not result["success"]
        mock_create.assert_not_called()

    def test_execute_create_event_calls_adapter(self, tmp_path):
        reviewed_dir = tmp_path / "reviewed"
        pending_dir = tmp_path / "pending"
        approved_dir = tmp_path / "approved"
        _write_reviewed(reviewed_dir, "req-create", _make_reviewed("req-create"))
        _write_pending(pending_dir, "req-create", _make_pending("req-create"))
        approval = {"request_id": "req-create", "approved": True, "approved_by": "user",
                    "approved_at": "2026-05-14T...", "operation_count": 1,
                    "explicit_user_approval_required": True}
        approved_dir.mkdir(parents=True)
        (approved_dir / "approved_req-create.json").write_text(json.dumps(approval))

        mock_config = MagicMock()
        mock_config.enabled = True
        mock_config.dry_run = False
        mock_config.default_calendar_id = "primary"
        mock_config.timezone = "America/New_York"

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.dry_run = False
        mock_result.message = "Event created."
        mock_result.event_id = "new-event-id"
        mock_result.calendar_id = "primary"

        with (
            patch("prometheus.agents.lumen_calendar_executor.REVIEWED_LUMEN_DIR", reviewed_dir),
            patch("prometheus.agents.lumen_calendar_executor.PENDING_LUMEN_DIR", pending_dir),
            patch("prometheus.agents.lumen_calendar_executor.APPROVED_LUMEN_DIR", approved_dir),
            patch("prometheus.agents.lumen_calendar_executor.COMPLETED_LUMEN_DIR", tmp_path / "completed"),
            patch("prometheus.agents.lumen_calendar_executor.FAILED_LUMEN_DIR", tmp_path / "failed"),
            patch("prometheus.agents.lumen_calendar_executor.load_google_calendar_config", return_value=mock_config),
            patch("prometheus.agents.lumen_calendar_executor.build_google_calendar_service", return_value=MagicMock()),
            patch("prometheus.agents.lumen_calendar_executor.create_calendar_event", return_value=mock_result) as mock_create,
        ):
            from prometheus.agents import lumen_calendar_executor as ex
            result = ex.execute_approved_calendar_request("req-create")
        assert result["success"]
        mock_create.assert_called_once()

    def test_execute_writes_completed_result(self, tmp_path):
        reviewed_dir = tmp_path / "reviewed"
        pending_dir = tmp_path / "pending"
        approved_dir = tmp_path / "approved"
        completed_dir = tmp_path / "completed"
        _write_reviewed(reviewed_dir, "req-done", _make_reviewed("req-done"))
        _write_pending(pending_dir, "req-done", _make_pending("req-done"))
        approval = {"request_id": "req-done", "approved": True, "approved_by": "user",
                    "approved_at": "2026-05-14T...", "operation_count": 1,
                    "explicit_user_approval_required": True}
        approved_dir.mkdir(parents=True)
        (approved_dir / "approved_req-done.json").write_text(json.dumps(approval))

        mock_config = MagicMock()
        mock_config.enabled = True
        mock_config.dry_run = False
        mock_config.default_calendar_id = "primary"
        mock_config.timezone = "America/New_York"

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.dry_run = False
        mock_result.message = "Event created."
        mock_result.event_id = "evt-xyz"
        mock_result.calendar_id = "primary"

        with (
            patch("prometheus.agents.lumen_calendar_executor.REVIEWED_LUMEN_DIR", reviewed_dir),
            patch("prometheus.agents.lumen_calendar_executor.PENDING_LUMEN_DIR", pending_dir),
            patch("prometheus.agents.lumen_calendar_executor.APPROVED_LUMEN_DIR", approved_dir),
            patch("prometheus.agents.lumen_calendar_executor.COMPLETED_LUMEN_DIR", completed_dir),
            patch("prometheus.agents.lumen_calendar_executor.FAILED_LUMEN_DIR", tmp_path / "failed"),
            patch("prometheus.agents.lumen_calendar_executor.load_google_calendar_config", return_value=mock_config),
            patch("prometheus.agents.lumen_calendar_executor.build_google_calendar_service", return_value=MagicMock()),
            patch("prometheus.agents.lumen_calendar_executor.create_calendar_event", return_value=mock_result),
        ):
            from prometheus.agents import lumen_calendar_executor as ex
            result = ex.execute_approved_calendar_request("req-done")
        assert result["success"]
        completed_files = list(completed_dir.glob("completed_*.json"))
        assert len(completed_files) == 1

    def test_execute_writes_failed_result_on_failure(self, tmp_path):
        reviewed_dir = tmp_path / "reviewed"
        pending_dir = tmp_path / "pending"
        approved_dir = tmp_path / "approved"
        completed_dir = tmp_path / "completed"
        failed_dir = tmp_path / "failed"
        _write_reviewed(reviewed_dir, "req-fail", _make_reviewed("req-fail"))
        _write_pending(pending_dir, "req-fail", _make_pending("req-fail"))
        approval = {"request_id": "req-fail", "approved": True, "approved_by": "user",
                    "approved_at": "2026-05-14T...", "operation_count": 1,
                    "explicit_user_approval_required": True}
        approved_dir.mkdir(parents=True)
        (approved_dir / "approved_req-fail.json").write_text(json.dumps(approval))

        mock_config = MagicMock()
        mock_config.enabled = True
        mock_config.dry_run = False
        mock_config.default_calendar_id = "primary"
        mock_config.timezone = "America/New_York"

        mock_result = MagicMock()
        mock_result.success = False
        mock_result.dry_run = False
        mock_result.message = "API error."
        mock_result.event_id = None
        mock_result.calendar_id = "primary"

        with (
            patch("prometheus.agents.lumen_calendar_executor.REVIEWED_LUMEN_DIR", reviewed_dir),
            patch("prometheus.agents.lumen_calendar_executor.PENDING_LUMEN_DIR", pending_dir),
            patch("prometheus.agents.lumen_calendar_executor.APPROVED_LUMEN_DIR", approved_dir),
            patch("prometheus.agents.lumen_calendar_executor.COMPLETED_LUMEN_DIR", completed_dir),
            patch("prometheus.agents.lumen_calendar_executor.FAILED_LUMEN_DIR", failed_dir),
            patch("prometheus.agents.lumen_calendar_executor.load_google_calendar_config", return_value=mock_config),
            patch("prometheus.agents.lumen_calendar_executor.build_google_calendar_service", return_value=MagicMock()),
            patch("prometheus.agents.lumen_calendar_executor.create_calendar_event", return_value=mock_result),
        ):
            from prometheus.agents import lumen_calendar_executor as ex
            result = ex.execute_approved_calendar_request("req-fail")
        assert not result["success"]
        failed_files = list(failed_dir.glob("failed_*.json"))
        assert len(failed_files) == 1


# ── execute_calendar_operation ────────────────────────────────────────────────

class TestExecuteCalendarOperation:
    def _mock_config(self) -> MagicMock:
        cfg = MagicMock()
        cfg.enabled = True
        cfg.dry_run = False
        cfg.default_calendar_id = "primary"
        cfg.timezone = "America/New_York"
        return cfg

    def test_create_event_calls_create(self):
        from prometheus.agents.lumen_calendar_executor import execute_calendar_operation
        op = {
            "operation_type": "create_event",
            "calendar_id": "primary",
            "title": "Test event",
            "start_time": "2026-05-15T14:00",
            "end_time": "2026-05-15T15:00",
        }
        mock_result = MagicMock()
        mock_result.success = True
        with patch("prometheus.agents.lumen_calendar_executor.create_calendar_event", return_value=mock_result) as mock_fn:
            result = execute_calendar_operation(op, self._mock_config(), MagicMock())
        mock_fn.assert_called_once()
        assert result.success

    def test_update_event_calls_update(self):
        from prometheus.agents.lumen_calendar_executor import execute_calendar_operation
        op = {"operation_type": "update_event", "calendar_id": "primary", "event_id": "evt-123", "title": "New title"}
        mock_result = MagicMock()
        mock_result.success = True
        with patch("prometheus.agents.lumen_calendar_executor.update_calendar_event", return_value=mock_result) as mock_fn:
            result = execute_calendar_operation(op, self._mock_config(), MagicMock())
        mock_fn.assert_called_once()

    def test_delete_event_calls_delete(self):
        from prometheus.agents.lumen_calendar_executor import execute_calendar_operation
        op = {"operation_type": "delete_event", "calendar_id": "primary", "event_id": "evt-456"}
        mock_result = MagicMock()
        mock_result.success = True
        with patch("prometheus.agents.lumen_calendar_executor.delete_calendar_event", return_value=mock_result) as mock_fn:
            result = execute_calendar_operation(op, self._mock_config(), MagicMock())
        mock_fn.assert_called_once()

    def test_read_events_returns_skipped(self):
        from prometheus.agents.lumen_calendar_executor import execute_calendar_operation
        op = {"operation_type": "read_events", "calendar_id": "primary"}
        result = execute_calendar_operation(op, self._mock_config(), MagicMock())
        assert result.success
        assert "skipped" in result.message.lower() or "not a write" in result.message.lower()

    def test_find_availability_returns_skipped(self):
        from prometheus.agents.lumen_calendar_executor import execute_calendar_operation
        op = {"operation_type": "find_availability", "calendar_id": "primary"}
        result = execute_calendar_operation(op, self._mock_config(), MagicMock())
        assert result.success


# ── Safety checks ─────────────────────────────────────────────────────────────

class TestSafety:
    def test_no_subprocess_in_executor(self):
        src = (ROOT / "prometheus" / "agents" / "lumen_calendar_executor.py").read_text()
        assert "import subprocess" not in src
        assert "os.system(" not in src
        assert "shell=True" not in src

    def test_no_home_assistant_calls(self):
        src = (ROOT / "prometheus" / "agents" / "lumen_calendar_executor.py").read_text()
        # "home_assistant" and "ha_service" are allowed only in _SUSPICIOUS_KEYS (as blocked keys)
        # but must never appear as actual HA integration calls
        assert "run_ha_script" not in src
        assert "home_assistant_url" not in src.lower()
        assert "requests.get" not in src
        # Allowed only as entries in the suspicious-key blocklist — not as live HA calls
        lines_with_ha = [
            ln for ln in src.splitlines()
            if "home_assistant" in ln.lower() and "_SUSPICIOUS_KEYS" not in ln and "ha_service" not in ln
        ]
        assert not lines_with_ha, f"Unexpected home_assistant references: {lines_with_ha}"

    def test_no_execute_all_command(self):
        src = (ROOT / "prometheus" / "agents" / "lumen_calendar_executor.py").read_text()
        assert "--execute-all" not in src
        assert "execute_all" not in src

    def test_execute_requires_approval_record(self):
        """Cannot execute without a prior approval record — enforced in code."""
        src = (ROOT / "prometheus" / "agents" / "lumen_calendar_executor.py").read_text()
        assert "_load_approval_record" in src
        assert "approval record" in src.lower() or "approval" in src.lower()

    def test_execute_checks_dry_run_env(self):
        src = (ROOT / "prometheus" / "agents" / "lumen_calendar_executor.py").read_text()
        assert "GOOGLE_CALENDAR_DRY_RUN" in src

    def test_execute_checks_enabled_env(self):
        src = (ROOT / "prometheus" / "agents" / "lumen_calendar_executor.py").read_text()
        assert "GOOGLE_CALENDAR_ENABLED" in src

    def test_lumen_source_files_not_imported_or_modified(self):
        src = (ROOT / "prometheus" / "agents" / "lumen_calendar_executor.py").read_text()
        assert "/Lumen/" not in src
        assert "lumen_outbox" not in src.lower()

    def test_suspicious_keys_blocked(self):
        from prometheus.agents.lumen_calendar_executor import _validate_operations
        bad_op = {
            "operation_type": "create_event",
            "title": "Evil event",
            "start_time": "2026-05-15T14:00",
            "end_time": "2026-05-15T15:00",
            "command": "rm -rf /",  # suspicious key
        }
        ok, msg = _validate_operations([bad_op])
        assert not ok
        assert "suspicious" in msg.lower()

    def test_confirmed_required_in_tool_dispatch(self):
        """ToolRegistry dispatch for calendar_execute_approved_request checks confirmed=true."""
        src = (ROOT / "tools.py").read_text()
        assert "confirmed" in src

    def test_high_risk_classification(self):
        from prometheus.execution.tool_capability_registry import TOOL_CAPABILITIES
        assert TOOL_CAPABILITIES["calendar_execute_approved_request"].risk == "high"

    def test_execute_requires_confirmed_in_registry(self):
        from prometheus.execution.tool_capability_registry import TOOL_CAPABILITIES
        cap = TOOL_CAPABILITIES["calendar_execute_approved_request"]
        assert "confirmed" in cap.required_slots

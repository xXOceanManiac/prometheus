"""
test_lumen_ingestion.py — Tests for Prometheus Lumen outbox ingestion.
"""
from __future__ import annotations

import dataclasses
import inspect
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from prometheus.agents.lumen_ingestion import (
    LumenIngestionResult,
    PendingCalendarProposal,
    validate_lumen_calendar_request,
    ingest_lumen_outbox_once,
    list_pending_lumen_calendar_proposals,
)
from prometheus.infra.paths import (
    PROMETHEUS_ECOSYSTEM_ROOT,
    LUMEN_ROOT,
    LUMEN_OUTBOX_DIR,
    LUMEN_ACCEPTED_DIR,
    LUMEN_REJECTED_DIR,
    PENDING_LUMEN_DIR,
    ensure_lumen_ingestion_dirs,
)


# ── Path constant tests ───────────────────────────────────────────────────────

class TestLumenPaths:
    def test_ecosystem_root_resolves(self):
        assert PROMETHEUS_ECOSYSTEM_ROOT.name == "PROMETHEUS"

    def test_lumen_root_is_sibling(self):
        assert LUMEN_ROOT.name == "Lumen"
        assert LUMEN_ROOT.parent == PROMETHEUS_ECOSYSTEM_ROOT

    def test_lumen_outbox_under_lumen_runtime(self):
        assert LUMEN_OUTBOX_DIR.parent.name == "outbox" or LUMEN_OUTBOX_DIR.name == "outbox"

    def test_pending_lumen_dir_under_prometheus_runtime(self):
        # PENDING_LUMEN_DIR should be inside Prometheus runtime
        from prometheus.infra.paths import RUNTIME_ROOT
        assert RUNTIME_ROOT in PENDING_LUMEN_DIR.parents

    def test_ensure_lumen_ingestion_dirs_creates_dirs(self, tmp_path, monkeypatch):
        import prometheus.infra.paths as pmod
        monkeypatch.setattr(pmod, "LUMEN_ACCEPTED_DIR", tmp_path / "accepted")
        monkeypatch.setattr(pmod, "LUMEN_REJECTED_DIR", tmp_path / "rejected")
        monkeypatch.setattr(pmod, "LUMEN_ARCHIVE_DIR", tmp_path / "archive")
        monkeypatch.setattr(pmod, "PENDING_LUMEN_DIR", tmp_path / "pending")
        # Re-import to pick up monkeypatched values
        import prometheus.agents.lumen_ingestion as lmod
        monkeypatch.setattr(lmod, "LUMEN_ACCEPTED_DIR", tmp_path / "accepted")
        monkeypatch.setattr(lmod, "LUMEN_REJECTED_DIR", tmp_path / "rejected")
        monkeypatch.setattr(lmod, "PENDING_LUMEN_DIR", tmp_path / "pending")
        ensure_lumen_ingestion_dirs()
        assert (tmp_path / "accepted").exists()
        assert (tmp_path / "rejected").exists()


# ── Validation tests ──────────────────────────────────────────────────────────

def _good_request(overrides: dict | None = None) -> dict:
    base = {
        "request_id": "req-abc123",
        "source": "lumen",
        "reason": "Test request",
        "requires_prometheus_approval": True,
        "created_at": "2026-05-14T04:00:00+00:00",
        "operations": [
            {
                "operation_id": "op-001",
                "operation_type": "create_event",
                "requires_prometheus_approval": True,
                "dry_run": True,
                "calendar_id": "primary",
                "title": "Focus block",
                "start_time": "2026-05-15T14:00",
                "end_time": "2026-05-15T15:30",
                "reason": "Test",
                "created_at": "2026-05-14T04:00:00+00:00",
            }
        ],
    }
    if overrides:
        base.update(overrides)
    return base


class TestValidateLumenCalendarRequest:
    def test_valid_request_passes(self):
        ok, reason = validate_lumen_calendar_request(_good_request())
        assert ok
        assert reason == "OK"

    def test_not_a_dict_fails(self):
        ok, _ = validate_lumen_calendar_request("bad")
        assert not ok

    def test_wrong_source_fails(self):
        ok, _ = validate_lumen_calendar_request(_good_request({"source": "prometheus"}))
        assert not ok

    def test_missing_request_id_fails(self):
        req = _good_request()
        del req["request_id"]
        ok, _ = validate_lumen_calendar_request(req)
        assert not ok

    def test_empty_request_id_fails(self):
        ok, _ = validate_lumen_calendar_request(_good_request({"request_id": ""}))
        assert not ok

    def test_approval_false_fails(self):
        ok, _ = validate_lumen_calendar_request(_good_request({"requires_prometheus_approval": False}))
        assert not ok

    def test_empty_operations_fails(self):
        ok, _ = validate_lumen_calendar_request(_good_request({"operations": []}))
        assert not ok

    def test_operation_dry_run_false_fails(self):
        req = _good_request()
        req["operations"][0]["dry_run"] = False
        ok, reason = validate_lumen_calendar_request(req)
        assert not ok
        assert "dry_run" in reason

    def test_operation_approval_false_fails(self):
        req = _good_request()
        req["operations"][0]["requires_prometheus_approval"] = False
        ok, reason = validate_lumen_calendar_request(req)
        assert not ok

    def test_unsupported_operation_type_fails(self):
        req = _good_request()
        req["operations"][0]["operation_type"] = "send_email"
        ok, reason = validate_lumen_calendar_request(req)
        assert not ok
        assert "operation_type" in reason

    def test_suspicious_key_command_fails(self):
        req = _good_request()
        req["operations"][0]["command"] = "rm -rf /"
        ok, reason = validate_lumen_calendar_request(req)
        assert not ok
        assert "command" in reason

    def test_suspicious_key_token_fails(self):
        req = _good_request()
        req["operations"][0]["token"] = "secret"
        ok, reason = validate_lumen_calendar_request(req)
        assert not ok
        assert "token" in reason

    def test_all_valid_operation_types_accepted(self):
        for op_type in ["create_event", "update_event", "delete_event",
                         "read_events", "find_availability", "suggest_schedule_change"]:
            req = _good_request()
            req["operations"][0]["operation_type"] = op_type
            ok, _ = validate_lumen_calendar_request(req)
            assert ok, f"Expected {op_type} to pass"


# ── Ingestion tests ───────────────────────────────────────────────────────────

def _write_request(outbox_dir: Path, name: str, payload: dict) -> Path:
    p = outbox_dir / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


class TestIngestLumenOutboxOnce:
    def test_missing_outbox_returns_empty(self, tmp_path, monkeypatch):
        import prometheus.agents.lumen_ingestion as lmod
        monkeypatch.setattr(lmod, "LUMEN_OUTBOX_DIR", tmp_path / "nonexistent")
        monkeypatch.setattr(lmod, "LUMEN_ACCEPTED_DIR", tmp_path / "accepted")
        monkeypatch.setattr(lmod, "LUMEN_REJECTED_DIR", tmp_path / "rejected")
        monkeypatch.setattr(lmod, "PENDING_LUMEN_DIR", tmp_path / "pending")
        results = ingest_lumen_outbox_once()
        assert results == []

    def test_valid_request_is_accepted(self, tmp_path, monkeypatch):
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        accepted = tmp_path / "accepted"
        rejected = tmp_path / "rejected"
        pending = tmp_path / "pending"

        import prometheus.agents.lumen_ingestion as lmod
        monkeypatch.setattr(lmod, "LUMEN_OUTBOX_DIR", outbox)
        monkeypatch.setattr(lmod, "LUMEN_ACCEPTED_DIR", accepted)
        monkeypatch.setattr(lmod, "LUMEN_REJECTED_DIR", rejected)
        monkeypatch.setattr(lmod, "PENDING_LUMEN_DIR", pending)

        _write_request(outbox, "lumen_calendar_request_001.json", _good_request())
        results = ingest_lumen_outbox_once()
        assert len(results) == 1
        assert results[0].status == "accepted"

    def test_accepted_creates_pending_proposal(self, tmp_path, monkeypatch):
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        import prometheus.agents.lumen_ingestion as lmod
        monkeypatch.setattr(lmod, "LUMEN_OUTBOX_DIR", outbox)
        monkeypatch.setattr(lmod, "LUMEN_ACCEPTED_DIR", tmp_path / "accepted")
        monkeypatch.setattr(lmod, "LUMEN_REJECTED_DIR", tmp_path / "rejected")
        monkeypatch.setattr(lmod, "PENDING_LUMEN_DIR", tmp_path / "pending")

        req = _good_request()
        _write_request(outbox, "lumen_calendar_request_002.json", req)
        results = ingest_lumen_outbox_once()
        assert results[0].status == "accepted"
        proposal_path = Path(results[0].destination_path)
        assert proposal_path.exists()
        data = json.loads(proposal_path.read_text())
        assert data["request_id"] == req["request_id"]
        assert data["source"] == "lumen"

    def test_accepted_original_moves_to_accepted_dir(self, tmp_path, monkeypatch):
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        accepted = tmp_path / "accepted"
        import prometheus.agents.lumen_ingestion as lmod
        monkeypatch.setattr(lmod, "LUMEN_OUTBOX_DIR", outbox)
        monkeypatch.setattr(lmod, "LUMEN_ACCEPTED_DIR", accepted)
        monkeypatch.setattr(lmod, "LUMEN_REJECTED_DIR", tmp_path / "rejected")
        monkeypatch.setattr(lmod, "PENDING_LUMEN_DIR", tmp_path / "pending")

        _write_request(outbox, "lumen_calendar_request_003.json", _good_request())
        ingest_lumen_outbox_once()
        # original no longer in outbox
        assert not (outbox / "lumen_calendar_request_003.json").exists()
        # moved to accepted
        assert (accepted / "lumen_calendar_request_003.json").exists()

    def test_malformed_json_is_rejected(self, tmp_path, monkeypatch):
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        rejected = tmp_path / "rejected"
        import prometheus.agents.lumen_ingestion as lmod
        monkeypatch.setattr(lmod, "LUMEN_OUTBOX_DIR", outbox)
        monkeypatch.setattr(lmod, "LUMEN_ACCEPTED_DIR", tmp_path / "accepted")
        monkeypatch.setattr(lmod, "LUMEN_REJECTED_DIR", rejected)
        monkeypatch.setattr(lmod, "PENDING_LUMEN_DIR", tmp_path / "pending")

        bad = outbox / "lumen_calendar_request_bad.json"
        bad.write_text("{not valid json", encoding="utf-8")
        results = ingest_lumen_outbox_once()
        assert results[0].status == "rejected"
        assert (rejected / "lumen_calendar_request_bad.json").exists()

    def test_missing_approval_is_rejected(self, tmp_path, monkeypatch):
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        import prometheus.agents.lumen_ingestion as lmod
        monkeypatch.setattr(lmod, "LUMEN_OUTBOX_DIR", outbox)
        monkeypatch.setattr(lmod, "LUMEN_ACCEPTED_DIR", tmp_path / "accepted")
        monkeypatch.setattr(lmod, "LUMEN_REJECTED_DIR", tmp_path / "rejected")
        monkeypatch.setattr(lmod, "PENDING_LUMEN_DIR", tmp_path / "pending")

        req = _good_request({"requires_prometheus_approval": False})
        _write_request(outbox, "lumen_calendar_request_noapproval.json", req)
        results = ingest_lumen_outbox_once()
        assert results[0].status == "rejected"

    def test_dry_run_false_is_rejected(self, tmp_path, monkeypatch):
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        import prometheus.agents.lumen_ingestion as lmod
        monkeypatch.setattr(lmod, "LUMEN_OUTBOX_DIR", outbox)
        monkeypatch.setattr(lmod, "LUMEN_ACCEPTED_DIR", tmp_path / "accepted")
        monkeypatch.setattr(lmod, "LUMEN_REJECTED_DIR", tmp_path / "rejected")
        monkeypatch.setattr(lmod, "PENDING_LUMEN_DIR", tmp_path / "pending")

        req = _good_request()
        req["operations"][0]["dry_run"] = False
        _write_request(outbox, "lumen_calendar_request_nodryrun.json", req)
        results = ingest_lumen_outbox_once()
        assert results[0].status == "rejected"

    def test_operation_approval_false_rejected(self, tmp_path, monkeypatch):
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        import prometheus.agents.lumen_ingestion as lmod
        monkeypatch.setattr(lmod, "LUMEN_OUTBOX_DIR", outbox)
        monkeypatch.setattr(lmod, "LUMEN_ACCEPTED_DIR", tmp_path / "accepted")
        monkeypatch.setattr(lmod, "LUMEN_REJECTED_DIR", tmp_path / "rejected")
        monkeypatch.setattr(lmod, "PENDING_LUMEN_DIR", tmp_path / "pending")

        req = _good_request()
        req["operations"][0]["requires_prometheus_approval"] = False
        _write_request(outbox, "lumen_calendar_request_opnoapproval.json", req)
        results = ingest_lumen_outbox_once()
        assert results[0].status == "rejected"


# ── list_pending tests ────────────────────────────────────────────────────────

class TestListPendingProposals:
    def test_list_pending_returns_proposals(self, tmp_path, monkeypatch):
        pending = tmp_path / "pending"
        pending.mkdir()
        import prometheus.agents.lumen_ingestion as lmod
        monkeypatch.setattr(lmod, "PENDING_LUMEN_DIR", pending)

        proposal = PendingCalendarProposal(
            request_id="req-test001",
            source="lumen",
            reason="test",
            operation_count=1,
            operations=[],
            created_at="2026-05-14T00:00:00Z",
            ingested_at="2026-05-14T00:01:00Z",
            source_path="/tmp/fake.json",
        )
        (pending / "pending_req-test001.json").write_text(
            json.dumps(dataclasses.asdict(proposal)), encoding="utf-8"
        )
        results = list_pending_lumen_calendar_proposals()
        assert len(results) == 1
        assert results[0].request_id == "req-test001"

    def test_empty_pending_dir_returns_empty(self, tmp_path, monkeypatch):
        pending = tmp_path / "pending"
        pending.mkdir()
        import prometheus.agents.lumen_ingestion as lmod
        monkeypatch.setattr(lmod, "PENDING_LUMEN_DIR", pending)
        results = list_pending_lumen_calendar_proposals()
        assert results == []

    def test_missing_pending_dir_returns_empty(self, tmp_path, monkeypatch):
        import prometheus.agents.lumen_ingestion as lmod
        monkeypatch.setattr(lmod, "PENDING_LUMEN_DIR", tmp_path / "nonexistent")
        results = list_pending_lumen_calendar_proposals()
        assert results == []


# ── Safety guard tests ────────────────────────────────────────────────────────

class TestNoForbiddenDependencies:
    def _source(self, module_path: str) -> str:
        p = Path(ROOT) / module_path
        return p.read_text(encoding="utf-8")

    def test_no_subprocess_in_lumen_ingestion(self):
        src = self._source("prometheus/agents/lumen_ingestion.py")
        assert "subprocess" not in src
        assert "os.system" not in src

    def test_no_google_calendar_api_in_lumen_ingestion(self):
        src = self._source("prometheus/agents/lumen_ingestion.py")
        assert "googleapiclient" not in src
        assert "google.oauth2" not in src
        assert "oauth2client" not in src

    def test_no_home_assistant_calls_in_lumen_ingestion(self):
        src = self._source("prometheus/agents/lumen_ingestion.py")
        assert "home_assistant" not in src.lower() or "ha_service" in src  # ha_service only in reject list
        # More specific: no HA HTTP calls
        assert "requests.post" not in src
        assert "HOME_ASSISTANT_API_KEY" not in src

    def test_no_calendar_execution_in_lumen_ingestion(self):
        src = self._source("prometheus/agents/lumen_ingestion.py")
        # No actual calendar write/execute calls
        assert "insert(" not in src
        assert "events().insert" not in src
        assert "calendar_service" not in src

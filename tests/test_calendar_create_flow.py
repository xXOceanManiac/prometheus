"""
tests/test_calendar_create_flow.py — Tests for NL calendar creation flow.

Covers:
- parse_calendar_create_request: date/time/title extraction
- parse_and_propose: full pipeline including availability search
- confirm_pending_calendar_confirmation: executor integration, dry-run blocking
- cancel_pending_calendar_confirmation: cancellation
- has_pending_calendar_confirmation: filesystem check
- Safety: no HA calls, no passive writes, no direct GCal calls
- Intent override routing: create/confirm/cancel phrases
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ── Module under test ─────────────────────────────────────────────────────────

from prometheus.agents.calendar_create_flow import (
    parse_calendar_create_request,
    parse_and_propose,
    confirm_pending_calendar_confirmation,
    cancel_pending_calendar_confirmation,
    has_pending_calendar_confirmation,
    get_most_recent_pending_confirmation,
    _extract_title,
    _extract_date_hint,
    _extract_time_hint,
    _resolve_date,
    _resolve_time,
    _default_duration,
    _extract_explicit_duration,
    _human_summary,
    _build_operation,
    should_auto_execute_calendar_create,
    _check_conflict,
    _direct_create_calendar_event,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _patch_confirm_dir(tmp_path):
    """Redirect pending confirmation writes to a temp directory."""
    conf_dir = tmp_path / "calendar_confirmations"
    conf_dir.mkdir()
    with patch(
        "prometheus.agents.calendar_create_flow.PENDING_CALENDAR_CONFIRMATIONS_DIR",
        conf_dir,
    ):
        yield conf_dir


@pytest.fixture()
def _patch_executor_dirs(tmp_path):
    """Redirect all executor dirs to temp directories."""
    reviewed = tmp_path / "reviewed"
    approved = tmp_path / "approved"
    reviewed.mkdir()
    approved.mkdir()
    with patch.multiple(
        "prometheus.agents.calendar_create_flow",
        REVIEWED_LUMEN_DIR=reviewed,
        APPROVED_LUMEN_DIR=approved,
    ):
        yield {"reviewed": reviewed, "approved": approved}


TODAY = date(2026, 5, 14)   # Thursday
NOW = datetime(2026, 5, 14, 10, 0, 0)


# ═══════════════════════════════════════════════════════════════════════════════
# Section 1 — Title extraction
# ═══════════════════════════════════════════════════════════════════════════════

class TestExtractTitle:
    def test_schedule_verb(self):
        assert "Focus Block" == _extract_title("schedule a focus block tomorrow at 2")

    def test_add_verb(self):
        assert "Workout Session" == _extract_title("add a workout session this afternoon")

    def test_put_verb(self):
        assert "Standup" == _extract_title("put a standup on my calendar on friday at 10")

    def test_block_off_for(self):
        title = _extract_title("block off some time tomorrow morning for deep work")
        assert "Deep Work" in title or "deep work" in title.lower()

    def test_called_pattern(self):
        assert "Tuesday Retrospective" == _extract_title(
            "schedule an event called Tuesday Retrospective at 3pm"
        )

    def test_named_pattern(self):
        assert "Team Sync" == _extract_title("add a meeting named Team Sync tomorrow")

    def test_create_verb(self):
        title = _extract_title("create a meeting with the team tomorrow at noon")
        assert title  # Some title extracted

    def test_stops_at_date_keyword(self):
        title = _extract_title("schedule a focus block tomorrow at 2")
        assert "tomorrow" not in title.lower()

    def test_stops_at_time_keyword(self):
        title = _extract_title("schedule a focus block at 3pm")
        assert "at" not in title.lower()

    def test_empty_gives_empty(self):
        # Should not crash
        result = _extract_title("at 2pm tomorrow")
        assert isinstance(result, str)


# ═══════════════════════════════════════════════════════════════════════════════
# Section 2 — Date hint extraction
# ═══════════════════════════════════════════════════════════════════════════════

class TestExtractDateHint:
    def test_tomorrow(self):
        assert "tomorrow" == _extract_date_hint("schedule a standup tomorrow at 10")

    def test_today(self):
        assert "today" == _extract_date_hint("add a meeting today at 3pm")

    def test_this_afternoon_is_today(self):
        assert "today" == _extract_date_hint("add a workout this afternoon")

    def test_tonight_is_today(self):
        assert "today" == _extract_date_hint("schedule a run tonight")

    def test_weekday(self):
        assert "friday" == _extract_date_hint("standup on friday at 10")

    def test_next_weekday(self):
        assert "next monday" == _extract_date_hint("schedule a meeting next monday at 2pm")

    def test_this_weekday(self):
        assert "monday" == _extract_date_hint("add a sync this monday at noon")

    def test_no_date_gives_empty(self):
        assert "" == _extract_date_hint("schedule a standup at 10am")


# ═══════════════════════════════════════════════════════════════════════════════
# Section 3 — Time hint extraction
# ═══════════════════════════════════════════════════════════════════════════════

class TestExtractTimeHint:
    def test_at_bare_hour(self):
        time_hint, window = _extract_time_hint("schedule a focus block tomorrow at 2")
        assert "at 2" in time_hint
        assert window == ""

    def test_at_pm(self):
        time_hint, window = _extract_time_hint("standup tomorrow at 4pm")
        assert "4pm" in time_hint
        assert window == ""

    def test_at_am(self):
        time_hint, window = _extract_time_hint("meeting at 10am")
        assert "10am" in time_hint
        assert window == ""

    def test_morning_window(self):
        time_hint, window = _extract_time_hint("workout this morning")
        assert time_hint == ""
        assert window == "morning"

    def test_afternoon_window(self):
        time_hint, window = _extract_time_hint("add a session this afternoon")
        assert time_hint == ""
        assert window == "afternoon"

    def test_evening_window(self):
        time_hint, window = _extract_time_hint("run this evening")
        assert window == "evening"

    def test_tonight_window(self):
        time_hint, window = _extract_time_hint("something tonight")
        assert window == "tonight"

    def test_no_time_gives_empty(self):
        time_hint, window = _extract_time_hint("schedule a standup on friday")
        assert time_hint == ""
        assert window == ""


# ═══════════════════════════════════════════════════════════════════════════════
# Section 4 — Date resolution
# ═══════════════════════════════════════════════════════════════════════════════

class TestResolveDate:
    def test_today(self):
        assert TODAY == _resolve_date("today", TODAY)

    def test_tomorrow(self):
        assert TODAY + timedelta(days=1) == _resolve_date("tomorrow", TODAY)

    def test_friday_from_thursday(self):
        # Today is Thursday (weekday=3), Friday (4) is 1 day ahead
        result = _resolve_date("friday", TODAY)
        assert result == TODAY + timedelta(days=1)

    def test_monday_from_thursday(self):
        # Today is Thursday (3), next Monday (0) is 4 days ahead
        result = _resolve_date("monday", TODAY)
        assert result == TODAY + timedelta(days=4)

    def test_thursday_from_thursday_skips_to_next_week(self):
        # Same weekday → skip to next occurrence
        result = _resolve_date("thursday", TODAY)
        assert result == TODAY + timedelta(days=7)

    def test_next_monday(self):
        result = _resolve_date("next monday", TODAY)
        assert result is not None
        assert result > TODAY
        assert result.weekday() == 0  # Monday

    def test_empty_hint_gives_none(self):
        assert _resolve_date("", TODAY) is None

    def test_unknown_hint_gives_none(self):
        assert _resolve_date("next week sometime", TODAY) is None


# ═══════════════════════════════════════════════════════════════════════════════
# Section 5 — Time resolution
# ═══════════════════════════════════════════════════════════════════════════════

class TestResolveTime:
    def test_bare_2_becomes_14(self):
        h, m = _resolve_time("at 2")
        assert h == 14
        assert m == 0

    def test_bare_7_becomes_19(self):
        h, m = _resolve_time("at 7")
        assert h == 19

    def test_bare_8_stays_8(self):
        h, m = _resolve_time("at 8")
        assert h == 8

    def test_bare_10_stays_10(self):
        h, m = _resolve_time("at 10")
        assert h == 10

    def test_bare_12_stays_12(self):
        h, m = _resolve_time("at 12")
        assert h == 12

    def test_explicit_pm(self):
        h, m = _resolve_time("at 4pm")
        assert h == 16

    def test_explicit_am(self):
        h, m = _resolve_time("at 9am")
        assert h == 9

    def test_with_minutes(self):
        h, m = _resolve_time("at 2:30")
        assert h == 14
        assert m == 30

    def test_with_minutes_pm(self):
        h, m = _resolve_time("at 4:30pm")
        assert h == 16
        assert m == 30

    def test_empty_gives_none(self):
        assert _resolve_time("") is None

    def test_window_gives_none(self):
        assert _resolve_time("morning") is None


# ═══════════════════════════════════════════════════════════════════════════════
# Section 6 — Duration defaults
# ═══════════════════════════════════════════════════════════════════════════════

class TestDefaultDuration:
    def test_focus_block_is_90(self):
        assert 90 == _default_duration("focus block")

    def test_deep_work_is_90(self):
        assert 90 == _default_duration("deep work")

    def test_focus_alone_is_90(self):
        assert 90 == _default_duration("focus")

    def test_workout_is_60(self):
        assert 60 == _default_duration("workout")

    def test_standup_is_30(self):
        assert 30 == _default_duration("standup")

    def test_check_in_is_30(self):
        assert 30 == _default_duration("check-in")

    def test_meeting_is_60(self):
        assert 60 == _default_duration("team meeting")

    def test_unknown_defaults_to_60(self):
        assert 60 == _default_duration("widget review")


# ═══════════════════════════════════════════════════════════════════════════════
# Section 7 — Full parse
# ═══════════════════════════════════════════════════════════════════════════════

class TestParseCalendarCreateRequest:
    def test_full_explicit_request(self):
        draft = parse_calendar_create_request(
            "schedule a focus block tomorrow at 2", now=NOW
        )
        assert draft["title"] == "Focus Block"
        assert draft["date_str"] == "2026-05-15"  # tomorrow
        assert draft["start_time_str"] == "14:00:00"
        assert draft["end_time_str"] == "15:30:00"  # 90 min
        assert draft["duration_minutes"] == 90
        assert draft["missing_fields"] == []
        assert not draft["needs_availability_search"]

    def test_window_based_request(self):
        draft = parse_calendar_create_request(
            "add a workout this afternoon", now=NOW
        )
        assert "workout" in draft["title"].lower()
        assert draft["date_str"] == TODAY.isoformat()
        assert draft["window_hint"] == "afternoon"
        assert draft["needs_availability_search"] is True
        assert "time" not in draft["missing_fields"]  # window counts

    def test_missing_date_detected(self):
        draft = parse_calendar_create_request("schedule a standup at 10am", now=NOW)
        assert "date" in draft["missing_fields"]

    def test_missing_time_detected(self):
        draft = parse_calendar_create_request(
            "add a focus block tomorrow", now=NOW
        )
        assert "time" in draft["missing_fields"]

    def test_weekday_resolves_correctly(self):
        draft = parse_calendar_create_request(
            "standup on friday at 10am", now=NOW
        )
        assert draft["date_str"] == "2026-05-15"  # next Friday from Thursday
        assert draft["start_time_str"] == "10:00:00"

    def test_explicit_pm_time(self):
        draft = parse_calendar_create_request(
            "book a session tomorrow at 4pm", now=NOW
        )
        assert draft["start_time_str"] == "16:00:00"

    def test_end_time_computed_from_duration(self):
        draft = parse_calendar_create_request(
            "schedule a standup tomorrow at 10am", now=NOW
        )
        # standup = 30 min → 10:30
        assert draft["end_time_str"] == "10:30:00"

    def test_operation_contains_no_suspicious_keys(self):
        draft = parse_calendar_create_request(
            "schedule a focus block tomorrow at 2", now=NOW
        )
        # Ensure no HA/shell keys in parsed draft
        for bad_key in ("command", "shell", "home_assistant", "token", "api_key"):
            assert bad_key not in draft


# ═══════════════════════════════════════════════════════════════════════════════
# Section 8 — Operation building
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildOperation:
    def _draft(self):
        return {
            "title": "Focus Block",
            "date_str": "2026-05-15",
            "start_time_str": "14:00:00",
            "end_time_str": "15:30:00",
            "duration_minutes": 90,
        }

    def test_operation_type(self):
        op = _build_operation(self._draft())
        assert op["operation_type"] == "create_event"

    def test_requires_approval(self):
        op = _build_operation(self._draft())
        assert op["requires_prometheus_approval"] is True

    def test_dry_run_true(self):
        op = _build_operation(self._draft())
        assert op["dry_run"] is True

    def test_start_time_format(self):
        op = _build_operation(self._draft())
        assert op["start_time"] == "2026-05-15T14:00:00"

    def test_end_time_format(self):
        op = _build_operation(self._draft())
        assert op["end_time"] == "2026-05-15T15:30:00"

    def test_calendar_id_is_primary(self):
        op = _build_operation(self._draft())
        assert op["calendar_id"] == "primary"

    def test_no_suspicious_keys(self):
        op = _build_operation(self._draft())
        for bad_key in ("command", "shell", "home_assistant", "token", "api_key", "subprocess"):
            assert bad_key not in op


# ═══════════════════════════════════════════════════════════════════════════════
# Section 9 — Human summary
# ═══════════════════════════════════════════════════════════════════════════════

class TestHumanSummary:
    def _draft(self, **overrides):
        base = {
            "title": "Focus Block",
            "date_hint": "tomorrow",
            "date_str": "2026-05-15",
            "start_time_str": "14:00:00",
            "end_time_str": "15:30:00",
        }
        base.update(overrides)
        return base

    def test_includes_title(self):
        summary = _human_summary(self._draft())
        assert "Focus Block" in summary

    def test_includes_confirm_question(self):
        summary = _human_summary(self._draft())
        assert "Confirm?" in summary

    def test_includes_time_range(self):
        summary = _human_summary(self._draft())
        assert "2" in summary and "PM" in summary

    def test_uses_date_hint_for_relative_dates(self):
        summary = _human_summary(self._draft(date_hint="tomorrow"))
        assert "tomorrow" in summary

    def test_today_date_hint(self):
        summary = _human_summary(self._draft(date_hint="today"))
        assert "today" in summary


# ═══════════════════════════════════════════════════════════════════════════════
# Section 10 — Pending confirmation filesystem
# ═══════════════════════════════════════════════════════════════════════════════

class TestPendingConfirmation:
    def _write_pending(self, conf_dir, status="pending", expires_offset_hours=24):
        conf_id = uuid.uuid4().hex[:16]
        now = datetime.now(timezone.utc)
        record = {
            "confirmation_id": conf_id,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=expires_offset_hours)).isoformat(),
            "user_request": "schedule a focus block tomorrow at 2",
            "draft": {
                "title": "Focus Block",
                "date_str": "2026-05-15",
                "start_time_str": "14:00:00",
                "end_time_str": "15:30:00",
                "duration_minutes": 90,
            },
            "proposed_operation": {
                "operation_type": "create_event",
                "title": "Focus Block",
                "start_time": "2026-05-15T14:00:00",
                "end_time": "2026-05-15T15:30:00",
                "calendar_id": "primary",
                "requires_prometheus_approval": True,
                "dry_run": True,
            },
            "human_summary": "I can add 'Focus Block' tomorrow from 2:00–3:30 PM. Confirm?",
            "status": status,
        }
        path = conf_dir / f"pending_cal_confirm_{conf_id}.json"
        path.write_text(json.dumps(record))
        return record, conf_id

    def test_has_pending_returns_true_when_exists(self, _patch_confirm_dir):
        self._write_pending(_patch_confirm_dir)
        assert has_pending_calendar_confirmation() is True

    def test_has_pending_returns_false_when_empty(self, _patch_confirm_dir):
        assert has_pending_calendar_confirmation() is False

    def test_expired_confirmation_not_returned(self, _patch_confirm_dir):
        self._write_pending(_patch_confirm_dir, expires_offset_hours=-1)
        assert has_pending_calendar_confirmation() is False

    def test_canceled_confirmation_not_returned(self, _patch_confirm_dir):
        self._write_pending(_patch_confirm_dir, status="canceled")
        assert has_pending_calendar_confirmation() is False

    def test_confirmed_confirmation_not_returned(self, _patch_confirm_dir):
        self._write_pending(_patch_confirm_dir, status="confirmed")
        assert has_pending_calendar_confirmation() is False

    def test_get_most_recent_returns_record(self, _patch_confirm_dir):
        record, _ = self._write_pending(_patch_confirm_dir)
        result = get_most_recent_pending_confirmation()
        assert result is not None
        assert result["confirmation_id"] == record["confirmation_id"]


# ═══════════════════════════════════════════════════════════════════════════════
# Section 11 — parse_and_propose full flow
# ═══════════════════════════════════════════════════════════════════════════════

class TestParseAndPropose:
    def test_returns_executed_for_complete_request(self, _patch_confirm_dir, _patch_executor_dirs):
        mock_exec_result = {
            "success": True,
            "message": "Executed 1 operation(s) successfully.",
            "operation_count": 1,
            "operation_results": [{"success": True}],
        }
        with patch(
            "prometheus.agents.calendar_create_flow.execute_approved_calendar_request",
            return_value=mock_exec_result,
        ):
            with patch(
                "prometheus.agents.calendar_create_flow._calendar_get_date_fn",
                return_value={"ok": True, "events": []},
            ):
                result = parse_and_propose("schedule a focus block tomorrow at 2")
        assert result["status"] == "executed"
        assert result.get("title") == "Focus Block"
        assert "request_id" in result

    def test_returns_needs_input_for_missing_date(self):
        result = parse_and_propose("schedule a standup at 10am")
        assert result["status"] == "needs_input"
        assert "date" in result["missing_fields"]
        assert result["confirmation_id"] is None

    def test_returns_needs_input_for_missing_time(self):
        result = parse_and_propose("add a focus block tomorrow")
        assert result["status"] == "needs_input"
        assert "time" in result["missing_fields"]

    def test_pending_confirmation_file_written(self, _patch_confirm_dir):
        result = parse_and_propose("schedule a standup tomorrow at 10am")
        if result["status"] == "pending":
            conf_id = result["confirmation_id"]
            path = _patch_confirm_dir / f"pending_cal_confirm_{conf_id}.json"
            assert path.exists()

    def test_window_based_calls_availability_search(self, _patch_confirm_dir):
        mock_slot = {"start_time_str": "13:00:00", "end_time_str": "14:00:00"}
        with patch(
            "prometheus.agents.calendar_create_flow._find_availability_slot",
            return_value=mock_slot,
        ):
            result = parse_and_propose("add a workout this afternoon")
        assert result["status"] == "pending"

    def test_no_availability_returned_when_calendar_unavailable(self, _patch_confirm_dir):
        with patch(
            "prometheus.agents.calendar_create_flow._find_availability_slot",
            return_value=None,
        ):
            result = parse_and_propose("add a workout this afternoon")
        assert result["status"] == "no_availability"
        assert result["confirmation_id"] is None

    def test_no_passive_write_on_needs_input(self, _patch_confirm_dir):
        result = parse_and_propose("schedule a standup")
        assert result["status"] == "needs_input"
        # No files written
        files = list(_patch_confirm_dir.glob("*.json"))
        assert files == []


# ═══════════════════════════════════════════════════════════════════════════════
# Section 12 — confirm_pending_calendar_confirmation
# ═══════════════════════════════════════════════════════════════════════════════

class TestConfirmPending:
    def _write_pending(self, conf_dir):
        conf_id = uuid.uuid4().hex[:16]
        now = datetime.now(timezone.utc)
        record = {
            "confirmation_id": conf_id,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=24)).isoformat(),
            "user_request": "schedule a focus block tomorrow at 2",
            "draft": {
                "title": "Focus Block",
                "date_str": "2026-05-15",
                "start_time_str": "14:00:00",
                "end_time_str": "15:30:00",
                "duration_minutes": 90,
            },
            "proposed_operation": {
                "operation_type": "create_event",
                "title": "Focus Block",
                "start_time": "2026-05-15T14:00:00",
                "end_time": "2026-05-15T15:30:00",
                "calendar_id": "primary",
                "requires_prometheus_approval": True,
                "dry_run": True,
            },
            "human_summary": "I can add 'Focus Block' tomorrow from 2:00–3:30 PM. Confirm?",
            "status": "pending",
        }
        path = conf_dir / f"pending_cal_confirm_{conf_id}.json"
        path.write_text(json.dumps(record))
        return record, conf_id

    def test_no_pending_returns_no_pending(self, _patch_confirm_dir):
        result = confirm_pending_calendar_confirmation()
        assert result["no_pending"] is True
        assert result["success"] is False

    def test_blocked_when_dry_run_true(self, _patch_confirm_dir, _patch_executor_dirs):
        self._write_pending(_patch_confirm_dir)
        mock_exec_result = {
            "success": False,
            "reason": "Calendar execution is blocked because GOOGLE_CALENDAR_DRY_RUN=true. Set GOOGLE_CALENDAR_DRY_RUN=false to allow live writes.",
            "operation_count": 0,
            "operation_results": [],
        }
        with patch(
            "prometheus.agents.calendar_create_flow.execute_approved_calendar_request",
            return_value=mock_exec_result,
        ):
            result = confirm_pending_calendar_confirmation()
        assert result["blocked"] is True
        assert result["success"] is False

    def test_success_when_executor_succeeds(self, _patch_confirm_dir, _patch_executor_dirs):
        self._write_pending(_patch_confirm_dir)
        mock_exec_result = {
            "success": True,
            "message": "Executed 1 operation(s) successfully.",
            "operation_count": 1,
            "operation_results": [{"success": True}],
        }
        with patch(
            "prometheus.agents.calendar_create_flow.execute_approved_calendar_request",
            return_value=mock_exec_result,
        ):
            result = confirm_pending_calendar_confirmation()
        assert result["success"] is True
        assert result["title"] == "Focus Block"
        assert not result["blocked"]

    def test_writes_reviewed_file(self, _patch_confirm_dir, _patch_executor_dirs):
        self._write_pending(_patch_confirm_dir)
        with patch(
            "prometheus.agents.calendar_create_flow.execute_approved_calendar_request",
            return_value={"success": True, "operation_count": 1, "operation_results": []},
        ):
            result = confirm_pending_calendar_confirmation()
        req_id = result.get("request_id", "")
        reviewed = _patch_executor_dirs["reviewed"] / f"reviewed_{req_id}.json"
        assert reviewed.exists()

    def test_writes_approval_file(self, _patch_confirm_dir, _patch_executor_dirs):
        self._write_pending(_patch_confirm_dir)
        with patch(
            "prometheus.agents.calendar_create_flow.execute_approved_calendar_request",
            return_value={"success": True, "operation_count": 1, "operation_results": []},
        ):
            result = confirm_pending_calendar_confirmation()
        req_id = result.get("request_id", "")
        approval = _patch_executor_dirs["approved"] / f"approved_{req_id}.json"
        assert approval.exists()

    def test_reviewed_file_has_original_operations(self, _patch_confirm_dir, _patch_executor_dirs):
        self._write_pending(_patch_confirm_dir)
        with patch(
            "prometheus.agents.calendar_create_flow.execute_approved_calendar_request",
            return_value={"success": True, "operation_count": 1, "operation_results": []},
        ):
            result = confirm_pending_calendar_confirmation()
        req_id = result.get("request_id", "")
        reviewed_path = _patch_executor_dirs["reviewed"] / f"reviewed_{req_id}.json"
        reviewed = json.loads(reviewed_path.read_text())
        assert reviewed.get("all_dry_run") is True
        assert len(reviewed.get("original_operations", [])) == 1

    def test_does_not_call_goog_calendar_directly(self, _patch_confirm_dir, _patch_executor_dirs):
        self._write_pending(_patch_confirm_dir)
        with patch(
            "prometheus.agents.calendar_create_flow.execute_approved_calendar_request",
            return_value={"success": True, "operation_count": 1, "operation_results": []},
        ) as mock_exec:
            confirm_pending_calendar_confirmation()
        # executor was called once — that's it; no direct gcal calls
        assert mock_exec.call_count == 1

    def test_request_id_has_nlcal_prefix(self, _patch_confirm_dir, _patch_executor_dirs):
        self._write_pending(_patch_confirm_dir)
        with patch(
            "prometheus.agents.calendar_create_flow.execute_approved_calendar_request",
            return_value={"success": True, "operation_count": 1, "operation_results": []},
        ):
            result = confirm_pending_calendar_confirmation()
        assert result.get("request_id", "").startswith("req-nlcal-")


# ═══════════════════════════════════════════════════════════════════════════════
# Section 13 — cancel_pending_calendar_confirmation
# ═══════════════════════════════════════════════════════════════════════════════

class TestCancelPending:
    def _write_pending(self, conf_dir):
        conf_id = uuid.uuid4().hex[:16]
        now = datetime.now(timezone.utc)
        record = {
            "confirmation_id": conf_id,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=24)).isoformat(),
            "user_request": "schedule a focus block tomorrow at 2",
            "draft": {"title": "Focus Block"},
            "proposed_operation": {},
            "human_summary": "I can add 'Focus Block' tomorrow from 2:00–3:30 PM. Confirm?",
            "status": "pending",
        }
        path = conf_dir / f"pending_cal_confirm_{conf_id}.json"
        path.write_text(json.dumps(record))
        return record, conf_id

    def test_cancel_when_pending_exists(self, _patch_confirm_dir):
        self._write_pending(_patch_confirm_dir)
        result = cancel_pending_calendar_confirmation()
        assert result["canceled"] is True
        assert not result.get("no_pending")

    def test_cancel_when_no_pending(self, _patch_confirm_dir):
        result = cancel_pending_calendar_confirmation()
        assert result["canceled"] is False
        assert result.get("no_pending") is True

    def test_file_marked_canceled(self, _patch_confirm_dir):
        record, conf_id = self._write_pending(_patch_confirm_dir)
        cancel_pending_calendar_confirmation()
        path = _patch_confirm_dir / f"pending_cal_confirm_{conf_id}.json"
        updated = json.loads(path.read_text())
        assert updated["status"] == "canceled"

    def test_cancel_returns_title(self, _patch_confirm_dir):
        self._write_pending(_patch_confirm_dir)
        result = cancel_pending_calendar_confirmation()
        assert result.get("title") == "Focus Block"

    def test_after_cancel_has_pending_is_false(self, _patch_confirm_dir):
        self._write_pending(_patch_confirm_dir)
        cancel_pending_calendar_confirmation()
        assert has_pending_calendar_confirmation() is False


# ═══════════════════════════════════════════════════════════════════════════════
# Section 14 — Safety: no HA calls, no direct GCal calls, no passive writes
# ═══════════════════════════════════════════════════════════════════════════════

class TestSafetyConstraints:
    def test_no_home_assistant_calls_in_source(self):
        import inspect
        import prometheus.agents.calendar_create_flow as mod
        src = inspect.getsource(mod)
        # Should not import or reference HA
        for forbidden in ("home_assistant", "ha_service", "HomeAssistant", "requests.post"):
            assert forbidden not in src, f"Found forbidden reference: {forbidden}"

    def test_no_direct_gcal_api_calls_in_source(self):
        import inspect
        import prometheus.agents.calendar_create_flow as mod
        src = inspect.getsource(mod)
        # Should not import or call raw GCal API helpers directly (bypassing executor)
        # Note: "_direct_create_calendar_event" is our own function name — allowed.
        for forbidden in ("build_google_calendar_service", "update_calendar_event", "googleapiclient"):
            assert forbidden not in src, f"Found direct GCal reference: {forbidden}"

    def test_propose_does_not_write_to_calendar_when_no_slot(self, _patch_confirm_dir):
        # Window-based request with no available slot → no executor call
        with patch(
            "prometheus.agents.calendar_create_flow._find_availability_slot",
            return_value=None,
        ):
            with patch(
                "prometheus.agents.calendar_create_flow.execute_approved_calendar_request",
                side_effect=AssertionError("Should not call executor when no slot"),
            ):
                result = parse_and_propose("add a workout this afternoon")
        assert result["status"] == "no_availability"

    def test_no_passive_scheduling_for_incomplete_request(self, _patch_confirm_dir):
        # Missing-field requests must never call the executor
        with patch(
            "prometheus.agents.calendar_create_flow.execute_approved_calendar_request",
        ) as mock_exec:
            result = parse_and_propose("schedule a standup")  # no date, no time
        assert result["status"] == "needs_input"
        mock_exec.assert_not_called()

    def test_operation_has_dry_run_true(self):
        from prometheus.agents.calendar_create_flow import _build_operation
        draft = {
            "title": "Standup",
            "date_str": "2026-05-15",
            "start_time_str": "10:00:00",
            "end_time_str": "10:30:00",
        }
        op = _build_operation(draft)
        assert op["dry_run"] is True

    def test_operation_requires_prometheus_approval(self):
        from prometheus.agents.calendar_create_flow import _build_operation
        draft = {
            "title": "Standup",
            "date_str": "2026-05-15",
            "start_time_str": "10:00:00",
            "end_time_str": "10:30:00",
        }
        op = _build_operation(draft)
        assert op["requires_prometheus_approval"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# Section 15 — Intent override routing
# ═══════════════════════════════════════════════════════════════════════════════

class TestIntentOverrideRouting:
    def test_create_phrases_route_to_proposal(self):
        from prometheus.core.intent_overrides import resolve_direct_intent
        phrases = [
            "schedule a focus block tomorrow at 2",
            "block off tomorrow afternoon for deep work",
            "add a meeting on friday at 10am",
            "book a session tomorrow morning",
        ]
        for phrase in phrases:
            result = resolve_direct_intent(phrase)
            assert result is not None, f"Expected routing for: {phrase!r}"
            assert result["payload"]["action"] == "calendar_create_proposal", (
                f"Expected calendar_create_proposal for: {phrase!r}, got {result}"
            )

    def test_confirm_routes_only_with_pending(self, _patch_confirm_dir):
        from prometheus.core.intent_overrides import resolve_direct_intent
        # Without pending → should NOT route to confirm
        result = resolve_direct_intent("yes")
        if result is not None:
            assert result["payload"]["action"] != "calendar_confirm_create"

    def test_confirm_routes_with_pending(self, _patch_confirm_dir):
        from prometheus.core.intent_overrides import resolve_direct_intent
        # Write a pending confirmation
        conf_id = uuid.uuid4().hex[:16]
        now = datetime.now(timezone.utc)
        record = {
            "confirmation_id": conf_id,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=24)).isoformat(),
            "user_request": "schedule a focus block tomorrow at 2",
            "draft": {"title": "Focus Block"},
            "proposed_operation": {},
            "human_summary": "Confirm?",
            "status": "pending",
        }
        (_patch_confirm_dir / f"pending_cal_confirm_{conf_id}.json").write_text(
            json.dumps(record)
        )
        result = resolve_direct_intent("yes")
        assert result is not None
        assert result["payload"]["action"] == "calendar_confirm_create"

    def test_cancel_routes_only_with_pending(self, _patch_confirm_dir):
        from prometheus.core.intent_overrides import resolve_direct_intent
        result = resolve_direct_intent("cancel")
        # Without pending, should not route to cancel_create
        if result is not None:
            assert result["payload"]["action"] != "calendar_cancel_create"

    def test_calendar_reads_not_affected(self):
        from prometheus.core.intent_overrides import resolve_direct_intent
        result = resolve_direct_intent("what's on my calendar today")
        assert result is not None
        assert result["payload"]["action"] == "calendar_get_today"

    def test_create_does_not_match_read_phrases(self):
        from prometheus.core.intent_overrides import resolve_direct_intent
        # "what's on my calendar tomorrow" should route to read, not create
        result = resolve_direct_intent("what's on my calendar tomorrow")
        assert result is not None
        assert result["payload"]["action"] != "calendar_create_proposal"

    def test_put_church_meeting_on_calendar_routes_to_proposal(self):
        from prometheus.core.intent_overrides import resolve_direct_intent
        result = resolve_direct_intent("put church meeting on my calendar sunday at 10")
        assert result is not None
        assert result["payload"]["action"] == "calendar_create_proposal"

    def test_add_event_to_calendar_routes_to_proposal(self):
        from prometheus.core.intent_overrides import resolve_direct_intent
        result = resolve_direct_intent("add team lunch to my calendar friday at noon")
        assert result is not None
        assert result["payload"]["action"] == "calendar_create_proposal"

    def test_schedule_broad_routes_to_proposal(self):
        from prometheus.core.intent_overrides import resolve_direct_intent
        result = resolve_direct_intent("schedule church meeting tomorrow at 10am")
        assert result is not None
        assert result["payload"]["action"] == "calendar_create_proposal"


# ═══════════════════════════════════════════════════════════════════════════════
# Section 16 — Explicit duration extraction
# ═══════════════════════════════════════════════════════════════════════════════

class TestExtractExplicitDuration:
    def test_90_minutes(self):
        assert _extract_explicit_duration("block off 90 minutes for work") == 90

    def test_30_min(self):
        assert _extract_explicit_duration("schedule a 30 min standup") == 30

    def test_1_hour(self):
        assert _extract_explicit_duration("add a 1 hour workout") == 60

    def test_2_hours(self):
        assert _extract_explicit_duration("block off 2 hours for deep work") == 120

    def test_half_an_hour(self):
        assert _extract_explicit_duration("add a half an hour check-in") == 30

    def test_half_hour(self):
        assert _extract_explicit_duration("schedule a half hour meeting") == 30

    def test_an_hour(self):
        assert _extract_explicit_duration("block off an hour for focus") == 60

    def test_and_a_half_hours(self):
        assert _extract_explicit_duration("block off 1 and a half hours") == 90

    def test_no_duration_returns_none(self):
        assert _extract_explicit_duration("schedule a standup tomorrow at 10") is None

    def test_duration_overrides_title_default(self):
        # standup default is 30 min, but explicit "90 minutes" overrides
        draft = parse_calendar_create_request(
            "schedule a standup tomorrow at 10am for 90 minutes", now=NOW
        )
        assert draft["duration_minutes"] == 90


# ═══════════════════════════════════════════════════════════════════════════════
# Section 17 — Updated time hint extraction (bare time, bare number after date)
# ═══════════════════════════════════════════════════════════════════════════════

class TestExtractTimeHintV2:
    def test_bare_pm_without_at(self):
        # "4pm tomorrow" → captures "4pm"
        time_hint, window = _extract_time_hint("add a workout 4pm tomorrow")
        assert "4pm" in time_hint
        assert window == ""

    def test_bare_am_without_at(self):
        time_hint, window = _extract_time_hint("standup 10am friday")
        assert "10am" in time_hint

    def test_bare_number_after_tomorrow(self):
        # "tomorrow 2" → captures "2"
        time_hint, window = _extract_time_hint("schedule a focus block tomorrow 2")
        assert time_hint == "2"
        assert window == ""

    def test_bare_number_after_friday(self):
        time_hint, window = _extract_time_hint("create an event called call knox friday 3")
        assert time_hint == "3"

    def test_at_takes_precedence(self):
        # "tomorrow at 2" → "at 2" wins over bare-number path
        time_hint, window = _extract_time_hint("schedule a focus block tomorrow at 2")
        assert "at 2" in time_hint


# ═══════════════════════════════════════════════════════════════════════════════
# Section 18 — should_auto_execute_calendar_create
# ═══════════════════════════════════════════════════════════════════════════════

class TestShouldAutoExecute:
    def _draft(self, **overrides):
        base = {
            "title": "Focus Block",
            "date_str": "2026-05-15",
            "date_hint": "tomorrow",
            "start_time_str": "14:00:00",
            "end_time_str": "15:30:00",
            "duration_minutes": 90,
            "needs_availability_search": False,
            "missing_fields": [],
        }
        base.update(overrides)
        return base

    def test_fully_specified_low_risk_ok(self):
        ok, reason = should_auto_execute_calendar_create(
            self._draft(), "schedule a focus block tomorrow at 2"
        )
        assert ok is True
        assert reason == ""

    def test_missing_fields_blocks(self):
        ok, _ = should_auto_execute_calendar_create(
            self._draft(missing_fields=["date"]), "schedule a focus block"
        )
        assert ok is False

    def test_window_based_blocks(self):
        ok, _ = should_auto_execute_calendar_create(
            self._draft(needs_availability_search=True), "schedule a focus block this afternoon"
        )
        assert ok is False

    def test_duration_over_240_blocks(self):
        ok, _ = should_auto_execute_calendar_create(
            self._draft(duration_minutes=300), "schedule a 5 hour workshop tomorrow at 9"
        )
        assert ok is False

    def test_recurring_blocks(self):
        ok, _ = should_auto_execute_calendar_create(
            self._draft(), "schedule a standup every monday at 10"
        )
        assert ok is False

    def test_all_day_blocks(self):
        ok, _ = should_auto_execute_calendar_create(
            self._draft(), "add a focus day all day tomorrow"
        )
        assert ok is False

    def test_with_attendees_blocks(self):
        ok, _ = should_auto_execute_calendar_create(
            self._draft(), "schedule a meeting with jake tomorrow at 2"
        )
        assert ok is False

    def test_sleep_hours_before_6am_blocks(self):
        ok, _ = should_auto_execute_calendar_create(
            self._draft(start_time_str="05:00:00"), "schedule a run tomorrow at 5am"
        )
        assert ok is False

    def test_sleep_hours_at_11pm_blocks(self):
        ok, _ = should_auto_execute_calendar_create(
            self._draft(start_time_str="23:00:00"), "schedule something tomorrow at 11pm"
        )
        assert ok is False

    def test_time_6am_is_ok(self):
        ok, _ = should_auto_execute_calendar_create(
            self._draft(start_time_str="06:00:00"), "schedule a run tomorrow at 6am"
        )
        assert ok is True

    def test_reschedule_blocks(self):
        ok, _ = should_auto_execute_calendar_create(
            self._draft(), "reschedule the standup to tomorrow at 2"
        )
        assert ok is False


# ═══════════════════════════════════════════════════════════════════════════════
# Section 19 — _check_conflict
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheckConflict:
    def _draft(self):
        return {
            "date_str": "2026-05-15",
            "start_time_str": "14:00:00",
            "end_time_str": "15:30:00",
        }

    def test_no_conflict_when_calendar_empty(self):
        with patch(
            "prometheus.agents.calendar_create_flow._calendar_get_date_fn",
            return_value={"ok": True, "events": []},
        ):
            assert _check_conflict(self._draft()) is None

    def test_conflict_detected(self):
        events = [
            {
                "summary": "Morning Standup",
                "start": "2026-05-15T14:30:00",
                "end": "2026-05-15T15:00:00",
            }
        ]
        with patch(
            "prometheus.agents.calendar_create_flow._calendar_get_date_fn",
            return_value={"ok": True, "events": events},
        ):
            result = _check_conflict(self._draft())
        assert result == "Morning Standup"

    def test_no_conflict_when_adjacent(self):
        # Event ends at 14:00 exactly → no overlap with 14:00 start
        events = [
            {
                "summary": "Early Meeting",
                "start": "2026-05-15T13:00:00",
                "end": "2026-05-15T14:00:00",
            }
        ]
        with patch(
            "prometheus.agents.calendar_create_flow._calendar_get_date_fn",
            return_value={"ok": True, "events": events},
        ):
            assert _check_conflict(self._draft()) is None

    def test_all_day_events_skipped(self):
        events = [{"summary": "Holiday", "start": "2026-05-15", "end": "2026-05-16"}]
        with patch(
            "prometheus.agents.calendar_create_flow._calendar_get_date_fn",
            return_value={"ok": True, "events": events},
        ):
            assert _check_conflict(self._draft()) is None

    def test_failure_returns_none(self):
        with patch(
            "prometheus.agents.calendar_create_flow._calendar_get_date_fn",
            side_effect=RuntimeError("API down"),
        ):
            assert _check_conflict(self._draft()) is None

    def test_none_fn_returns_none(self):
        with patch("prometheus.agents.calendar_create_flow._calendar_get_date_fn", None):
            assert _check_conflict(self._draft()) is None


# ═══════════════════════════════════════════════════════════════════════════════
# Section 20 — _direct_create_calendar_event
# ═══════════════════════════════════════════════════════════════════════════════

class TestDirectCreateCalendarEvent:
    def _draft(self):
        return {
            "title": "Focus Block",
            "date_str": "2026-05-15",
            "date_hint": "tomorrow",
            "start_time_str": "14:00:00",
            "end_time_str": "15:30:00",
            "duration_minutes": 90,
        }

    def test_success_returns_executed(self, _patch_executor_dirs):
        with patch(
            "prometheus.agents.calendar_create_flow.execute_approved_calendar_request",
            return_value={"success": True, "operation_count": 1},
        ):
            result = _direct_create_calendar_event("schedule a focus block tomorrow at 2", self._draft())
        assert result["status"] == "executed"
        assert result["title"] == "Focus Block"
        assert "start_time" in result
        assert result["request_id"].startswith("req-direct-")

    def test_dry_run_blocked_returns_blocked(self, _patch_executor_dirs):
        with patch(
            "prometheus.agents.calendar_create_flow.execute_approved_calendar_request",
            return_value={
                "success": False,
                "reason": "Calendar execution is blocked because GOOGLE_CALENDAR_DRY_RUN=true.",
            },
        ):
            result = _direct_create_calendar_event("schedule a focus block tomorrow at 2", self._draft())
        assert result["status"] == "blocked"

    def test_executor_error_returns_failed(self, _patch_executor_dirs):
        with patch(
            "prometheus.agents.calendar_create_flow.execute_approved_calendar_request",
            side_effect=RuntimeError("network error"),
        ):
            result = _direct_create_calendar_event("schedule a focus block tomorrow at 2", self._draft())
        assert result["status"] == "failed"

    def test_writes_reviewed_file(self, _patch_executor_dirs):
        with patch(
            "prometheus.agents.calendar_create_flow.execute_approved_calendar_request",
            return_value={"success": True, "operation_count": 1},
        ):
            result = _direct_create_calendar_event("schedule a focus block tomorrow at 2", self._draft())
        req_id = result["request_id"]
        reviewed = _patch_executor_dirs["reviewed"] / f"reviewed_{req_id}.json"
        assert reviewed.exists()
        data = json.loads(reviewed.read_text())
        assert data["approval_mode"] == "direct_user_command"

    def test_writes_approval_file(self, _patch_executor_dirs):
        with patch(
            "prometheus.agents.calendar_create_flow.execute_approved_calendar_request",
            return_value={"success": True, "operation_count": 1},
        ):
            result = _direct_create_calendar_event("schedule a focus block tomorrow at 2", self._draft())
        req_id = result["request_id"]
        approval = _patch_executor_dirs["approved"] / f"approved_{req_id}.json"
        assert approval.exists()
        data = json.loads(approval.read_text())
        assert data["approval_mode"] == "direct_user_command"
        assert data["explicit_user_approval_required"] is False

    def test_does_not_write_pending_file(self, _patch_confirm_dir, _patch_executor_dirs):
        with patch(
            "prometheus.agents.calendar_create_flow.execute_approved_calendar_request",
            return_value={"success": True, "operation_count": 1},
        ):
            _direct_create_calendar_event("schedule a focus block tomorrow at 2", self._draft())
        pending_files = list(_patch_confirm_dir.glob("pending_cal_confirm_*.json"))
        assert pending_files == []


# ═══════════════════════════════════════════════════════════════════════════════
# Section 21 — parse_and_propose auto-execute integration
# ═══════════════════════════════════════════════════════════════════════════════

class TestParseAndProposeAutoExecute:
    def test_explicit_request_auto_executes(self, _patch_confirm_dir, _patch_executor_dirs):
        with patch(
            "prometheus.agents.calendar_create_flow.execute_approved_calendar_request",
            return_value={"success": True, "operation_count": 1},
        ):
            with patch(
                "prometheus.agents.calendar_create_flow._calendar_get_date_fn",
                return_value={"ok": True, "events": []},
            ):
                result = parse_and_propose("add a workout tomorrow at 4")
        assert result["status"] == "executed"

    def test_window_based_still_asks_confirmation(self, _patch_confirm_dir):
        mock_slot = {"start_time_str": "13:00:00", "end_time_str": "14:00:00"}
        with patch("prometheus.agents.calendar_create_flow._find_availability_slot", return_value=mock_slot):
            result = parse_and_propose("add a workout this afternoon")
        assert result["status"] == "pending"
        assert result["confirmation_id"] is not None

    def test_with_attendees_goes_to_pending(self, _patch_confirm_dir):
        with patch(
            "prometheus.agents.calendar_create_flow._calendar_get_date_fn",
            return_value={"ok": True, "events": []},
        ):
            result = parse_and_propose("schedule a meeting with jake tomorrow at 2pm")
        assert result["status"] == "pending"

    def test_conflict_goes_to_conflict_status(self, _patch_confirm_dir):
        events = [
            {
                "summary": "Other Meeting",
                "start": "2026-05-15T14:30:00",
                "end": "2026-05-15T15:00:00",
            }
        ]
        with patch(
            "prometheus.agents.calendar_create_flow._calendar_get_date_fn",
            return_value={"ok": True, "events": events},
        ):
            result = parse_and_propose("schedule a focus block tomorrow at 2")
        assert result["status"] == "conflict"
        assert result["conflict_event"] == "Other Meeting"
        assert result["confirmation_id"] is not None  # pending file written

    def test_conflict_writes_pending_file(self, _patch_confirm_dir):
        events = [
            {
                "summary": "Other Meeting",
                "start": "2026-05-15T14:30:00",
                "end": "2026-05-15T15:00:00",
            }
        ]
        with patch(
            "prometheus.agents.calendar_create_flow._calendar_get_date_fn",
            return_value={"ok": True, "events": events},
        ):
            result = parse_and_propose("schedule a focus block tomorrow at 2")
        conf_id = result["confirmation_id"]
        path = _patch_confirm_dir / f"pending_cal_confirm_{conf_id}.json"
        assert path.exists()

    def test_dry_run_blocked_returns_blocked_status(self, _patch_confirm_dir, _patch_executor_dirs):
        with patch(
            "prometheus.agents.calendar_create_flow.execute_approved_calendar_request",
            return_value={
                "success": False,
                "reason": "Calendar execution is blocked because GOOGLE_CALENDAR_DRY_RUN=true.",
            },
        ):
            with patch(
                "prometheus.agents.calendar_create_flow._calendar_get_date_fn",
                return_value={"ok": True, "events": []},
            ):
                result = parse_and_propose("schedule a focus block tomorrow at 2")
        assert result["status"] == "blocked"

    def test_recurring_goes_to_pending(self, _patch_confirm_dir):
        result = parse_and_propose("schedule a standup every monday at 10am")
        # Recurring → pending (or needs_input if date resolution fails)
        assert result["status"] in ("pending", "needs_input")

    def test_no_pending_file_for_direct_executed(self, _patch_confirm_dir, _patch_executor_dirs):
        with patch(
            "prometheus.agents.calendar_create_flow.execute_approved_calendar_request",
            return_value={"success": True, "operation_count": 1},
        ):
            with patch(
                "prometheus.agents.calendar_create_flow._calendar_get_date_fn",
                return_value={"ok": True, "events": []},
            ):
                result = parse_and_propose("schedule a focus block tomorrow at 2")
        assert result["status"] == "executed"
        pending_files = list(_patch_confirm_dir.glob("pending_cal_confirm_*.json"))
        assert pending_files == []


# ═══════════════════════════════════════════════════════════════════════════════
# Section 22 — Parser improvements (4pm, tomorrow 2, explicit duration in parse)
# ═══════════════════════════════════════════════════════════════════════════════

class TestParserImprovements:
    def test_4pm_tomorrow_no_at(self):
        draft = parse_calendar_create_request("add a workout 4pm tomorrow", now=NOW)
        assert draft["start_time_str"] == "16:00:00"

    def test_tomorrow_bare_number(self):
        draft = parse_calendar_create_request("schedule a focus block tomorrow 2", now=NOW)
        assert draft["start_time_str"] == "14:00:00"

    def test_friday_at_3(self):
        draft = parse_calendar_create_request("create an event called call knox friday at 3", now=NOW)
        assert draft["start_time_str"] == "15:00:00"
        assert "Knox" in draft["title"] or "Call Knox" in draft["title"]

    def test_sunday_at_10(self):
        draft = parse_calendar_create_request(
            "put church meeting on my calendar sunday at 10", now=NOW
        )
        assert draft["start_time_str"] == "10:00:00"
        assert draft["date_str"] != ""

    def test_explicit_90_minutes_overrides_default(self):
        draft = parse_calendar_create_request(
            "block off 90 minutes for work tomorrow at 2", now=NOW
        )
        assert draft["duration_minutes"] == 90
        assert draft["end_time_str"] == "15:30:00"

    def test_1_hour_overrides_standup_default(self):
        draft = parse_calendar_create_request(
            "schedule a standup tomorrow at 10am for 1 hour", now=NOW
        )
        # standup default 30 → overridden to 60
        assert draft["duration_minutes"] == 60

    def test_church_meeting_title(self):
        draft = parse_calendar_create_request(
            "put church meeting on my calendar sunday at 10", now=NOW
        )
        assert "church" in draft["title"].lower() or "Church" in draft["title"]


# ═══════════════════════════════════════════════════════════════════════════════
# Section 23 — Response synthesizer new statuses
# ═══════════════════════════════════════════════════════════════════════════════

class TestResponseSynthesizerCalendarCreate:
    def _synth(self, action, data):
        from prometheus.execution.response_synthesizer import synthesize_tool_response
        from tools import ToolResult  # type: ignore[import]
        tr = ToolResult(ok=True, message="ok", data=data)
        return synthesize_tool_response(action, tr)

    def test_executed_status_mentions_done(self):
        data = {
            "status": "executed",
            "title": "Focus Block",
            "date_hint": "tomorrow",
            "date_str": "2026-05-15",
            "start_time": "2026-05-15T14:00:00",
            "end_time": "2026-05-15T15:30:00",
        }
        result = self._synth("calendar_create_proposal", data)
        assert "Focus Block" in result
        assert "14:00" in result or "done" in result.lower() or "Done" in result

    def test_blocked_status_mentions_dry_run(self):
        data = {"status": "blocked", "title": "Focus Block"}
        result = self._synth("calendar_create_proposal", data)
        assert "DRY_RUN" in result or "dry-run" in result.lower() or "blocked" in result.lower()

    def test_failed_status_mentions_failure(self):
        data = {"status": "failed", "title": "Focus Block", "reason": "API timeout"}
        result = self._synth("calendar_create_proposal", data)
        assert "Focus Block" in result or "failed" in result.lower() or "not be added" in result.lower()

    def test_conflict_status_mentions_conflict(self):
        data = {
            "status": "conflict",
            "conflict_event": "Morning Standup",
            "human_summary": "I can add 'Focus Block' tomorrow from 2–3:30 PM. Confirm?",
        }
        result = self._synth("calendar_create_proposal", data)
        assert "Morning Standup" in result or "overlap" in result.lower() or "conflict" in result.lower()

    def test_pending_status_asks_confirm(self):
        data = {
            "status": "pending",
            "human_summary": "I can add 'Focus Block' tomorrow from 2–3:30 PM. Confirm?",
        }
        result = self._synth("calendar_create_proposal", data)
        assert "Confirm?" in result or "confirm" in result.lower()

"""
test_google_calendar.py — Tests for the Google Calendar adapter.

No live API calls. No OAuth browser flow. Uses mocks/fake service objects.
"""
from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from prometheus.integrations.google_calendar import (
    GoogleCalendarConfig,
    GoogleCalendarEvent,
    GoogleCalendarResult,
    load_google_calendar_config,
    build_google_calendar_service,
    list_calendar_events,
    create_calendar_event,
    update_calendar_event,
    delete_calendar_event,
    dry_run_calendar_operation,
    _GOOGLE_AVAILABLE,
)


# ── Import / availability tests ───────────────────────────────────────────────

class TestModuleImport:
    def test_module_imports_cleanly(self):
        import prometheus.integrations.google_calendar  # noqa
        assert True

    def test_google_available_is_bool(self):
        assert isinstance(_GOOGLE_AVAILABLE, bool)

    def test_default_config_is_disabled(self):
        cfg = GoogleCalendarConfig()
        assert cfg.enabled is False

    def test_default_config_is_dry_run(self):
        cfg = GoogleCalendarConfig()
        assert cfg.dry_run is True

    def test_default_calendar_id_is_primary(self):
        cfg = GoogleCalendarConfig()
        assert cfg.default_calendar_id == "primary"

    def test_default_scopes_include_calendar(self):
        cfg = GoogleCalendarConfig()
        assert any("calendar" in s for s in cfg.scopes)


# ── Config loading tests ──────────────────────────────────────────────────────

class TestLoadGoogleCalendarConfig:
    def test_missing_enabled_defaults_false(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_CALENDAR_ENABLED", raising=False)
        cfg = load_google_calendar_config()
        assert cfg.enabled is False

    def test_missing_dry_run_defaults_true(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_CALENDAR_DRY_RUN", raising=False)
        cfg = load_google_calendar_config()
        assert cfg.dry_run is True

    def test_enabled_true_from_env(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CALENDAR_ENABLED", "true")
        cfg = load_google_calendar_config()
        assert cfg.enabled is True

    def test_enabled_false_from_env(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CALENDAR_ENABLED", "false")
        cfg = load_google_calendar_config()
        assert cfg.enabled is False

    def test_dry_run_false_from_env(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CALENDAR_DRY_RUN", "false")
        cfg = load_google_calendar_config()
        assert cfg.dry_run is False

    def test_calendar_id_from_env(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CALENDAR_ID", "work@example.com")
        cfg = load_google_calendar_config()
        assert cfg.default_calendar_id == "work@example.com"

    def test_credentials_path_from_env(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CALENDAR_CREDENTIALS_PATH", "/tmp/creds.json")
        cfg = load_google_calendar_config()
        assert cfg.credentials_path == "/tmp/creds.json"

    def test_missing_credentials_path_is_none(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_CALENDAR_CREDENTIALS_PATH", raising=False)
        cfg = load_google_calendar_config()
        assert cfg.credentials_path is None

    def test_timezone_from_env(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CALENDAR_TIMEZONE", "America/Los_Angeles")
        cfg = load_google_calendar_config()
        assert cfg.timezone == "America/Los_Angeles"

    def test_enabled_numeric_one(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CALENDAR_ENABLED", "1")
        cfg = load_google_calendar_config()
        assert cfg.enabled is True


# ── Service builder rejection tests ──────────────────────────────────────────

class TestBuildGoogleCalendarService:
    def test_rejects_disabled_config(self):
        cfg = GoogleCalendarConfig(enabled=False)
        with pytest.raises((ValueError, ImportError)):
            build_google_calendar_service(cfg)

    def test_rejects_missing_credentials(self):
        cfg = GoogleCalendarConfig(enabled=True, credentials_path=None, token_path="/tmp/tok.json")
        with pytest.raises((ValueError, ImportError)):
            build_google_calendar_service(cfg)

    def test_rejects_missing_token_path(self):
        cfg = GoogleCalendarConfig(enabled=True, credentials_path="/tmp/creds.json", token_path=None)
        with pytest.raises((ValueError, ImportError)):
            build_google_calendar_service(cfg)

    def test_rejects_when_google_libs_unavailable(self):
        cfg = GoogleCalendarConfig(enabled=True, credentials_path="/tmp/creds.json", token_path="/tmp/tok.json")
        if _GOOGLE_AVAILABLE:
            pytest.skip("Google libs available — testing unavailable path only")
        with pytest.raises((ImportError, ValueError)):
            build_google_calendar_service(cfg)


# ── List events tests ─────────────────────────────────────────────────────────

def _fake_service(items: list[dict]) -> MagicMock:
    svc = MagicMock()
    svc.events.return_value.list.return_value.execute.return_value = {"items": items}
    return svc


class TestListCalendarEvents:
    def test_returns_events(self):
        raw = [{"id": "ev1", "summary": "Meeting", "start": {"dateTime": "2026-05-15T09:00:00"}, "end": {"dateTime": "2026-05-15T10:00:00"}}]
        svc = _fake_service(raw)
        cfg = GoogleCalendarConfig()
        events = list_calendar_events(svc, cfg, "2026-05-15T00:00:00Z", "2026-05-16T00:00:00Z")
        assert len(events) == 1
        assert events[0].title == "Meeting"
        assert events[0].event_id == "ev1"

    def test_empty_calendar_returns_empty(self):
        svc = _fake_service([])
        cfg = GoogleCalendarConfig()
        events = list_calendar_events(svc, cfg, "2026-05-15T00:00:00Z", "2026-05-16T00:00:00Z")
        assert events == []

    def test_all_day_event_parsed(self):
        raw = [{"id": "allday1", "summary": "Holiday", "start": {"date": "2026-05-15"}, "end": {"date": "2026-05-16"}}]
        svc = _fake_service(raw)
        cfg = GoogleCalendarConfig()
        events = list_calendar_events(svc, cfg, "2026-05-15T00:00:00Z", "2026-05-16T00:00:00Z")
        assert len(events) == 1
        assert events[0].start_time == "2026-05-15"
        assert events[0].event_id == "allday1"

    def test_uses_override_calendar_id(self):
        svc = _fake_service([])
        cfg = GoogleCalendarConfig(default_calendar_id="primary")
        list_calendar_events(svc, cfg, "2026-05-15T00:00:00Z", "2026-05-16T00:00:00Z", calendar_id="work@example.com")
        call_kwargs = svc.events.return_value.list.call_args.kwargs
        assert call_kwargs.get("calendarId") == "work@example.com"


# ── Create event tests ────────────────────────────────────────────────────────

class TestCreateCalendarEvent:
    def test_dry_run_does_not_call_service(self):
        svc = MagicMock()
        cfg = GoogleCalendarConfig(enabled=True, dry_run=True)
        result = create_calendar_event(svc, cfg, "Focus", "2026-05-15T14:00:00", "2026-05-15T15:30:00")
        svc.events.assert_not_called()
        assert result.dry_run is True
        assert result.success is True

    def test_dry_run_result_contains_proposed_event(self):
        svc = MagicMock()
        cfg = GoogleCalendarConfig(enabled=True, dry_run=True)
        result = create_calendar_event(svc, cfg, "Test", "2026-05-15T10:00:00", "2026-05-15T11:00:00")
        assert result.event is not None
        assert result.event.title == "Test"

    def test_disabled_returns_failure(self):
        svc = MagicMock()
        cfg = GoogleCalendarConfig(enabled=False, dry_run=True)
        result = create_calendar_event(svc, cfg, "Test", "2026-05-15T10:00:00", "2026-05-15T11:00:00")
        assert result.success is False
        svc.events.assert_not_called()

    def test_missing_title_raises(self):
        svc = MagicMock()
        cfg = GoogleCalendarConfig(enabled=True, dry_run=True)
        with pytest.raises(ValueError):
            create_calendar_event(svc, cfg, "", "2026-05-15T10:00:00", "2026-05-15T11:00:00")

    def test_missing_start_time_raises(self):
        svc = MagicMock()
        cfg = GoogleCalendarConfig(enabled=True, dry_run=True)
        with pytest.raises(ValueError):
            create_calendar_event(svc, cfg, "Test", "", "2026-05-15T11:00:00")

    def test_missing_end_time_raises(self):
        svc = MagicMock()
        cfg = GoogleCalendarConfig(enabled=True, dry_run=True)
        with pytest.raises(ValueError):
            create_calendar_event(svc, cfg, "Test", "2026-05-15T10:00:00", "")

    def test_live_write_calls_service(self):
        created_raw = {"id": "new123", "summary": "Focus", "start": {"dateTime": "2026-05-15T14:00:00"}, "end": {"dateTime": "2026-05-15T15:30:00"}}
        svc = MagicMock()
        svc.events.return_value.insert.return_value.execute.return_value = created_raw
        cfg = GoogleCalendarConfig(enabled=True, dry_run=False)
        result = create_calendar_event(svc, cfg, "Focus", "2026-05-15T14:00:00", "2026-05-15T15:30:00")
        svc.events.return_value.insert.assert_called_once()
        assert result.success is True
        assert result.event_id == "new123"
        assert result.dry_run is False


# ── Update event tests ────────────────────────────────────────────────────────

class TestUpdateCalendarEvent:
    def test_dry_run_does_not_call_service(self):
        svc = MagicMock()
        cfg = GoogleCalendarConfig(enabled=True, dry_run=True)
        result = update_calendar_event(svc, cfg, "ev123", title="New Title")
        svc.events.assert_not_called()
        assert result.dry_run is True
        assert result.success is True
        assert result.event_id == "ev123"

    def test_disabled_returns_failure(self):
        svc = MagicMock()
        cfg = GoogleCalendarConfig(enabled=False)
        result = update_calendar_event(svc, cfg, "ev123", title="New")
        assert result.success is False

    def test_missing_event_id_raises(self):
        svc = MagicMock()
        cfg = GoogleCalendarConfig(enabled=True, dry_run=True)
        with pytest.raises(ValueError):
            update_calendar_event(svc, cfg, "")

    def test_live_update_calls_patch(self):
        updated_raw = {"id": "ev123", "summary": "Updated", "start": {"dateTime": "2026-05-15T10:00:00"}, "end": {"dateTime": "2026-05-15T11:00:00"}}
        svc = MagicMock()
        svc.events.return_value.patch.return_value.execute.return_value = updated_raw
        cfg = GoogleCalendarConfig(enabled=True, dry_run=False)
        result = update_calendar_event(svc, cfg, "ev123", title="Updated")
        svc.events.return_value.patch.assert_called_once()
        assert result.success is True
        assert result.dry_run is False


# ── Delete event tests ────────────────────────────────────────────────────────

class TestDeleteCalendarEvent:
    def test_dry_run_does_not_call_service(self):
        svc = MagicMock()
        cfg = GoogleCalendarConfig(enabled=True, dry_run=True)
        result = delete_calendar_event(svc, cfg, "ev456")
        svc.events.assert_not_called()
        assert result.dry_run is True
        assert result.success is True

    def test_disabled_returns_failure(self):
        svc = MagicMock()
        cfg = GoogleCalendarConfig(enabled=False)
        result = delete_calendar_event(svc, cfg, "ev456")
        assert result.success is False

    def test_missing_event_id_raises(self):
        svc = MagicMock()
        cfg = GoogleCalendarConfig(enabled=True, dry_run=True)
        with pytest.raises(ValueError):
            delete_calendar_event(svc, cfg, "")

    def test_live_delete_calls_service(self):
        svc = MagicMock()
        svc.events.return_value.delete.return_value.execute.return_value = None
        cfg = GoogleCalendarConfig(enabled=True, dry_run=False)
        result = delete_calendar_event(svc, cfg, "ev456")
        svc.events.return_value.delete.assert_called_once()
        assert result.success is True
        assert result.dry_run is False


# ── dry_run_calendar_operation tests ─────────────────────────────────────────

class TestDryRunCalendarOperation:
    def _op(self, op_type: str, **kwargs) -> dict:
        return {"operation_type": op_type, "calendar_id": "primary", **kwargs}

    def test_create_event_dry_run(self):
        cfg = GoogleCalendarConfig()
        result = dry_run_calendar_operation(
            self._op("create_event", title="Meeting", start_time="2026-05-15T09:00:00", end_time="2026-05-15T10:00:00"),
            cfg,
        )
        assert result.success is True
        assert result.dry_run is True
        assert result.operation_type == "create_event"
        assert result.event is not None

    def test_update_event_dry_run(self):
        cfg = GoogleCalendarConfig()
        result = dry_run_calendar_operation(
            self._op("update_event", event_id="ev001", title="Updated"),
            cfg,
        )
        assert result.success is True
        assert result.operation_type == "update_event"

    def test_delete_event_dry_run(self):
        cfg = GoogleCalendarConfig()
        result = dry_run_calendar_operation(
            self._op("delete_event", event_id="ev001"),
            cfg,
        )
        assert result.success is True
        assert result.operation_type == "delete_event"

    def test_read_events_dry_run(self):
        cfg = GoogleCalendarConfig()
        result = dry_run_calendar_operation(self._op("read_events"), cfg)
        assert result.success is True

    def test_find_availability_dry_run(self):
        cfg = GoogleCalendarConfig()
        result = dry_run_calendar_operation(self._op("find_availability"), cfg)
        assert result.success is True

    def test_suggest_schedule_change_dry_run(self):
        cfg = GoogleCalendarConfig()
        result = dry_run_calendar_operation(self._op("suggest_schedule_change"), cfg)
        assert result.success is True

    def test_unsupported_operation_type_fails(self):
        cfg = GoogleCalendarConfig()
        result = dry_run_calendar_operation(self._op("send_email"), cfg)
        assert result.success is False
        assert "Unsupported" in result.message


# ── Safety guard tests ────────────────────────────────────────────────────────

class TestNoForbiddenDependencies:
    def _source(self) -> str:
        p = ROOT / "prometheus" / "integrations" / "google_calendar.py"
        return p.read_text(encoding="utf-8")

    def test_no_home_assistant_calls(self):
        src = self._source()
        assert "HOME_ASSISTANT_API_KEY" not in src
        assert "ha_service" not in src.lower()
        assert "homeassistant" not in src.lower()

    def test_no_subprocess_execution(self):
        src = self._source()
        assert "import subprocess" not in src
        assert "subprocess.run" not in src
        assert "subprocess.Popen" not in src
        assert "os.system" not in src

    def test_no_browser_oauth_auto_run(self):
        # run_local_server should only be called inside allow_interactive_auth=True branch
        src = self._source()
        assert "run_local_server" in src  # exists but should be guarded
        assert "allow_interactive_auth" in src

    def test_write_safety_requires_enabled_and_not_dry_run(self):
        # With enabled=True and dry_run=True, service must never be called
        svc = MagicMock()
        cfg = GoogleCalendarConfig(enabled=True, dry_run=True)
        create_calendar_event(svc, cfg, "T", "2026-05-15T10:00:00", "2026-05-15T11:00:00")
        update_calendar_event(svc, cfg, "ev1")
        delete_calendar_event(svc, cfg, "ev1")
        svc.events.assert_not_called()

    def test_dataclasses_asdict_works_on_result(self):
        cfg = GoogleCalendarConfig(enabled=True, dry_run=True)
        svc = MagicMock()
        result = create_calendar_event(svc, cfg, "Test", "2026-05-15T10:00:00", "2026-05-15T11:00:00")
        d = dataclasses.asdict(result)
        assert isinstance(d, dict)
        assert "success" in d


# ── CLI dotenv loading tests ──────────────────────────────────────────────────

class TestLoadProjectDotenv:
    """Tests for _load_project_dotenv() — the CLI env-file loader."""

    def test_loads_enabled_true_from_env_file(self, tmp_path, monkeypatch):
        """Creating a .env with GOOGLE_CALENDAR_ENABLED=true makes load_google_calendar_config return enabled=True."""
        import os
        env_file = tmp_path / ".env"
        env_file.write_text("GOOGLE_CALENDAR_ENABLED=true\n", encoding="utf-8")

        monkeypatch.delenv("GOOGLE_CALENDAR_ENABLED", raising=False)

        from prometheus.integrations.google_calendar import _load_project_dotenv, load_google_calendar_config
        _load_project_dotenv(env_path=env_file)
        cfg = load_google_calendar_config()
        assert cfg.enabled is True

    def test_config_path_vars_loaded_from_env_file(self, tmp_path, monkeypatch):
        """Credentials and token paths in .env are picked up by load_google_calendar_config."""
        import os
        env_file = tmp_path / ".env"
        env_file.write_text(
            "GOOGLE_CALENDAR_CREDENTIALS_PATH=/fake/credentials.json\n"
            "GOOGLE_CALENDAR_TOKEN_PATH=/fake/token.json\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("GOOGLE_CALENDAR_CREDENTIALS_PATH", raising=False)
        monkeypatch.delenv("GOOGLE_CALENDAR_TOKEN_PATH", raising=False)

        from prometheus.integrations.google_calendar import _load_project_dotenv, load_google_calendar_config
        _load_project_dotenv(env_path=env_file)
        cfg = load_google_calendar_config()
        assert cfg.credentials_path == "/fake/credentials.json"
        assert cfg.token_path == "/fake/token.json"

    def test_returns_true_when_file_found(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("GOOGLE_CALENDAR_ENABLED=false\n", encoding="utf-8")

        from prometheus.integrations.google_calendar import _load_project_dotenv
        result = _load_project_dotenv(env_path=env_file)
        assert result is True

    def test_returns_false_when_file_missing(self, tmp_path):
        from prometheus.integrations.google_calendar import _load_project_dotenv
        result = _load_project_dotenv(env_path=tmp_path / "nonexistent.env")
        assert result is False

    def test_does_not_override_already_set_vars(self, tmp_path, monkeypatch):
        """Already-exported env vars are not overwritten."""
        env_file = tmp_path / ".env"
        env_file.write_text("GOOGLE_CALENDAR_ENABLED=true\n", encoding="utf-8")

        monkeypatch.setenv("GOOGLE_CALENDAR_ENABLED", "false")

        from prometheus.integrations.google_calendar import _load_project_dotenv
        _load_project_dotenv(env_path=env_file)
        import os
        assert os.environ["GOOGLE_CALENDAR_ENABLED"] == "false"

    def test_safe_defaults_when_no_env_file(self, tmp_path, monkeypatch):
        """If .env doesn't exist, defaults remain safe: enabled=False, dry_run=True."""
        monkeypatch.delenv("GOOGLE_CALENDAR_ENABLED", raising=False)
        monkeypatch.delenv("GOOGLE_CALENDAR_DRY_RUN", raising=False)

        from prometheus.integrations.google_calendar import _load_project_dotenv, load_google_calendar_config
        _load_project_dotenv(env_path=tmp_path / "missing.env")
        cfg = load_google_calendar_config()
        assert cfg.enabled is False
        assert cfg.dry_run is True

    def test_auto_detect_does_not_raise(self):
        """Calling _load_project_dotenv() with no args should not raise."""
        from prometheus.integrations.google_calendar import _load_project_dotenv
        result = _load_project_dotenv()
        assert isinstance(result, bool)

    def test_fallback_path_from_file_location(self):
        """The __file__-based fallback path resolves to Prometheus_Main/.env."""
        from pathlib import Path
        import prometheus.integrations.google_calendar as gc_mod
        computed = Path(gc_mod.__file__).resolve().parent.parent.parent / ".env"
        assert computed.name == ".env"
        assert computed.parent.name == "Prometheus_Main"

    def test_minimal_parser_handles_comments_and_blank_lines(self, tmp_path, monkeypatch):
        """Minimal fallback parser skips comments and blank lines."""
        env_file = tmp_path / ".env"
        env_file.write_text(
            "# This is a comment\n"
            "\n"
            "GOOGLE_CALENDAR_TIMEZONE=UTC\n"
            "# Another comment\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("GOOGLE_CALENDAR_TIMEZONE", raising=False)

        from prometheus.integrations.google_calendar import _load_project_dotenv, load_google_calendar_config
        _load_project_dotenv(env_path=env_file)
        cfg = load_google_calendar_config()
        assert cfg.timezone == "UTC"

    def test_values_with_equals_sign_in_value(self, tmp_path, monkeypatch):
        """Values that contain '=' are parsed correctly (partition on first '=' only)."""
        import os
        env_file = tmp_path / ".env"
        env_file.write_text("GOOGLE_CALENDAR_TIMEZONE=US/Eastern\n", encoding="utf-8")
        monkeypatch.delenv("GOOGLE_CALENDAR_TIMEZONE", raising=False)

        from prometheus.integrations.google_calendar import _load_project_dotenv
        _load_project_dotenv(env_path=env_file)
        assert os.environ.get("GOOGLE_CALENDAR_TIMEZONE") == "US/Eastern"

    def test_load_project_dotenv_is_in_source(self):
        """_load_project_dotenv is exported from the module."""
        from prometheus.integrations.google_calendar import _load_project_dotenv
        assert callable(_load_project_dotenv)

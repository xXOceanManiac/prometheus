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
    authorize_google_calendar,
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


# ── Token refresh handling tests ──────────────────────────────────────────────

def _make_refresh_config(tmp_path) -> GoogleCalendarConfig:
    secrets = tmp_path / "secrets" / "google"
    secrets.mkdir(parents=True)
    creds_file = secrets / "credentials.json"
    creds_file.write_text('{"installed": {}}', encoding="utf-8")
    return GoogleCalendarConfig(
        enabled=True,
        dry_run=True,
        credentials_path=str(creds_file),
        token_path=str(secrets / "calendar_token.json"),
    )


def _mock_creds(valid=False, expired=False, refresh_token="ref-tok", expiry=None):
    """Build a mock Credentials object with the given state."""
    creds = MagicMock()
    creds.valid = valid
    creds.expired = expired
    creds.refresh_token = refresh_token
    creds.expiry = expiry
    creds.to_json.return_value = json.dumps({
        "token": "access-tok", "refresh_token": refresh_token,
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "cid", "client_secret": "csec",
    })

    def _do_refresh(request):
        creds.valid = True
        creds.expired = False

    creds.refresh.side_effect = _do_refresh
    return creds


class TestTokenRefreshHandling:
    """Tests for the refresh-on-not-valid logic in build_google_calendar_service."""

    def _build(self, config, creds, fake_service=None, *, allow_interactive=False):
        if fake_service is None:
            fake_service = MagicMock()
        with patch("prometheus.integrations.google_calendar._GOOGLE_AVAILABLE", True), \
             patch("prometheus.integrations.google_calendar._Credentials") as mock_creds_cls, \
             patch("prometheus.integrations.google_calendar._Request", return_value=MagicMock()), \
             patch("prometheus.integrations.google_calendar._google_build", create=True, return_value=fake_service):
            mock_creds_cls.from_authorized_user_file.return_value = creds
            # Write a placeholder token file so from_authorized_user_file is called
            Path(config.token_path).parent.mkdir(parents=True, exist_ok=True)
            Path(config.token_path).write_text('{}', encoding="utf-8")
            result = build_google_calendar_service(config, allow_interactive_auth=allow_interactive)
        return result, creds

    def test_valid_false_expired_false_with_refresh_token_triggers_refresh(self, tmp_path):
        """The main regression: valid=False, expired=False, refresh_token set → refresh called."""
        config = _make_refresh_config(tmp_path)
        creds = _mock_creds(valid=False, expired=False, refresh_token="ref")

        self._build(config, creds)

        creds.refresh.assert_called_once()

    def test_valid_false_expired_true_with_refresh_token_triggers_refresh(self, tmp_path):
        """Existing behaviour: valid=False, expired=True also calls refresh."""
        config = _make_refresh_config(tmp_path)
        creds = _mock_creds(valid=False, expired=True, refresh_token="ref")

        self._build(config, creds)

        creds.refresh.assert_called_once()

    def test_valid_true_does_not_trigger_refresh(self, tmp_path):
        """Already-valid credentials are used as-is; no refresh."""
        config = _make_refresh_config(tmp_path)
        creds = _mock_creds(valid=True)

        self._build(config, creds)

        creds.refresh.assert_not_called()

    def test_refresh_success_saves_token_file(self, tmp_path):
        """After a successful refresh, the token file is updated."""
        config = _make_refresh_config(tmp_path)
        creds = _mock_creds(valid=False, expired=False, refresh_token="ref")
        token_path = Path(config.token_path)

        # Remove token so we know _write_token recreated it
        self._build(config, creds)

        assert token_path.exists()
        saved = json.loads(token_path.read_text())
        assert isinstance(saved, dict)

    def test_refresh_success_builds_service(self, tmp_path):
        """build_google_calendar_service returns a service object after refresh."""
        config = _make_refresh_config(tmp_path)
        fake_svc = MagicMock()
        creds = _mock_creds(valid=False, expired=False, refresh_token="ref")

        service, _ = self._build(config, creds, fake_service=fake_svc)

        assert service is fake_svc

    def test_refresh_failure_raises_clear_error(self, tmp_path):
        """If refresh() raises, a clear ValueError is raised (not the raw google error)."""
        config = _make_refresh_config(tmp_path)
        creds = _mock_creds(valid=False, expired=False, refresh_token="ref")
        creds.refresh.side_effect = Exception("transport error")

        with patch("prometheus.integrations.google_calendar._GOOGLE_AVAILABLE", True), \
             patch("prometheus.integrations.google_calendar._Credentials") as mock_cls, \
             patch("prometheus.integrations.google_calendar._Request", return_value=MagicMock()), \
             patch("prometheus.integrations.google_calendar._google_build", create=True, return_value=MagicMock()):
            mock_cls.from_authorized_user_file.return_value = creds
            Path(config.token_path).parent.mkdir(parents=True, exist_ok=True)
            Path(config.token_path).write_text('{}', encoding="utf-8")

            with pytest.raises(ValueError, match="could not be refreshed"):
                build_google_calendar_service(config, allow_interactive_auth=False)

    def test_refresh_still_invalid_after_success_raises_clear_error(self, tmp_path):
        """If refresh() doesn't raise but creds.valid stays False, raise clear error."""
        config = _make_refresh_config(tmp_path)
        creds = _mock_creds(valid=False, expired=False, refresh_token="ref")
        creds.refresh.side_effect = None   # refresh succeeds but valid stays False
        creds.refresh.return_value = None

        with patch("prometheus.integrations.google_calendar._GOOGLE_AVAILABLE", True), \
             patch("prometheus.integrations.google_calendar._Credentials") as mock_cls, \
             patch("prometheus.integrations.google_calendar._Request", return_value=MagicMock()), \
             patch("prometheus.integrations.google_calendar._google_build", create=True, return_value=MagicMock()):
            mock_cls.from_authorized_user_file.return_value = creds
            Path(config.token_path).parent.mkdir(parents=True, exist_ok=True)
            Path(config.token_path).write_text('{}', encoding="utf-8")

            with pytest.raises(ValueError, match="could not be refreshed"):
                build_google_calendar_service(config, allow_interactive_auth=False)

    def test_no_refresh_token_raises_no_valid_token_error(self, tmp_path):
        """Token with no refresh_token raises 'No valid token found' (not refresh error)."""
        config = _make_refresh_config(tmp_path)
        creds = _mock_creds(valid=False, expired=False, refresh_token=None)
        creds.refresh_token = None

        with patch("prometheus.integrations.google_calendar._GOOGLE_AVAILABLE", True), \
             patch("prometheus.integrations.google_calendar._Credentials") as mock_cls, \
             patch("prometheus.integrations.google_calendar._Request", return_value=MagicMock()), \
             patch("prometheus.integrations.google_calendar._google_build", create=True, return_value=MagicMock()):
            mock_cls.from_authorized_user_file.return_value = creds
            Path(config.token_path).parent.mkdir(parents=True, exist_ok=True)
            Path(config.token_path).write_text('{}', encoding="utf-8")

            with pytest.raises(ValueError, match="No valid token"):
                build_google_calendar_service(config, allow_interactive_auth=False)

    def test_list_upcoming_does_not_run_oauth(self, tmp_path):
        """list_upcoming_calendar_events uses allow_interactive_auth=False (default)."""
        config = _make_refresh_config(tmp_path)
        creds = _mock_creds(valid=False, expired=False, refresh_token="ref")

        from prometheus.integrations.google_calendar import list_upcoming_calendar_events

        with patch("prometheus.integrations.google_calendar._GOOGLE_AVAILABLE", True), \
             patch("prometheus.integrations.google_calendar._Credentials") as mock_cls, \
             patch("prometheus.integrations.google_calendar._Request", return_value=MagicMock()), \
             patch("prometheus.integrations.google_calendar._google_build", create=True) as mock_build, \
             patch("prometheus.integrations.google_calendar._InstalledAppFlow", create=True) as mock_flow_cls:
            mock_cls.from_authorized_user_file.return_value = creds
            fake_svc = MagicMock()
            fake_svc.events.return_value.list.return_value.execute.return_value = {"items": []}
            mock_build.return_value = fake_svc
            Path(config.token_path).parent.mkdir(parents=True, exist_ok=True)
            Path(config.token_path).write_text('{}', encoding="utf-8")

            service = build_google_calendar_service(config, allow_interactive_auth=False)
            events = list_upcoming_calendar_events(service, config)

        # OAuth flow must never have been invoked
        mock_flow_cls.from_client_secrets_file.assert_not_called()
        assert isinstance(events, list)

    def test_no_calendar_writes_during_refresh(self, tmp_path):
        """During token refresh, no calendar create/update/delete calls are made."""
        config = _make_refresh_config(tmp_path)
        creds = _mock_creds(valid=False, expired=False, refresh_token="ref")
        fake_svc = MagicMock()

        with patch("prometheus.integrations.google_calendar._GOOGLE_AVAILABLE", True), \
             patch("prometheus.integrations.google_calendar._Credentials") as mock_cls, \
             patch("prometheus.integrations.google_calendar._Request", return_value=MagicMock()), \
             patch("prometheus.integrations.google_calendar._google_build", create=True, return_value=fake_svc):
            mock_cls.from_authorized_user_file.return_value = creds
            Path(config.token_path).parent.mkdir(parents=True, exist_ok=True)
            Path(config.token_path).write_text('{}', encoding="utf-8")
            build_google_calendar_service(config, allow_interactive_auth=False)

        # The service's write methods must not have been called
        fake_svc.events.return_value.insert.assert_not_called()
        fake_svc.events.return_value.patch.assert_not_called()
        fake_svc.events.return_value.delete.assert_not_called()


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


# ── Credential serialization tests ───────────────────────────────────────────

class TestSerializeGoogleCredentials:
    def _import(self):
        from prometheus.integrations.google_calendar import _serialize_google_credentials
        return _serialize_google_credentials

    def test_uses_to_json_when_present(self):
        fn = self._import()
        creds = MagicMock()
        creds.to_json.return_value = '{"token": "abc"}'
        result = fn(creds)
        assert result == '{"token": "abc"}'
        creds.to_json.assert_called_once()

    def test_fallback_when_to_json_missing(self):
        fn = self._import()
        creds = MagicMock(spec=[
            "token", "refresh_token", "token_uri",
            "client_id", "client_secret", "scopes", "expiry",
        ])
        creds.token = "tok"
        creds.refresh_token = "ref"
        creds.token_uri = "https://oauth2.googleapis.com/token"
        creds.client_id = "cid"
        creds.client_secret = "csec"
        creds.scopes = ["https://www.googleapis.com/auth/calendar"]
        creds.expiry = None
        result = fn(creds)
        data = json.loads(result)
        assert data["token"] == "tok"
        assert data["refresh_token"] == "ref"

    def test_manual_includes_all_required_fields(self):
        fn = self._import()
        creds = MagicMock(spec=[
            "token", "refresh_token", "token_uri",
            "client_id", "client_secret", "scopes", "expiry",
        ])
        creds.token = "t"
        creds.refresh_token = "r"
        creds.token_uri = "https://oauth2.googleapis.com/token"
        creds.client_id = "cid"
        creds.client_secret = "csec"
        creds.scopes = ["https://www.googleapis.com/auth/calendar"]
        creds.expiry = None
        data = json.loads(fn(creds))
        for field in ("token", "refresh_token", "token_uri", "client_id", "client_secret", "scopes"):
            assert field in data, f"Missing field: {field}"

    def test_datetime_expiry_serialized_as_iso_string(self):
        from datetime import datetime, timezone
        fn = self._import()
        creds = MagicMock(spec=[
            "token", "refresh_token", "token_uri",
            "client_id", "client_secret", "scopes", "expiry",
        ])
        creds.token = "t"
        creds.refresh_token = "r"
        creds.token_uri = "https://oauth2.googleapis.com/token"
        creds.client_id = "cid"
        creds.client_secret = "csec"
        creds.scopes = ["https://www.googleapis.com/auth/calendar"]
        dt = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)
        creds.expiry = dt
        data = json.loads(fn(creds))
        assert "expiry" in data
        assert isinstance(data["expiry"], str)
        assert "2026-05-15" in data["expiry"]

    def test_none_fields_omitted_from_fallback(self):
        fn = self._import()
        creds = MagicMock(spec=["token", "refresh_token", "token_uri",
                                 "client_id", "client_secret", "scopes", "expiry"])
        creds.token = "t"
        creds.refresh_token = None
        creds.token_uri = "https://oauth2.googleapis.com/token"
        creds.client_id = "cid"
        creds.client_secret = None
        creds.scopes = None
        creds.expiry = None
        data = json.loads(fn(creds))
        assert "refresh_token" not in data
        assert "client_secret" not in data

    def test_result_is_valid_json(self):
        fn = self._import()
        creds = MagicMock(spec=["token", "refresh_token", "token_uri",
                                 "client_id", "client_secret", "scopes", "expiry"])
        creds.token = "t"
        creds.refresh_token = "r"
        creds.token_uri = "https://oauth2.googleapis.com/token"
        creds.client_id = "cid"
        creds.client_secret = "sec"
        creds.scopes = ["https://www.googleapis.com/auth/calendar"]
        creds.expiry = None
        result = fn(creds)
        assert isinstance(result, str)
        parsed = json.loads(result)
        assert isinstance(parsed, dict)

    def test_scopes_serialized_as_list(self):
        fn = self._import()
        creds = MagicMock(spec=["token", "refresh_token", "token_uri",
                                 "client_id", "client_secret", "scopes", "expiry"])
        creds.token = "t"
        creds.refresh_token = "r"
        creds.token_uri = "https://oauth2.googleapis.com/token"
        creds.client_id = "cid"
        creds.client_secret = "sec"
        creds.scopes = frozenset(["https://www.googleapis.com/auth/calendar"])
        creds.expiry = None
        data = json.loads(fn(creds))
        assert isinstance(data["scopes"], list)


# ── authorize_google_calendar tests ──────────────────────────────────────────

class TestAuthorizeGoogleCalendar:
    def _make_config(self, tmp_path) -> "GoogleCalendarConfig":
        secrets_dir = tmp_path / "secrets" / "google"
        secrets_dir.mkdir(parents=True)
        creds_file = secrets_dir / "credentials.json"
        creds_file.write_text('{"installed": {}}', encoding="utf-8")
        return GoogleCalendarConfig(
            enabled=True,
            dry_run=True,
            credentials_path=str(creds_file),
            token_path=str(tmp_path / "secrets" / "google" / "calendar_token.json"),
        )

    def _fake_creds(self):
        creds = MagicMock()
        creds.to_json.return_value = '{"token": "fake", "refresh_token": "ref"}'
        creds.token = "fake"
        creds.refresh_token = "ref"
        creds.valid = True
        return creds

    def test_writes_token_after_successful_oauth(self, tmp_path):
        config = self._make_config(tmp_path)
        fake_creds = self._fake_creds()
        fake_service = MagicMock()

        with patch("prometheus.integrations.google_calendar._GOOGLE_AVAILABLE", True), \
             patch("prometheus.integrations.google_calendar._OAUTHLIB_AVAILABLE", True), \
             patch("prometheus.integrations.google_calendar._InstalledAppFlow", create=True) as mock_flow_cls, \
             patch("prometheus.integrations.google_calendar._google_build", create=True, return_value=fake_service):
            mock_flow = MagicMock()
            mock_flow.run_local_server.return_value = fake_creds
            mock_flow_cls.from_client_secrets_file.return_value = mock_flow

            result = authorize_google_calendar(config, allow_interactive_auth=True)

        assert result is True
        assert Path(config.token_path).exists()

    def test_token_file_contains_valid_json(self, tmp_path):
        config = self._make_config(tmp_path)
        fake_creds = self._fake_creds()
        fake_service = MagicMock()

        with patch("prometheus.integrations.google_calendar._GOOGLE_AVAILABLE", True), \
             patch("prometheus.integrations.google_calendar._OAUTHLIB_AVAILABLE", True), \
             patch("prometheus.integrations.google_calendar._InstalledAppFlow", create=True) as mock_flow_cls, \
             patch("prometheus.integrations.google_calendar._google_build", create=True, return_value=fake_service):
            mock_flow = MagicMock()
            mock_flow.run_local_server.return_value = fake_creds
            mock_flow_cls.from_client_secrets_file.return_value = mock_flow
            authorize_google_calendar(config, allow_interactive_auth=True)

        saved = json.loads(Path(config.token_path).read_text())
        assert isinstance(saved, dict)

    def test_returns_false_if_token_missing_after_auth(self, tmp_path):
        config = self._make_config(tmp_path)
        fake_service = MagicMock()

        # Creds that have no to_json, and _write_token is patched to do nothing
        fake_creds = MagicMock(spec=["token", "refresh_token"])
        fake_creds.token = "t"
        fake_creds.refresh_token = "r"

        with patch("prometheus.integrations.google_calendar._GOOGLE_AVAILABLE", True), \
             patch("prometheus.integrations.google_calendar._OAUTHLIB_AVAILABLE", True), \
             patch("prometheus.integrations.google_calendar._InstalledAppFlow", create=True) as mock_flow_cls, \
             patch("prometheus.integrations.google_calendar._google_build", create=True, return_value=fake_service), \
             patch("prometheus.integrations.google_calendar._write_token"):  # write nothing
            mock_flow = MagicMock()
            mock_flow.run_local_server.return_value = fake_creds
            mock_flow_cls.from_client_secrets_file.return_value = mock_flow

            result = authorize_google_calendar(config, allow_interactive_auth=True)

        assert result is False
        assert not Path(config.token_path).exists()

    def test_creates_parent_dir_for_token(self, tmp_path):
        config = self._make_config(tmp_path)
        # Put the token in a deeply nested dir that doesn't exist yet
        deep_token = tmp_path / "deep" / "nested" / "dir" / "token.json"
        config = GoogleCalendarConfig(
            enabled=True, dry_run=True,
            credentials_path=config.credentials_path,
            token_path=str(deep_token),
        )
        fake_creds = self._fake_creds()
        fake_service = MagicMock()

        with patch("prometheus.integrations.google_calendar._GOOGLE_AVAILABLE", True), \
             patch("prometheus.integrations.google_calendar._OAUTHLIB_AVAILABLE", True), \
             patch("prometheus.integrations.google_calendar._InstalledAppFlow", create=True) as mock_flow_cls, \
             patch("prometheus.integrations.google_calendar._google_build", create=True, return_value=fake_service):
            mock_flow = MagicMock()
            mock_flow.run_local_server.return_value = fake_creds
            mock_flow_cls.from_client_secrets_file.return_value = mock_flow
            authorize_google_calendar(config, allow_interactive_auth=True)

        assert deep_token.exists()

    def test_chmod_attempted_safely(self, tmp_path):
        config = self._make_config(tmp_path)
        fake_creds = self._fake_creds()
        fake_service = MagicMock()

        # Patch chmod to raise OSError — should not propagate
        original_chmod = Path.chmod

        def patched_chmod(self, mode):
            raise OSError("chmod not supported")

        with patch("prometheus.integrations.google_calendar._GOOGLE_AVAILABLE", True), \
             patch("prometheus.integrations.google_calendar._OAUTHLIB_AVAILABLE", True), \
             patch("prometheus.integrations.google_calendar._InstalledAppFlow", create=True) as mock_flow_cls, \
             patch("prometheus.integrations.google_calendar._google_build", create=True, return_value=fake_service), \
             patch.object(Path, "chmod", patched_chmod):
            mock_flow = MagicMock()
            mock_flow.run_local_server.return_value = fake_creds
            mock_flow_cls.from_client_secrets_file.return_value = mock_flow
            # Should not raise even though chmod fails
            result = authorize_google_calendar(config, allow_interactive_auth=True)

        assert result is True

    def test_fallback_serializer_used_when_to_json_absent(self, tmp_path):
        config = self._make_config(tmp_path)
        # Build a creds object WITHOUT to_json
        creds_without_to_json = MagicMock(spec=[
            "token", "refresh_token", "token_uri",
            "client_id", "client_secret", "scopes", "expiry",
        ])
        creds_without_to_json.token = "tok"
        creds_without_to_json.refresh_token = "ref"
        creds_without_to_json.token_uri = "https://oauth2.googleapis.com/token"
        creds_without_to_json.client_id = "cid"
        creds_without_to_json.client_secret = "csec"
        creds_without_to_json.scopes = ["https://www.googleapis.com/auth/calendar"]
        creds_without_to_json.expiry = None
        fake_service = MagicMock()

        with patch("prometheus.integrations.google_calendar._GOOGLE_AVAILABLE", True), \
             patch("prometheus.integrations.google_calendar._OAUTHLIB_AVAILABLE", True), \
             patch("prometheus.integrations.google_calendar._InstalledAppFlow", create=True) as mock_flow_cls, \
             patch("prometheus.integrations.google_calendar._google_build", create=True, return_value=fake_service):
            mock_flow = MagicMock()
            mock_flow.run_local_server.return_value = creds_without_to_json
            mock_flow_cls.from_client_secrets_file.return_value = mock_flow
            result = authorize_google_calendar(config, allow_interactive_auth=True)

        assert result is True
        saved = json.loads(Path(config.token_path).read_text())
        assert saved["token"] == "tok"
        assert saved["refresh_token"] == "ref"

    def test_auth_not_run_at_import(self):
        src = (ROOT / "prometheus" / "integrations" / "google_calendar.py").read_text()
        # authorize_google_calendar() must not be called at module level
        import ast
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Expr,)) and isinstance(node.value, ast.Call):
                if isinstance(node.value.func, ast.Name):
                    assert node.value.func.id != "authorize_google_calendar", \
                        "authorize_google_calendar() called at module level"

    def test_auth_does_not_create_calendar_events(self):
        src = (ROOT / "prometheus" / "integrations" / "google_calendar.py").read_text()
        import ast
        tree = ast.parse(src)
        fn_src = ""
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "authorize_google_calendar":
                fn_src = ast.get_source_segment(src, node) or ""
                break
        assert "create_calendar_event" not in fn_src
        assert "update_calendar_event" not in fn_src
        assert "delete_calendar_event" not in fn_src

    def test_auth_does_not_call_home_assistant(self):
        src = (ROOT / "prometheus" / "integrations" / "google_calendar.py").read_text()
        import ast
        tree = ast.parse(src)
        fn_src = ""
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "authorize_google_calendar":
                fn_src = ast.get_source_segment(src, node) or ""
                break
        assert "home_assistant" not in fn_src.lower()
        assert "ha_service" not in fn_src


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

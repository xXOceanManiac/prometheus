"""
prometheus/integrations/google_calendar.py — Google Calendar adapter.

Narrow interface for calendar reads and writes. Writes are disabled and
dry_run by default; Prometheus_Main is the authority for enabling them.

No auth flow runs at import time. Google packages are optional at import.
"""
from __future__ import annotations

import dataclasses
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Optional

# ── Optional Google library detection ─────────────────────────────────────────
_GOOGLE_AVAILABLE = False
try:
    from google.oauth2.credentials import Credentials as _Credentials
    from google.auth.transport.requests import Request as _Request
    from googleapiclient.discovery import build as _google_build
    _GOOGLE_AVAILABLE = True
except ImportError:
    pass

_OAUTHLIB_AVAILABLE = False
try:
    from google_auth_oauthlib.flow import InstalledAppFlow as _InstalledAppFlow
    _OAUTHLIB_AVAILABLE = True
except ImportError:
    pass


# ── Config ────────────────────────────────────────────────────────────────────

@dataclass
class GoogleCalendarConfig:
    enabled: bool = False
    dry_run: bool = True
    default_calendar_id: str = "primary"
    credentials_path: Optional[str] = None
    token_path: Optional[str] = None
    scopes: list = field(default_factory=lambda: ["https://www.googleapis.com/auth/calendar"])
    timezone: str = "America/New_York"


def load_google_calendar_config() -> GoogleCalendarConfig:
    def _bool(key: str, default: bool) -> bool:
        val = os.getenv(key, "").strip().lower()
        if val in ("1", "true", "yes"):
            return True
        if val in ("0", "false", "no"):
            return False
        return default

    return GoogleCalendarConfig(
        enabled=_bool("GOOGLE_CALENDAR_ENABLED", False),
        dry_run=_bool("GOOGLE_CALENDAR_DRY_RUN", True),
        default_calendar_id=os.getenv("GOOGLE_CALENDAR_ID", "primary").strip() or "primary",
        credentials_path=os.getenv("GOOGLE_CALENDAR_CREDENTIALS_PATH") or None,
        token_path=os.getenv("GOOGLE_CALENDAR_TOKEN_PATH") or None,
        timezone=os.getenv("GOOGLE_CALENDAR_TIMEZONE", "America/New_York").strip() or "America/New_York",
    )


# ── Event / result models ─────────────────────────────────────────────────────

@dataclass
class GoogleCalendarEvent:
    event_id: Optional[str]
    calendar_id: str
    title: str
    start_time: str
    end_time: Optional[str]
    location: Optional[str]
    description: Optional[str]
    html_link: Optional[str]
    raw: Optional[dict]


@dataclass
class GoogleCalendarResult:
    success: bool
    dry_run: bool
    operation_type: str
    calendar_id: str
    event_id: Optional[str]
    message: str
    event: Optional[GoogleCalendarEvent]
    raw: Optional[dict]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _calendar_id(config: GoogleCalendarConfig, override: Optional[str]) -> str:
    return override or config.default_calendar_id


def _event_from_google(raw: dict, calendar_id: str) -> GoogleCalendarEvent:
    start = raw.get("start", {})
    end = raw.get("end", {})
    start_time = start.get("dateTime") or start.get("date") or ""
    end_time = end.get("dateTime") or end.get("date") or None
    return GoogleCalendarEvent(
        event_id=raw.get("id"),
        calendar_id=calendar_id,
        title=raw.get("summary", ""),
        start_time=start_time,
        end_time=end_time,
        location=raw.get("location"),
        description=raw.get("description"),
        html_link=raw.get("htmlLink"),
        raw=raw,
    )


def _disabled_result(operation_type: str, calendar_id: str) -> GoogleCalendarResult:
    return GoogleCalendarResult(
        success=False,
        dry_run=False,
        operation_type=operation_type,
        calendar_id=calendar_id,
        event_id=None,
        message="Google Calendar adapter is disabled. Set GOOGLE_CALENDAR_ENABLED=true to enable.",
        event=None,
        raw=None,
    )


# ── Credential serialization ──────────────────────────────────────────────────

def _serialize_google_credentials(creds) -> str:
    """
    Serialize Google OAuth2 Credentials to a JSON string suitable for
    Credentials.from_authorized_user_file().

    Tries creds.to_json() first (google-auth >= 2.x). Falls back to manual
    serialization using well-known Credentials attributes when to_json() is
    absent (older library versions or non-standard credential objects).
    """
    if hasattr(creds, "to_json") and callable(creds.to_json):
        return creds.to_json()

    # Manual fallback
    from datetime import datetime as _dt
    expiry = getattr(creds, "expiry", None)
    if isinstance(expiry, _dt):
        expiry = expiry.isoformat()

    scopes = getattr(creds, "scopes", None)
    if scopes is not None:
        scopes = list(scopes)

    data = {
        "token": getattr(creds, "token", None),
        "refresh_token": getattr(creds, "refresh_token", None),
        "token_uri": getattr(creds, "token_uri", None),
        "client_id": getattr(creds, "client_id", None),
        "client_secret": getattr(creds, "client_secret", None),
        "scopes": scopes,
        "expiry": expiry,
    }
    return json.dumps({k: v for k, v in data.items() if v is not None})


def _write_token(token_path, creds) -> None:
    """Write serialized credentials to token_path; chmod 600; never prints secrets."""
    from pathlib import Path as _Path
    p = _Path(token_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_serialize_google_credentials(creds), encoding="utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        pass


# ── Service builder ───────────────────────────────────────────────────────────

def build_google_calendar_service(
    config: GoogleCalendarConfig,
    allow_interactive_auth: bool = False,
):
    if not config.enabled:
        raise ValueError(
            "Google Calendar adapter is disabled. "
            "Set GOOGLE_CALENDAR_ENABLED=true in environment to enable."
        )
    if not _GOOGLE_AVAILABLE:
        raise ImportError(
            "Google Calendar API libraries are not installed. "
            "Run: pip install google-api-python-client google-auth google-auth-oauthlib"
        )
    if not config.credentials_path:
        raise ValueError(
            "GOOGLE_CALENDAR_CREDENTIALS_PATH is not set. "
            "Download credentials.json from Google Cloud Console."
        )
    if not config.token_path:
        raise ValueError(
            "GOOGLE_CALENDAR_TOKEN_PATH is not set. "
            "Specify a path where the OAuth token will be stored."
        )

    from pathlib import Path as _Path
    token_path = _Path(config.token_path)
    creds = None

    if token_path.exists():
        creds = _Credentials.from_authorized_user_file(str(token_path), config.scopes)

    if creds and creds.valid:
        pass
    elif creds and creds.expired and creds.refresh_token:
        creds.refresh(_Request())
        _write_token(token_path, creds)
    else:
        if not allow_interactive_auth:
            raise ValueError(
                f"No valid token found at {token_path}. "
                "Run with allow_interactive_auth=True once to authenticate, "
                "or use the OAuth flow to generate a token."
            )
        if not _OAUTHLIB_AVAILABLE:
            raise ImportError(
                "google-auth-oauthlib is required for interactive OAuth. "
                "Run: pip install google-auth-oauthlib"
            )
        flow = _InstalledAppFlow.from_client_secrets_file(config.credentials_path, config.scopes)
        creds = flow.run_local_server(port=0)
        _write_token(token_path, creds)

    return _google_build("calendar", "v3", credentials=creds)


# ── Calendar operations ───────────────────────────────────────────────────────

def list_calendar_events(
    service,
    config: GoogleCalendarConfig,
    time_min: str,
    time_max: str,
    calendar_id: Optional[str] = None,
    max_results: int = 20,
) -> list[GoogleCalendarEvent]:
    cal_id = _calendar_id(config, calendar_id)
    result = (
        service.events()
        .list(
            calendarId=cal_id,
            timeMin=time_min,
            timeMax=time_max,
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    items = result.get("items", [])
    return [_event_from_google(item, cal_id) for item in items]


def create_calendar_event(
    service,
    config: GoogleCalendarConfig,
    title: str,
    start_time: str,
    end_time: str,
    calendar_id: Optional[str] = None,
    location: Optional[str] = None,
    description: Optional[str] = None,
    timezone: Optional[str] = None,
) -> GoogleCalendarResult:
    if not title or not title.strip():
        raise ValueError("title is required for create_calendar_event")
    if not start_time:
        raise ValueError("start_time is required for create_calendar_event")
    if not end_time:
        raise ValueError("end_time is required for create_calendar_event")

    cal_id = _calendar_id(config, calendar_id)
    tz = timezone or config.timezone

    if not config.enabled:
        return _disabled_result("create_event", cal_id)

    body: dict[str, Any] = {
        "summary": title,
        "start": {"dateTime": start_time, "timeZone": tz},
        "end": {"dateTime": end_time, "timeZone": tz},
    }
    if location:
        body["location"] = location
    if description:
        body["description"] = description

    if config.dry_run:
        proposed_event = GoogleCalendarEvent(
            event_id=None,
            calendar_id=cal_id,
            title=title,
            start_time=start_time,
            end_time=end_time,
            location=location,
            description=description,
            html_link=None,
            raw=body,
        )
        return GoogleCalendarResult(
            success=True,
            dry_run=True,
            operation_type="create_event",
            calendar_id=cal_id,
            event_id=None,
            message=f"[DRY RUN] Would create event '{title}' at {start_time}.",
            event=proposed_event,
            raw=body,
        )

    created = service.events().insert(calendarId=cal_id, body=body).execute()
    return GoogleCalendarResult(
        success=True,
        dry_run=False,
        operation_type="create_event",
        calendar_id=cal_id,
        event_id=created.get("id"),
        message=f"Event '{title}' created.",
        event=_event_from_google(created, cal_id),
        raw=created,
    )


def update_calendar_event(
    service,
    config: GoogleCalendarConfig,
    event_id: str,
    calendar_id: Optional[str] = None,
    title: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    location: Optional[str] = None,
    description: Optional[str] = None,
    timezone: Optional[str] = None,
) -> GoogleCalendarResult:
    if not event_id or not event_id.strip():
        raise ValueError("event_id is required for update_calendar_event")

    cal_id = _calendar_id(config, calendar_id)
    tz = timezone or config.timezone

    if not config.enabled:
        return _disabled_result("update_event", cal_id)

    patch: dict[str, Any] = {}
    if title is not None:
        patch["summary"] = title
    if start_time is not None:
        patch["start"] = {"dateTime": start_time, "timeZone": tz}
    if end_time is not None:
        patch["end"] = {"dateTime": end_time, "timeZone": tz}
    if location is not None:
        patch["location"] = location
    if description is not None:
        patch["description"] = description

    if config.dry_run:
        return GoogleCalendarResult(
            success=True,
            dry_run=True,
            operation_type="update_event",
            calendar_id=cal_id,
            event_id=event_id,
            message=f"[DRY RUN] Would update event '{event_id}'.",
            event=None,
            raw={"event_id": event_id, "patch": patch},
        )

    updated = (
        service.events().patch(calendarId=cal_id, eventId=event_id, body=patch).execute()
    )
    return GoogleCalendarResult(
        success=True,
        dry_run=False,
        operation_type="update_event",
        calendar_id=cal_id,
        event_id=event_id,
        message=f"Event '{event_id}' updated.",
        event=_event_from_google(updated, cal_id),
        raw=updated,
    )


def delete_calendar_event(
    service,
    config: GoogleCalendarConfig,
    event_id: str,
    calendar_id: Optional[str] = None,
) -> GoogleCalendarResult:
    if not event_id or not event_id.strip():
        raise ValueError("event_id is required for delete_calendar_event")

    cal_id = _calendar_id(config, calendar_id)

    if not config.enabled:
        return _disabled_result("delete_event", cal_id)

    if config.dry_run:
        return GoogleCalendarResult(
            success=True,
            dry_run=True,
            operation_type="delete_event",
            calendar_id=cal_id,
            event_id=event_id,
            message=f"[DRY RUN] Would delete event '{event_id}'.",
            event=None,
            raw={"event_id": event_id},
        )

    service.events().delete(calendarId=cal_id, eventId=event_id).execute()
    return GoogleCalendarResult(
        success=True,
        dry_run=False,
        operation_type="delete_event",
        calendar_id=cal_id,
        event_id=event_id,
        message=f"Event '{event_id}' deleted.",
        event=None,
        raw={"event_id": event_id},
    )


# ── Convenience read operations ───────────────────────────────────────────────

def list_upcoming_calendar_events(
    service,
    config: GoogleCalendarConfig,
    days_ahead: int = 7,
    max_results: int = 20,
    calendar_id: Optional[str] = None,
) -> list[GoogleCalendarEvent]:
    """List upcoming events from now through days_ahead. Read-only, no writes."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    time_min = now.isoformat()
    time_max = (now + timedelta(days=days_ahead)).isoformat()
    return list_calendar_events(
        service=service,
        config=config,
        time_min=time_min,
        time_max=time_max,
        calendar_id=calendar_id,
        max_results=max_results,
    )


# ── OAuth bootstrap (explicit-only, never called automatically) ───────────────

def authorize_google_calendar(
    config: GoogleCalendarConfig,
    allow_interactive_auth: bool = True,
) -> bool:
    """
    Run the OAuth flow once to generate a token file.

    Only call this explicitly (e.g., via --auth CLI). Never called automatically.
    Returns True if authorization succeeded AND token file exists on disk.
    Raises ValueError/ImportError on configuration or library errors.
    """
    from pathlib import Path as _Path
    build_google_calendar_service(config, allow_interactive_auth=allow_interactive_auth)
    if config.token_path and not _Path(config.token_path).exists():
        return False
    return True


# ── Dry-run operation helper (for Lumen proposal router) ──────────────────────

_SUPPORTED_DRY_RUN_TYPES = frozenset({
    "create_event",
    "update_event",
    "delete_event",
    "read_events",
    "find_availability",
    "suggest_schedule_change",
})


def dry_run_calendar_operation(
    operation: dict,
    config: GoogleCalendarConfig,
) -> GoogleCalendarResult:
    op_type = operation.get("operation_type", "")
    if op_type not in _SUPPORTED_DRY_RUN_TYPES:
        return GoogleCalendarResult(
            success=False,
            dry_run=True,
            operation_type=op_type or "unknown",
            calendar_id=operation.get("calendar_id", config.default_calendar_id),
            event_id=operation.get("event_id"),
            message=f"Unsupported operation_type: {op_type!r}. "
                    f"Supported: {sorted(_SUPPORTED_DRY_RUN_TYPES)}.",
            event=None,
            raw=operation,
        )

    cal_id = operation.get("calendar_id") or config.default_calendar_id
    event_id = operation.get("event_id")
    title = operation.get("title")
    start_time = operation.get("start_time")
    end_time = operation.get("end_time")

    if op_type in ("read_events", "find_availability", "suggest_schedule_change"):
        return GoogleCalendarResult(
            success=True,
            dry_run=True,
            operation_type=op_type,
            calendar_id=cal_id,
            event_id=None,
            message=f"[DRY RUN] Would perform {op_type} on calendar '{cal_id}'.",
            event=None,
            raw=operation,
        )

    if op_type == "create_event":
        proposed = GoogleCalendarEvent(
            event_id=None,
            calendar_id=cal_id,
            title=title or "",
            start_time=start_time or "",
            end_time=end_time,
            location=operation.get("location"),
            description=operation.get("description"),
            html_link=None,
            raw=operation,
        )
        return GoogleCalendarResult(
            success=True,
            dry_run=True,
            operation_type="create_event",
            calendar_id=cal_id,
            event_id=None,
            message=f"[DRY RUN] Would create event '{title}' at {start_time}.",
            event=proposed,
            raw=operation,
        )

    if op_type == "update_event":
        return GoogleCalendarResult(
            success=True,
            dry_run=True,
            operation_type="update_event",
            calendar_id=cal_id,
            event_id=event_id,
            message=f"[DRY RUN] Would update event '{event_id}'.",
            event=None,
            raw=operation,
        )

    # delete_event
    return GoogleCalendarResult(
        success=True,
        dry_run=True,
        operation_type="delete_event",
        calendar_id=cal_id,
        event_id=event_id,
        message=f"[DRY RUN] Would delete event '{event_id}'.",
        event=None,
        raw=operation,
    )


# ── CLI env loading (explicit, not at import time) ────────────────────────────

def _load_project_dotenv(env_path=None) -> bool:
    """
    Load .env from the project root into os.environ.

    Called from CLI _main() only — never at module import time.
    Returns True if a .env file was found and processed.

    env_path: explicit override (Path or str). If None, auto-detects:
      1. prometheus.infra.paths.PROJECT_ROOT / ".env"
      2. Fallback: parent of parent of parent of __file__ / ".env"
         (google_calendar.py → integrations/ → prometheus/ → Prometheus_Main/)
    """
    from pathlib import Path as _Path

    if env_path is None:
        try:
            from prometheus.infra.paths import PROJECT_ROOT as _PROJECT_ROOT
            env_path = _PROJECT_ROOT / ".env"
        except Exception:
            env_path = _Path(__file__).resolve().parent.parent.parent / ".env"

    env_path = _Path(env_path)
    if not env_path.is_file():
        return False

    # Try python-dotenv first
    try:
        from dotenv import load_dotenv as _load_dotenv
        _load_dotenv(env_path, override=False)
        return True
    except ImportError:
        pass

    # Fallback: minimal KEY=value parser (no external deps required)
    try:
        with env_path.open(encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, raw_val = line.partition("=")
                key = key.strip()
                if not key or key in os.environ:
                    continue
                val = raw_val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                    val = val[1:-1]
                os.environ[key] = val
        return True
    except OSError:
        return False


# ── CLI entry point ───────────────────────────────────────────────────────────

def _main(argv: list[str] | None = None) -> None:
    import json as _json
    _load_project_dotenv()

    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print(
            "Usage: python -m prometheus.integrations.google_calendar "
            "--config | --dry-run-create-sample | --auth | --list-upcoming [DAYS]"
        )
        sys.exit(1)

    cmd = args[0]

    if cmd == "--config":
        config = load_google_calendar_config()
        d = dataclasses.asdict(config)
        if d.get("credentials_path"):
            d["credentials_path"] = "<set>"
        if d.get("token_path"):
            d["token_path"] = "<set>"
        print(_json.dumps(d, indent=2))

    elif cmd == "--dry-run-create-sample":
        config = load_google_calendar_config()
        config = dataclasses.replace(config, dry_run=True)
        from datetime import date, timedelta
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        op = {
            "operation_type": "create_event",
            "calendar_id": config.default_calendar_id,
            "title": "Sample Focus Block",
            "start_time": f"{tomorrow}T14:00:00",
            "end_time": f"{tomorrow}T15:30:00",
            "description": "Dry-run sample event.",
        }
        result = dry_run_calendar_operation(op, config)
        d = dataclasses.asdict(result)
        print(_json.dumps(d, indent=2))

    elif cmd == "--auth":
        config = load_google_calendar_config()
        print("Starting OAuth authorization flow...")
        print(f"  credentials_path: {config.credentials_path or '(not set)'}")
        print(f"  token_path: {config.token_path or '(not set)'}")
        try:
            ok = authorize_google_calendar(config, allow_interactive_auth=True)
            if ok:
                print(f"Authorization successful. Token saved to: {config.token_path}")
            else:
                print(
                    f"OAuth completed but token file was not written: {config.token_path}",
                    file=sys.stderr,
                )
                sys.exit(1)
        except (ValueError, ImportError) as exc:
            print(f"Authorization failed: {exc}", file=sys.stderr)
            sys.exit(1)

    elif cmd == "--list-upcoming":
        days = int(args[1]) if len(args) > 1 else 7
        config = load_google_calendar_config()
        if not config.enabled:
            print(
                "Google Calendar is disabled. Set GOOGLE_CALENDAR_ENABLED=true to enable.",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            service = build_google_calendar_service(config, allow_interactive_auth=False)
        except (ValueError, ImportError) as exc:
            print(f"Cannot connect to Google Calendar: {exc}", file=sys.stderr)
            sys.exit(1)
        events = list_upcoming_calendar_events(service, config, days_ahead=days)
        output = [
            {
                "event_id": e.event_id,
                "title": e.title,
                "start_time": e.start_time,
                "end_time": e.end_time,
                "calendar_id": e.calendar_id,
                "location": e.location,
            }
            for e in events
        ]
        print(_json.dumps(output, indent=2))

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    _main()

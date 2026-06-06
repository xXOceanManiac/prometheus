"""
prometheus/services/readonly_dashboard.py

Read-only HTTP server for remote state view (e.g. second laptop).

Reads from canonical dashboard state path:
  ~/Desktop/PROMETHEUS/state/dashboard_state.json

Endpoints (GET only):
  /health  — {"status": "ok", "version": "1"}
  /state   — full dashboard_state.json content, secrets redacted
  /news    — cards.news section only
  /        — rich read-only HTML showing real state/news/activity data

Security:
  - GET only. POST/PUT/DELETE return 405.
  - No command, voice, HA, or chat endpoints.
  - Secrets redacted from all responses.

Config:
  PROMETHEUS_READONLY_DASHBOARD_ENABLED=true
  PROMETHEUS_READONLY_DASHBOARD_HOST=0.0.0.0
  PROMETHEUS_READONLY_DASHBOARD_PORT=8765
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

# ── Canonical state path ─────────────────────────────────────────────────────
_DASHBOARD_STATE_PATH = Path.home() / "Desktop" / "PROMETHEUS" / "state" / "dashboard_state.json"

_SECRET_KEY_PATTERNS = (
    "api_key", "apikey", "token", "secret", "password", "passwd",
    "openai", "guardian", "home_assistant", "porcupine", "credential",
    "auth", "bearer",
)


def _read_dashboard_state() -> dict:
    try:
        if _DASHBOARD_STATE_PATH.exists():
            return json.loads(_DASHBOARD_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _redact(obj: Any, depth: int = 0) -> Any:
    if depth > 12:
        return obj
    if isinstance(obj, dict):
        return {
            k: "[REDACTED]" if any(p in str(k).lower() for p in _SECRET_KEY_PATTERNS)
            else _redact(v, depth + 1)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(item, depth + 1) for item in obj]
    return obj


def _json_response(data: Any) -> bytes:
    return json.dumps(_redact(data), indent=2, ensure_ascii=False).encode("utf-8")


def _build_html(state: dict) -> str:
    """Build a rich read-only HTML status page from dashboard state."""
    godot_state = str(state.get("state", "idle")).upper()
    project = str(state.get("active_project", "—"))
    updated = str(state.get("updated_at", ""))[:19].replace("T", " ")
    cards = state.get("cards") or {}

    # News section
    news = cards.get("news") or {}
    news_status = str(news.get("status", "demo"))
    news_chip = str(news.get("chip", "DEMO"))
    articles = news.get("articles") or []

    news_rows = ""
    for a in articles[:9]:
        if not isinstance(a, dict):
            continue
        title = str(a.get("title") or "").replace("<", "&lt;").replace(">", "&gt;")
        tag = str(a.get("section") or a.get("tag") or "News")
        time_ago = str(a.get("time_ago") or "")
        summary = str(a.get("summary") or "").replace("<", "&lt;").replace(">", "&gt;")
        href = str(a.get("href") or a.get("url") or "#")
        news_rows += f"""
      <div class="nitem">
        <div class="nmeta"><span class="npill">{tag}</span> <span class="ndim">{time_ago}</span></div>
        <a class="ntitle" href="{href}" target="_blank" rel="noopener">{title}</a>
        <div class="nsummary">{summary}</div>
      </div>"""

    if not news_rows:
        news_rows = '<div class="ndim" style="padding:8px">No articles yet — Guardian feed loading…</div>'

    # Activity section
    activity = cards.get("activity") or {}
    act_items = activity.get("items") or []
    act_rows = "".join(
        f'<li>{str(i).replace("<","&lt;").replace(">","&gt;")}</li>'
        for i in act_items[:6]
    ) or "<li>No recent activity</li>"

    # Tasks section
    tasks = cards.get("tasks") or {}
    task_items = tasks.get("items") or []
    task_rows = "".join(
        f'<li>{str(i).replace("<","&lt;").replace(">","&gt;")}</li>'
        for i in task_items[:5]
    ) or "<li>No active tasks</li>"

    # Objective section
    objective = cards.get("objective") or {}
    obj_summary = str(objective.get("summary") or "No active mission.").replace("<", "&lt;").replace(">", "&gt;")
    obj_items = objective.get("items") or []
    obj_rows = "".join(
        f'<li>{str(i).replace("<","&lt;").replace(">","&gt;")}</li>'
        for i in obj_items[:4]
    )

    state_color = {
        "IDLE": "#c8902a", "LISTENING": "#f0bc55", "PROCESSING": "#4da8d8",
        "SPEAKING": "#f09a35", "EXECUTING": "#3cc89e", "WARNING": "#da4848",
    }.get(godot_state, "#c8902a")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Prometheus — Remote Dashboard</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:system-ui,sans-serif;background:#080a0d;color:#ddd;padding:20px 24px}}
h1{{color:#c8902a;font-size:1.4em;margin-bottom:2px}}
.subtitle{{color:#555;font-size:.85em;margin-bottom:20px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px;max-width:1200px}}
@media(max-width:700px){{.grid{{grid-template-columns:1fr}}}}
.card{{background:#11151c;border:1px solid #252b35;border-radius:10px;padding:16px 18px}}
.card h2{{font-size:.8em;letter-spacing:.12em;color:#888;text-transform:uppercase;margin-bottom:8px}}
.chip{{display:inline-block;font-size:.7em;font-weight:700;letter-spacing:.08em;
       padding:2px 8px;border-radius:5px;background:#1e2530;margin-left:6px;vertical-align:middle}}
.state{{font-size:1.6em;font-weight:700;color:{state_color};margin:4px 0 8px}}
.meta{{font-size:.78em;color:#555}}
ul{{list-style:none;padding:0}}
ul li{{padding:4px 0;border-bottom:1px solid #1a1e26;font-size:.85em;color:#bbb}}
ul li:last-child{{border:none}}
.nitem{{padding:10px 0;border-bottom:1px solid #1a1e26}}
.nitem:last-child{{border:none}}
.nmeta{{margin-bottom:3px}}
.npill{{font-size:.7em;font-weight:700;letter-spacing:.06em;background:#1a2540;
        color:#5a90d8;padding:2px 7px;border-radius:4px}}
.ndim{{font-size:.72em;color:#555;margin-left:6px}}
.ntitle{{font-size:.9em;color:#e8c878;text-decoration:none;display:block;margin-bottom:3px;font-weight:600}}
.ntitle:hover{{color:#f0d890;text-decoration:underline}}
.nsummary{{font-size:.78em;color:#888;line-height:1.4}}
.news-full{{grid-column:1/-1}}
.status-ok{{color:#3cc89e}}.status-demo{{color:#888}}.status-err{{color:#da4848}}
</style>
</head>
<body>
<h1>Prometheus <span class="chip">{godot_state}</span></h1>
<p class="subtitle">Read-only view · {updated} UTC · <a href="/state" style="color:#5a90d8">/state JSON</a></p>
<div class="grid">
  <div class="card">
    <h2>Status</h2>
    <div class="state">{godot_state}</div>
    <div class="meta">Project: {project}</div>
    <div class="meta">Updated: {updated}</div>
  </div>
  <div class="card">
    <h2>Mission</h2>
    <p style="font-size:.88em;color:#bbb;margin-bottom:8px">{obj_summary}</p>
    <ul>{obj_rows}</ul>
  </div>
  <div class="card">
    <h2>Activity</h2>
    <ul>{act_rows}</ul>
  </div>
  <div class="card">
    <h2>Tasks</h2>
    <ul>{task_rows}</ul>
  </div>
  <div class="card news-full">
    <h2>News <span class="chip">The Guardian</span>
      <span class="chip {'status-ok' if news_status=='live' else 'status-demo' if news_status in ('demo','fallback') else 'status-err'}">{news_chip}</span>
    </h2>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:0 24px">
      {news_rows}
    </div>
  </div>
</div>
<script>
// Auto-refresh every 15 seconds
setTimeout(()=>location.reload(), 15000);
</script>
</body>
</html>"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # suppress access log noise

    def _send(self, code: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?")[0].rstrip("/") or "/"

        if path == "/health":
            self._send(200, _json_response({"status": "ok", "version": "1"}))

        elif path == "/state":
            self._send(200, _json_response(_read_dashboard_state()))

        elif path == "/news":
            state = _read_dashboard_state()
            self._send(200, _json_response(state.get("cards", {}).get("news", {})))

        elif path == "/":
            state = _read_dashboard_state()
            html = _build_html(state)
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

        else:
            self._send(404, _json_response({"error": "not found"}))

    def do_POST(self) -> None:
        self._send(405, _json_response({"error": "read-only — no mutations allowed"}))

    def do_PUT(self) -> None:
        self._send(405, _json_response({"error": "read-only — no mutations allowed"}))

    def do_DELETE(self) -> None:
        self._send(405, _json_response({"error": "read-only — no mutations allowed"}))


class ReadonlyDashboard:
    """Minimal read-only HTTP server — runs in a daemon thread."""

    def __init__(self, host: str = "", port: int = 0) -> None:
        self._host = host or os.getenv("PROMETHEUS_READONLY_DASHBOARD_HOST", "0.0.0.0")
        self._port = port or int(os.getenv("PROMETHEUS_READONLY_DASHBOARD_PORT", "8765"))
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        try:
            self._server = HTTPServer((self._host, self._port), _Handler)
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                daemon=True,
                name="prometheus-readonly-dashboard",
            )
            self._thread.start()
            from utils import log_event
            log_event("readonly_dashboard_started", {
                "host": self._host,
                "port": self._port,
                "state_path": str(_DASHBOARD_STATE_PATH),
            })
            print(
                f"[READONLY_DASHBOARD] started on http://{self._host}:{self._port} "
                f"state_path={_DASHBOARD_STATE_PATH}",
                flush=True,
            )
        except Exception as exc:
            from utils import log_event
            log_event("readonly_dashboard_start_error", {"error": str(exc)[:200]})
            print(f"[READONLY_DASHBOARD] failed to start: {exc!r:.100}", flush=True)

    def stop(self) -> None:
        if self._server:
            try:
                self._server.shutdown()
            except Exception:
                pass

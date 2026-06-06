"""
prometheus/services/readonly_dashboard.py

Read-only HTTP server for remote state view (e.g. second laptop).

Endpoints (GET only):
  /health  — {"status": "ok", "version": "1"}
  /state   — full hud_state.json content, secrets redacted
  /news    — just the news section
  /        — simple read-only HTML status page

Security:
  - GET only. POST/PUT/DELETE return 405.
  - No command, voice, HA, or chat endpoints.
  - No secrets in responses (API keys, tokens, passwords are stripped).
  - Reads from disk on every request — no in-process state mutation.

Config (env vars):
  PROMETHEUS_READONLY_DASHBOARD_ENABLED=true   (default: false)
  PROMETHEUS_READONLY_DASHBOARD_HOST=0.0.0.0   (default: 0.0.0.0)
  PROMETHEUS_READONLY_DASHBOARD_PORT=8765      (default: 8765)
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

_HUD_STATE_PATH = Path.home() / ".prometheus" / "hud_state.json"

_SECRET_KEY_PATTERNS = (
    "api_key", "apikey", "token", "secret", "password", "passwd",
    "openai", "guardian", "home_assistant", "porcupine", "credential",
    "auth", "bearer",
)


def _read_hud_state() -> dict:
    """Read hud_state.json from disk. Returns {} on any failure."""
    try:
        if _HUD_STATE_PATH.exists():
            return json.loads(_HUD_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _redact(obj: Any, depth: int = 0) -> Any:
    """
    Recursively redact dict keys that look like secrets.
    Only processes dicts and lists — leaves scalars intact.
    """
    if depth > 12:
        return obj
    if isinstance(obj, dict):
        out: dict = {}
        for k, v in obj.items():
            k_lower = str(k).lower()
            if any(pattern in k_lower for pattern in _SECRET_KEY_PATTERNS):
                out[k] = "[REDACTED]"
            else:
                out[k] = _redact(v, depth + 1)
        return out
    if isinstance(obj, list):
        return [_redact(item, depth + 1) for item in obj]
    return obj


def _json_response(data: Any) -> bytes:
    return json.dumps(_redact(data), indent=2, ensure_ascii=False).encode("utf-8")


_HTML_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Prometheus — Read-only Dashboard</title>
<style>
body {{ font-family: system-ui, sans-serif; background: #0a0c0f; color: #ddd; margin: 0; padding: 24px; }}
h1 {{ color: #e8a030; margin-bottom: 4px; }}
p {{ color: #888; margin-top: 4px; }}
a {{ color: #6a9fd8; }}
.card {{ background: #13171e; border: 1px solid #272c36; border-radius: 10px; padding: 16px 20px; margin-top: 16px; }}
.chip {{ display: inline-block; font-size: 11px; font-weight: 700; letter-spacing: .08em;
         background: #1e2530; color: #e8a030; padding: 3px 8px; border-radius: 5px; }}
.state {{ font-size: 2em; font-weight: 700; color: #e8a030; margin: 8px 0; }}
</style>
</head>
<body>
<h1>Prometheus</h1>
<p>Read-only status view · <a href="/state">/state JSON</a> · <a href="/news">/news JSON</a></p>
<div class="card">
  <span class="chip">DASHBOARD</span>
  <div class="state" id="state">Loading…</div>
  <p id="project">—</p>
  <p style="font-size:12px; color:#555;">Data fetched from hud_state.json — auto-refresh every 10s.</p>
</div>
<script>
async function update() {{
  try {{
    const r = await fetch('/state');
    const d = await r.json();
    document.getElementById('state').textContent = (d.state || 'idle').toUpperCase();
    document.getElementById('project').textContent = 'Project: ' + (d.active_project || '—');
  }} catch(e) {{}}
}}
update();
setInterval(update, 10000);
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
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
            state = _read_hud_state()
            self._send(200, _json_response(state))

        elif path == "/news":
            state = _read_hud_state()
            news = state.get("cards", {}).get("news", {})
            self._send(200, _json_response(news))

        elif path == "/":
            self._send(200, _HTML_PAGE.encode("utf-8"), "text/html; charset=utf-8")

        else:
            self._send(404, _json_response({"error": "not found"}))

    def do_POST(self) -> None:
        self._send(405, _json_response({"error": "read-only — no mutations allowed"}))

    def do_PUT(self) -> None:
        self._send(405, _json_response({"error": "read-only — no mutations allowed"}))

    def do_DELETE(self) -> None:
        self._send(405, _json_response({"error": "read-only — no mutations allowed"}))


class ReadonlyDashboard:
    """
    Minimal read-only HTTP server that runs in a daemon thread.

    start() is non-blocking — returns immediately.
    stop() shuts down the server.
    """

    def __init__(
        self,
        host: str = "",
        port: int = 0,
    ) -> None:
        self._host = host or os.getenv("PROMETHEUS_READONLY_DASHBOARD_HOST", "0.0.0.0")
        self._port = port or int(os.getenv("PROMETHEUS_READONLY_DASHBOARD_PORT", "8765"))
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the server in a daemon thread. Returns immediately."""
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
                "url": f"http://{self._host}:{self._port}",
            })
            print(f"[DASHBOARD] read-only server started on http://{self._host}:{self._port}", flush=True)
        except Exception as exc:
            from utils import log_event
            log_event("readonly_dashboard_start_error", {"error": str(exc)[:200]})
            print(f"[DASHBOARD] failed to start read-only server: {exc!r:.100}", flush=True)

    def stop(self) -> None:
        if self._server:
            try:
                self._server.shutdown()
            except Exception:
                pass

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


def _esc(text: str) -> str:
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _build_html(state: dict) -> str:
    """Build a Prometheus dark/fire-themed read-only HTML status page."""
    godot_state = str(state.get("state", "idle")).upper()
    project = _esc(state.get("active_project", "—"))
    updated = str(state.get("updated_at", ""))[:19].replace("T", " ")
    cards = state.get("cards") or {}

    # ── State accent color (matches Godot) ────────────────────────────────────
    state_color = {
        "IDLE": "#e8a030", "LISTENING": "#f5ca50", "PROCESSING": "#48a8e0",
        "SPEAKING": "#f09a35", "EXECUTING": "#3cc89e", "WARNING": "#e03838",
    }.get(godot_state, "#e8a030")

    # ── Calendar rail ─────────────────────────────────────────────────────────
    cal_card = cards.get("calendar") or {}
    cal_events = cal_card.get("events") or []
    cal_status = str(cal_card.get("status", "pending"))

    if cal_events:
        cal_event_rows = ""
        for ev in cal_events[:12]:
            if not isinstance(ev, dict):
                continue
            etitle = _esc(ev.get("title") or ev.get("summary") or "Untitled")
            estart = _esc(ev.get("time_label") or ev.get("start_time") or ev.get("start") or "")
            is_now_ev = bool(ev.get("is_now", False))
            is_next_ev = bool(ev.get("is_next", False))
            loc = str(ev.get("location") or "").strip()
            ev_class = "cal-event"
            if is_now_ev:
                ev_class += " cal-ev-now"
            elif is_next_ev:
                ev_class += " cal-ev-next"
            loc_html = f'<div class="cal-eloc">{_esc(loc)}</div>' if loc else ""
            cal_event_rows += (
                f'<div class="{ev_class}">'
                f'<span class="cal-etime">{estart}</span>'
                f'<span class="cal-etitle">{etitle}</span>'
                f'{loc_html}'
                f'</div>'
            )
        if not cal_event_rows:
            cal_event_rows = '<div class="cal-pending">No events found</div>'
    elif cal_status == "error":
        cal_event_rows = '<div class="cal-pending">Calendar fetch failed<br><span class="cal-hint">Check Prometheus logs</span></div>'
    else:
        cal_event_rows = '<div class="cal-pending">Calendar source pending<br><span class="cal-hint">Connect calendar data to dashboard_state.json → cards.calendar.events</span></div>'

    # ── News ─────────────────────────────────────────────────────────────────
    news = cards.get("news") or {}
    news_status = str(news.get("status", "demo"))
    articles = news.get("articles") or []
    live_badge = '<span class="live-badge">● LIVE</span>' if news_status == "live" else '<span class="demo-badge">DEMO</span>'

    news_rows = ""
    for a in articles[:9]:
        if not isinstance(a, dict):
            continue
        title = _esc(a.get("title") or "")
        tag = _esc(a.get("tag") or a.get("section") or "News").upper()
        time_ago = _esc(a.get("time_ago") or "")
        summary = _esc(a.get("summary") or "")
        href = _esc(a.get("href") or a.get("url") or "#")
        thumb = str(a.get("thumb") or a.get("thumbnail") or "").strip()
        if thumb:
            thumb_html = (
                f'<div class="nthumb-row">'
                f'<img class="nthumb" src="{_esc(thumb)}" alt="" loading="lazy" onerror="this.parentNode.classList.remove(\'nthumb-row\')">'
                f'<div class="ntext">'
                f'<div class="nmeta"><span class="npill">{tag}</span><span class="ndim">{time_ago}</span></div>'
                f'<a class="ntitle" href="{href}" target="_blank" rel="noopener noreferrer">{title}</a>'
                f'</div></div>'
                f'<div class="nsummary">{summary}</div>'
            )
        else:
            thumb_html = (
                f'<div class="nmeta"><span class="npill">{tag}</span><span class="ndim">{time_ago}</span></div>'
                f'<a class="ntitle" href="{href}" target="_blank" rel="noopener noreferrer">{title}</a>'
                f'<div class="nsummary">{summary}</div>'
            )
        news_rows += f'<div class="nitem">{thumb_html}</div>'

    if not news_rows:
        news_rows = '<p class="ndim" style="padding:14px 0;grid-column:1/-1">No articles yet — Guardian feed loading…</p>'

    # ── Activity ──────────────────────────────────────────────────────────────
    activity = cards.get("activity") or {}
    act_items = activity.get("items") or []
    act_rows = "\n".join(
        f'<div class="list-row"><span class="list-dot">▸</span><span class="list-text">{_esc(i)}</span></div>'
        for i in act_items[:6]
    ) or '<div class="list-row ndim">No recent activity</div>'

    # ── Tasks ─────────────────────────────────────────────────────────────────
    tasks = cards.get("tasks") or {}
    task_items = tasks.get("items") or []
    task_rows = "\n".join(
        f'<div class="list-row"><span class="list-dot">▸</span><span class="list-text">{_esc(i)}</span></div>'
        for i in task_items[:5]
    ) or '<div class="list-row ndim">No active tasks</div>'

    # ── Mission/Objective ─────────────────────────────────────────────────────
    objective = cards.get("objective") or {}
    obj_summary = _esc(objective.get("summary") or "No active mission.")
    obj_items = objective.get("items") or []
    obj_rows = "\n".join(
        f'<div class="list-row"><span class="list-dot">·</span><span class="list-text">{_esc(i)}</span></div>'
        for i in obj_items[:4]
    )

    # ── Brand ─────────────────────────────────────────────────────────────────
    brand = cards.get("brand") or {}
    brand_items = brand.get("items") or []
    brand_rows = "\n".join(
        f'<div class="list-row"><span class="list-dot">·</span><span class="list-text">{_esc(i)}</span></div>'
        for i in brand_items[:4]
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Prometheus — Mission Control</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --accent:{state_color};
  --amber:#e8a030;
  --amber-dim:rgba(232,160,48,0.18);
  --amber-border:rgba(232,160,48,0.28);
  --card:#0f1318;
  --card-border:rgba(232,160,48,0.22);
  --well:#080b0e;
  --well-border:rgba(232,160,48,0.14);
  --text:#dcd6ca;
  --text-dim:#a09070;
  --muted:#7a7060;
  --dim:#4a4540;
  --green:#3dc87a;
  --blue:#4a9ae8;
  --shadow:rgba(0,0,0,0.55);
}}
body{{
  font-family:system-ui,-apple-system,sans-serif;
  background:#05080a;
  color:var(--text);
  min-height:100vh;
  padding:24px 28px 48px;
}}
/* Header */
.prom-header{{display:flex;align-items:center;gap:16px;margin-bottom:6px}}
.prom-title{{
  font-size:1.6em;font-weight:800;
  color:var(--amber);letter-spacing:.06em;
  text-shadow:0 0 28px rgba(232,160,48,0.35);
}}
.prom-sub-title{{font-size:.78em;color:var(--text-dim);letter-spacing:.06em;font-weight:500}}
.state-badge{{
  font-size:.68em;font-weight:700;letter-spacing:.12em;
  color:var(--accent);border:1px solid var(--accent);
  border-radius:4px;padding:2px 9px;
  text-shadow:0 0 8px color-mix(in srgb,var(--accent) 60%,transparent);
}}
.prom-meta{{color:var(--dim);font-size:.76em;margin-bottom:24px;display:flex;gap:14px;flex-wrap:wrap}}
.prom-meta a{{color:var(--blue);text-decoration:none}}
.prom-meta a:hover{{text-decoration:underline}}
/* Grid */
.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;max-width:1440px}}
.span2{{grid-column:span 2}}
.span3{{grid-column:1/-1}}
@media(max-width:960px){{
  .grid{{grid-template-columns:1fr 1fr}}
  .span2{{grid-column:span 1}}
  .span3{{grid-column:span 2}}
}}
@media(max-width:600px){{
  .grid{{grid-template-columns:1fr}}
  .span2,.span3{{grid-column:span 1}}
  body{{padding:14px 16px 32px}}
}}
/* Cards — match Godot card_style */
.card{{
  background:var(--card);
  border:1px solid var(--card-border);
  border-radius:14px;
  padding:0;
  display:flex;flex-direction:column;
  box-shadow:0 9px 24px var(--shadow);
  overflow:hidden;
}}
.card-head{{
  display:flex;align-items:center;gap:10px;
  padding:12px 16px 10px;
  border-bottom:1px solid var(--amber-border);
}}
.card-title{{
  font-size:.70em;font-weight:700;letter-spacing:.14em;
  color:var(--amber);
  text-transform:uppercase;flex:1;
}}
.card-chip{{
  font-size:.62em;font-weight:700;letter-spacing:.08em;
  padding:2px 7px;border-radius:4px;
  color:var(--amber);
  border:1px solid var(--amber-border);
  background:var(--amber-dim);
}}
/* Content well — matches Godot content_well_style */
.well{{
  background:var(--well);
  border-radius:0 0 13px 13px;
  padding:13px 15px;
  flex:1;
  border-top:none;
}}
/* Status card */
.state-val{{
  font-size:2.2em;font-weight:800;
  color:var(--accent);line-height:1.1;margin-bottom:8px;
  text-shadow:0 0 20px color-mix(in srgb,var(--accent) 50%,transparent);
}}
.state-meta{{font-size:.80em;color:var(--muted);line-height:2.0}}
/* List rows — match Godot bullet rows */
.list-row{{
  display:flex;gap:9px;align-items:baseline;
  padding:6px 0;
  border-bottom:1px solid rgba(232,160,48,0.07);
  font-size:.83em;
}}
.list-row:last-child{{border:none;padding-bottom:0}}
.list-dot{{color:var(--accent);flex-shrink:0;font-size:.75em;margin-top:2px}}
.list-text{{color:var(--text);line-height:1.45}}
/* Mission summary */
.mission-summary{{font-size:.85em;color:var(--text-dim);line-height:1.55;margin-bottom:8px}}
/* News */
.live-badge{{font-size:.65em;font-weight:700;color:var(--green);letter-spacing:.08em}}
.demo-badge{{font-size:.65em;font-weight:700;color:var(--dim);letter-spacing:.08em}}
.news-head{{
  display:flex;align-items:center;gap:8px;
  padding:12px 16px 10px;
  border-bottom:1px solid var(--amber-border);
}}
.news-grid{{
  display:grid;
  grid-template-columns:repeat(3,1fr);
  gap:12px;
  padding:14px 15px;
  background:var(--well);
  border-radius:0 0 13px 13px;
}}
@media(max-width:960px){{.news-grid{{grid-template-columns:1fr 1fr}}}}
@media(max-width:600px){{.news-grid{{grid-template-columns:1fr}}}}
.nitem{{
  display:flex;flex-direction:column;gap:8px;
  background:rgba(15,19,24,0.95);
  border:1px solid rgba(232,160,48,0.14);
  border-radius:9px;
  padding:11px 12px;
  overflow:hidden;
  transition:border-color .18s;
}}
.nitem:hover{{border-color:rgba(232,160,48,0.32)}}
.nthumb-row{{display:flex;gap:10px;align-items:flex-start}}
.nthumb{{
  width:52px;height:52px;
  object-fit:cover;border-radius:6px;
  flex-shrink:0;
}}
.ntext{{flex:1;min-width:0}}
.nmeta{{display:flex;align-items:center;gap:7px;margin-bottom:5px;flex-wrap:wrap}}
.npill{{
  font-size:.61em;font-weight:700;letter-spacing:.06em;
  padding:1px 6px;border-radius:3px;
  background:rgba(74,154,232,0.15);color:var(--blue);
  border:1px solid rgba(74,154,232,0.28);white-space:nowrap;
}}
.ndim{{font-size:.68em;color:var(--dim)}}
.ntitle{{
  font-size:.87em;font-weight:600;color:#e8c870;
  text-decoration:none;display:block;
  line-height:1.38;margin-bottom:4px;
}}
.ntitle:hover{{color:#f5da80;text-decoration:underline}}
.nsummary{{
  font-size:.75em;color:var(--muted);line-height:1.45;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;
}}
/* Refresh indicator */
.refresh-bar{{
  position:fixed;bottom:0;left:0;right:0;height:2px;
  background:linear-gradient(90deg,transparent,var(--amber),transparent);
  opacity:0;animation:refreshFade 1s ease-in-out 14s forwards;
}}
@keyframes refreshFade{{0%{{opacity:0}}50%{{opacity:.7}}100%{{opacity:0}}}}
/* Outer two-column layout: main content + right calendar rail */
.outer-layout{{display:flex;gap:20px;align-items:flex-start;max-width:1640px}}
.main-content{{flex:1;min-width:0}}
.cal-rail{{
  width:290px;flex-shrink:0;
  display:flex;flex-direction:column;gap:14px;
  position:sticky;top:24px;
  height:calc(100vh - 100px);
  align-self:flex-start;
}}
/* Analog clock */
.clock-wrap{{
  background:var(--card);border:1px solid var(--card-border);
  border-radius:14px;padding:18px 14px 12px;
  display:flex;flex-direction:column;align-items:center;gap:8px;
  box-shadow:0 9px 24px var(--shadow);
}}
.clock-face{{display:block}}
.clock-date-line{{font-size:.72em;color:var(--text-dim);letter-spacing:.04em;text-align:center}}
/* Calendar events rail card */
.cal-card{{
  background:var(--card);border:1px solid var(--card-border);
  border-radius:14px;overflow:hidden;
  box-shadow:0 9px 24px var(--shadow);
  flex:1;min-height:0;display:flex;flex-direction:column;
}}
.cal-events-scroll{{flex:1;overflow-y:auto;padding:10px 12px;display:flex;flex-direction:column;gap:8px;}}
.cal-card-head{{
  display:flex;align-items:center;gap:10px;
  padding:10px 14px 8px;
  border-bottom:1px solid var(--amber-border);
}}
.cal-card-title{{font-size:.68em;font-weight:700;letter-spacing:.14em;color:var(--amber);text-transform:uppercase;flex:1}}
.cal-event{{
  display:flex;flex-direction:column;gap:2px;
  padding:7px 9px 7px 11px;border-radius:8px;
  background:var(--well);
  border-left:2px solid var(--well-border);
  border-top:1px solid var(--well-border);
  border-right:1px solid var(--well-border);
  border-bottom:1px solid var(--well-border);
}}
.cal-event.cal-ev-now{{
  background:rgba(232,160,48,0.12);
  border-left:3px solid var(--amber);
  border-top:1px solid rgba(232,160,48,0.20);
  border-right:1px solid rgba(232,160,48,0.20);
  border-bottom:1px solid rgba(232,160,48,0.20);
}}
.cal-event.cal-ev-next{{
  background:rgba(232,160,48,0.06);
  border-left:2px solid rgba(232,160,48,0.55);
}}
.cal-etime{{font-size:.66em;color:var(--amber);letter-spacing:.04em;font-weight:600}}
.cal-etitle{{font-size:.78em;color:var(--text);line-height:1.35}}
.cal-eloc{{font-size:.65em;color:var(--dim);margin-top:1px}}
.cal-pending{{
  padding:14px 14px;font-size:.76em;color:var(--muted);line-height:1.5;
}}
.cal-hint{{font-size:.86em;color:var(--dim)}}
@media(max-width:1100px){{
  .outer-layout{{flex-direction:column}}
  .cal-rail{{width:100%;flex-direction:row;flex-wrap:wrap;position:static;height:auto}}
  .clock-wrap{{flex:0 0 auto}}
  .cal-card{{flex:1;min-width:220px}}
}}
</style>
</head>
<body>
<div class="prom-header">
  <span class="prom-title">PROMETHEUS</span>
  <span class="prom-sub-title">MISSION CONTROL</span>
  <span class="state-badge">{godot_state}</span>
</div>
<div class="prom-meta">
  <span>Read-only</span>
  <span>{updated} UTC</span>
  <span>Project: {project}</span>
  <a href="/state">/state JSON</a>
  <a href="/news">/news JSON</a>
</div>
<div class="outer-layout">
<div class="main-content">
<div class="grid">
  <div class="card">
    <div class="card-head"><span class="card-title">Status</span></div>
    <div class="well">
      <div class="state-val">{godot_state}</div>
      <div class="state-meta">Project: {project}<br>Updated: {updated}</div>
    </div>
  </div>
  <div class="card span2">
    <div class="card-head"><span class="card-title">Mission</span><span class="card-chip">CONTEXT</span></div>
    <div class="well">
      <div class="mission-summary">{obj_summary}</div>
      {obj_rows}
    </div>
  </div>
  <div class="card">
    <div class="card-head"><span class="card-title">Activity</span><span class="card-chip">LIVE</span></div>
    <div class="well">{act_rows}</div>
  </div>
  <div class="card">
    <div class="card-head"><span class="card-title">Prometheus</span></div>
    <div class="well">{brand_rows}</div>
  </div>
  <div class="card">
    <div class="card-head"><span class="card-title">Tasks</span><span class="card-chip">QUEUE</span></div>
    <div class="well">{task_rows}</div>
  </div>
  <div class="card span3" style="padding:0">
    <div class="news-head">
      <span class="card-title" style="color:var(--amber)">Relevant News</span>
      <span class="card-chip">The Guardian</span>
      &nbsp;{live_badge}
    </div>
    <div class="news-grid">{news_rows}</div>
  </div>
</div>
</div><!-- .main-content -->

<div class="cal-rail">
  <div class="clock-wrap">
    <svg id="clock-face" class="clock-face" viewBox="0 0 120 120" width="160" height="160" aria-label="Analog clock">
      <!-- Face -->
      <circle cx="60" cy="60" r="56" fill="none" stroke="#2a2e35" stroke-width="2"/>
      <!-- Hour tick marks -->
      <g stroke="#4a4e55" stroke-width="2">
        <line x1="60" y1="8"  x2="60" y2="16"/>
        <line x1="60" y1="8"  x2="60" y2="16" transform="rotate(30,60,60)"/>
        <line x1="60" y1="8"  x2="60" y2="16" transform="rotate(60,60,60)"/>
        <line x1="60" y1="8"  x2="60" y2="16" transform="rotate(90,60,60)"/>
        <line x1="60" y1="8"  x2="60" y2="16" transform="rotate(120,60,60)"/>
        <line x1="60" y1="8"  x2="60" y2="16" transform="rotate(150,60,60)"/>
        <line x1="60" y1="8"  x2="60" y2="16" transform="rotate(180,60,60)"/>
        <line x1="60" y1="8"  x2="60" y2="16" transform="rotate(210,60,60)"/>
        <line x1="60" y1="8"  x2="60" y2="16" transform="rotate(240,60,60)"/>
        <line x1="60" y1="8"  x2="60" y2="16" transform="rotate(270,60,60)"/>
        <line x1="60" y1="8"  x2="60" y2="16" transform="rotate(300,60,60)"/>
        <line x1="60" y1="8"  x2="60" y2="16" transform="rotate(330,60,60)"/>
      </g>
      <!-- Hour hand -->
      <line id="hand-h" x1="60" y1="60" x2="60" y2="30" stroke="#c8c0b0" stroke-width="3.5" stroke-linecap="round"/>
      <!-- Minute hand -->
      <line id="hand-m" x1="60" y1="60" x2="60" y2="18" stroke="#a89878" stroke-width="2.5" stroke-linecap="round"/>
      <!-- Second hand -->
      <line id="hand-s" x1="60" y1="68" x2="60" y2="14" stroke="#e8a030" stroke-width="1.2" stroke-linecap="round"/>
      <!-- Center cap -->
      <circle cx="60" cy="60" r="3.5" fill="#6a6458"/>
      <circle cx="60" cy="60" r="1.5" fill="#e8a030"/>
    </svg>
    <div class="clock-date-line" id="clock-date"></div>
  </div>

  <div class="cal-card">
    <div class="cal-card-head">
      <span class="cal-card-title">Today</span>
      <span class="card-chip">CALENDAR</span>
    </div>
    <div class="cal-events-scroll">
      {cal_event_rows}
    </div>
  </div>
</div><!-- .cal-rail -->

</div><!-- .outer-layout -->
<div class="refresh-bar"></div>
<script>
(function(){{
  function pad(n){{return String(n).padStart(2,'0');}}
  function tick(){{
    var now=new Date();
    var s=now.getSeconds(),m=now.getMinutes()+s/60,h=(now.getHours()%12)+m/60;
    var sd=s*6,md=m*6,hd=h*30;
    function rot(id,deg,cx,cy){{
      var el=document.getElementById(id);
      if(el) el.setAttribute('transform','rotate('+deg+','+cx+','+cy+')');
    }}
    rot('hand-s',sd,60,60);
    rot('hand-m',md,60,60);
    rot('hand-h',hd,60,60);
    var days=['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
    var months=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    var d=document.getElementById('clock-date');
    if(d) d.textContent=days[now.getDay()]+' '+months[now.getMonth()]+' '+now.getDate()+' · '+pad(now.getHours())+':'+pad(now.getMinutes());
  }}
  tick();
  setInterval(tick,1000);
  setTimeout(()=>location.reload(),15000);
}})();
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

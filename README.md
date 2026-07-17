# PROMETHEUS

**PROMETHEUS** is a local-first, voice-driven desktop assistant for Linux. It listens over push-to-talk, routes known commands deterministically to local tools, uses the OpenAI Realtime API for open-ended requests, watches workspace and calendar context, controls Home Assistant devices, runs background coding tasks with Claude, and drives a Godot mission-control dashboard.

## Dashboard Preview

![PROMETHEUS idle dashboard](assets/idle.png)

The dashboard (sibling repo directory `../Frontend_Dashboard`, Godot 4) reads `../state/dashboard_state.json`, written continuously by the running core.

## Architecture

One process, one entry point. `main.py` is a thin launcher; everything lives in the `prometheus` package:

```
prometheus/
│  # harness — routing, orchestration, permissions, execution, events
├── core/          PrometheusCore runtime loop, Realtime API client,
│                  deterministic intent overrides, session context, identity
├── execution/     ToolRegistry (desktop_action), background worker pool,
│                  workspace policy, response synthesizer (truth contract)
├── planning/      Planner → Executor → Verifier loop for background tasks,
│                  workflow selector/registry, decision router
├── sensors/       Event bus + window/process/clipboard/filesystem/error sensors
├── infra/         Config, paths, logging utils, LLM router
│
│  # capabilities — one vertical package per feature
├── voice/         Mic capture, speaker, push-to-talk, wake word
├── coding/        Architect/Coder/Tester/Debugger agents, orchestrated builds,
│                  Claude CLI coding agent, git checkpoint/rollback safety
├── calendar/      Calendar read/create tools + Lumen proposal queues
├── routines/      Morning routine + calendar event trigger engine
├── memory/        Working/episodic/semantic/procedural memory, vault query
│                  (Obsidian corpus via SQLite FTS5), session summarizer
├── context/       Contextual intent resolver, world model, mission state
├── workspace/     Ambient window/project awareness, project resolver
├── hud/           Dashboard state writer + visual state (Godot HUD)
├── news/          Guardian news fetch/scoring for the HUD
│
│  # integrations — external system adapters, no harness knowledge
└── integrations/  Google Calendar API adapter, Home Assistant verifier
```

Prometheus is **reactive by default**: it initiates speech only when you talk
to it or when an explicitly enabled scheduled routine (morning routine) fires.
There are no proactive announcements, startup briefings, or idle model calls.

See [prometheus/ARCHITECTURE.md](prometheus/ARCHITECTURE.md) for request flow, event flow, and how to add a capability.

`gesture_control/` is a standalone camera-gesture subsystem (MediaPipe); it is not part of the core runtime.

## Prerequisites

- Ubuntu / KDE Plasma on X11 (`wmctrl`, `xdotool` for workspace awareness)
- Python 3.12 + virtualenv at `.venv`
- Microphone and speakers (voice), Ollama running locally (fast LLM routing, optional)
- OpenAI API key (Realtime voice), Home Assistant on LAN (device control, optional)
- `claude` CLI on PATH (background coding tasks, optional)
- Godot 4 (dashboard, optional)

## Configuration

Secrets go in `.env` at the repo root (never committed):

```bash
OPENAI_API_KEY=...
HOME_ASSISTANT_API_KEY=...
HOME_ASSISTANT_URL=http://homeassistant.local:8123
PORCUPINE_ACCESS_KEY=...              # optional wake word
PROMETHEUS_MORNING_ROUTINE_ENABLED=true
PROMETHEUS_REALTIME_REQUIRED=false    # true = fail startup if Realtime is down
```

Runtime config lives at `~/.jarvis/config.json` and deep-merges over `DEFAULT_CONFIG` in `prometheus/infra/config.py` (`vault_path` enables the Obsidian memory corpus). Google Calendar OAuth credentials live under `runtime/secrets/google/` (git-ignored).

## Running

```bash
./prometheus.sh start      # systemd user service (autostarts on login)
./prometheus.sh stop | restart | status | logs

# manual / dev
source .venv/bin/activate
python3 main.py

# dashboard (separate window, optional)
../Frontend_Dashboard/launch_dashboard.sh
```

Voice startup failures are non-fatal by default: the core keeps running (HUD writer, routines, calendar triggers) without voice and reports why.

## Testing

```bash
.venv/bin/python -m pytest tests/                       # hermetic suite (sandboxed HOME, no API calls)
PROMETHEUS_LIVE_TESTS=1 .venv/bin/python -m pytest tests/live -q   # read-only live smoke tests
./scripts/prometheus_daily_readiness.sh                 # 11 readiness gates, scored
```

The hermetic suite redirects `HOME` to a temp sandbox and blanks API keys
before importing anything — it can never touch real state, paid services, or
devices. The live smoke layer is read-only (vault, calendar reads, Lumen
outbox, heartbeat, HUD state) and only runs when explicitly requested.

Manual diagnostics live in `scripts/` (PTT trace, morning-routine dry runs, HA script tests — the `test_morning_*.py` scripts hit real devices; run deliberately).

The morning routine can be exercised without waiting for a calendar event:

```bash
.venv/bin/python scripts/run_morning_routine_now.py        # real HA calls
```

## Lumen (calendar subsystem)

`../Lumen` is a subordinate calendar-intelligence project with its own tests. Prometheus reads calendars through `prometheus/integrations/google_calendar.py` and exchanges calendar *write* proposals with Lumen through file-based queues (`../Lumen/runtime/outbox` → `runtime/pending|reviewed|approved|completed/lumen_calendar`). Writes require explicit approval:

```bash
python -m prometheus.calendar.lumen_router --list-pending
python -m prometheus.calendar.lumen_executor --approve REQUEST_ID
python -m prometheus.calendar.lumen_executor --execute-approved REQUEST_ID
```

## Known limitations

- Voice requires working audio devices and an OpenAI key; wake word requires Porcupine.
- Workspace awareness is X11-only.
- Home Assistant scripts must exist as `script.jarvis_*` / `script.prometheus_*` entities.
- Legacy `jarvis` naming persists in `~/.jarvis/` state paths and HA entity names.

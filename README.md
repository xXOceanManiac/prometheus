# PROMETHEUS

**PROMETHEUS** is a local-first, voice-driven desktop assistant for Linux.

It handles known commands through deterministic local routing, uses the OpenAI Realtime API for open-ended voice interaction, reads workspace and calendar context, controls approved Home Assistant devices, runs background coding tasks through Claude, and publishes normalized state to a Godot mission-control dashboard.

Prometheus is **reactive by default**. It does not initiate speech, model calls, startup briefings, or background announcements unless:

* the user directly interacts with it; or
* an explicitly enabled scheduled routine runs.

## Dashboard Preview

![PROMETHEUS idle dashboard](assets/idle.png)

The Godot dashboard lives in the sibling directory:

```text
../Frontend_Dashboard
```

The running Prometheus core continuously writes normalized dashboard state to:

```text
../state/dashboard_state.json
```

The dashboard reads that state without importing Prometheus internals.

## Architecture

Prometheus runs as one process with one entry point.

`main.py` is a thin launcher. The application lives inside the `prometheus` package and is organized into three broad layers:

```text
prometheus/
│
│  Harness
│  Routing, orchestration, permissions, execution, and events
│
├── core/
│   Runtime loop, Realtime client, deterministic intent routing,
│   session context, and identity
│
├── execution/
│   Tool registry, background workers, workspace policy,
│   response synthesis, and execution truth contracts
│
├── planning/
│   Planner → Executor → Verifier workflows, workflow selection,
│   registry, and decision routing
│
├── sensors/
│   Event bus and window, process, clipboard, filesystem,
│   and error sensors
│
├── infra/
│   Configuration, paths, logging, and LLM routing
│
│  Capabilities
│  Vertical packages for user-facing functionality
│
├── voice/
│   Microphone capture, speaker output, push-to-talk, and wake word
│
├── coding/
│   Architect, Coder, Tester, and Debugger agents; background builds;
│   Claude CLI execution; Git checkpoint and rollback safety
│
├── calendar/
│   Calendar reads and writes, plus Lumen proposal queues
│
├── routines/
│   Morning routine and calendar-triggered workflows
│
├── memory/
│   Working, episodic, semantic, and procedural memory;
│   Obsidian corpus search through SQLite FTS5;
│   session summarization
│
├── context/
│   Contextual intent resolution, world model, and mission state
│
├── workspace/
│   Window and project awareness, plus project resolution
│
├── hud/
│   Dashboard state writer and normalized visual state
│
├── news/
│   Guardian news retrieval and relevance scoring for direct requests
│   and enabled routines
│
│  Integrations
│  External system adapters with no harness knowledge
│
└── integrations/
    Google Calendar and Home Assistant adapters
```

The intended dependency direction is:

```text
interfaces
    ↓
harness
    ↓
capabilities
    ↓
integrations
```

Capabilities expose explicit in-process Python interfaces. Prometheus does not use internal HTTP APIs, microservices, message brokers, or dynamic plugin frameworks.

See [`prometheus/ARCHITECTURE.md`](prometheus/ARCHITECTURE.md) for request flow, event flow, dependency boundaries, and instructions for adding capabilities.

## Reactive Behavior

Prometheus does not perform general proactive narration.

Removed behavior includes:

* startup briefings;
* idle model polling;
* proactive announcements;
* background build announcements;
* automatic old-error resurfacing;
* presence-based narration;
* automatic shutdown summaries;
* generic event-start speech.

Background task status remains available through the HUD and direct status requests.

The morning routine is the primary scheduled speech workflow. It is controlled explicitly through configuration and performs no model, voice, calendar, media, or device activity when disabled.

## Standalone Components

`gesture_control/` is a standalone MediaPipe camera-gesture subsystem.

It is not part of the Prometheus runtime and does not participate in the harness or capability architecture.

The Godot HUD is also maintained as a separate sibling project:

```text
../Frontend_Dashboard
```

## Prerequisites

Prometheus currently targets Ubuntu with KDE Plasma on X11.

Required or optional dependencies include:

* Python 3.12;
* a virtual environment at `.venv`;
* `wmctrl` and `xdotool` for workspace awareness;
* microphone and speakers for voice interaction;
* an OpenAI API key for Realtime voice;
* Ollama for optional local intent routing;
* Home Assistant for optional device control;
* the `claude` CLI for optional background coding tasks;
* Porcupine for optional wake-word detection;
* Godot 4 for the optional dashboard.

## Configuration

Secrets belong in `.env` at the repository root and must never be committed.

```bash
OPENAI_API_KEY=...
HOME_ASSISTANT_API_KEY=...
HOME_ASSISTANT_URL=http://homeassistant.local:8123

PORCUPINE_ACCESS_KEY=...               # optional wake word
PROMETHEUS_MORNING_ROUTINE_ENABLED=true
PROMETHEUS_REALTIME_REQUIRED=false     # true = fail startup if Realtime is unavailable
```

Runtime configuration currently lives at:

```text
~/.jarvis/config.json
```

This file deep-merges over `DEFAULT_CONFIG` in:

```text
prometheus/infra/config.py
```

The legacy `.jarvis` path remains for compatibility with existing local state. New architecture and package names use Prometheus terminology.

Setting `vault_path` enables the Obsidian memory corpus.

Google Calendar OAuth credentials live under:

```text
runtime/secrets/google/
```

That directory is Git-ignored.

## Running Prometheus

### systemd user service

```bash
./prometheus.sh start
./prometheus.sh stop
./prometheus.sh restart
./prometheus.sh status
./prometheus.sh logs
```

The service is configured to start automatically with the user session.

### Manual development run

```bash
source .venv/bin/activate
python3 main.py
```

### Dashboard

```bash
../Frontend_Dashboard/launch_dashboard.sh
```

Voice startup failures are non-fatal by default.

When voice initialization fails, the core may continue running local services such as:

* HUD state output;
* enabled routines;
* calendar triggers;
* local diagnostics.

Set `PROMETHEUS_REALTIME_REQUIRED=true` when voice availability should be required for startup.

## Testing

Prometheus uses three levels of verification:

1. runtime contract tests;
2. hermetic integration tests;
3. opt-in read-only live smoke tests.

### Hermetic suite

```bash
.venv/bin/python -m pytest tests/
```

The default suite:

* redirects `HOME` to a temporary sandbox;
* clears API credentials before Prometheus imports;
* uses temporary Git repositories and runtime state;
* does not touch real user memory;
* does not call paid services;
* does not write to Google Calendar;
* does not control Home Assistant devices.

Tests are expected to exercise production runtime paths. External providers may be replaced at adapter boundaries, but the Prometheus behavior under test should remain real.

### Live smoke tests

```bash
PROMETHEUS_LIVE_TESTS=1 \
  .venv/bin/python -m pytest tests/live -q
```

Live tests are opt-in and read-only.

They may verify:

* vault access;
* Google Calendar reads;
* Lumen queue access;
* service heartbeat;
* HUD state freshness.

A mocked or fixture-backed check must not be reported as live verification.

### Readiness suite

```bash
./scripts/prometheus_daily_readiness.sh
```

The readiness suite currently checks eleven operational gates, including reactive-by-default behavior.

### Manual diagnostics

Additional diagnostics live in `scripts/`.

Some scripts intentionally interact with real services or devices. Review them before running.

Examples include:

* push-to-talk tracing;
* voice diagnostics;
* Home Assistant script tests;
* morning-routine dry runs;
* deliberate live morning-routine execution.

## Morning Routine

The morning routine can be triggered manually without waiting for its calendar event:

```bash
.venv/bin/python scripts/run_morning_routine_now.py
```

This command may perform real Home Assistant, media, voice, calendar, and device actions.

Run it deliberately.

When `PROMETHEUS_MORNING_ROUTINE_ENABLED=false`, the routine must not:

* create a Realtime session;
* call a model;
* enqueue speech;
* query routine-only calendar context;
* control Home Assistant devices;
* start media.

## Lumen Calendar Subsystem

Lumen lives in the sibling directory:

```text
../Lumen
```

It is a subordinate calendar-intelligence project with its own tests and runtime state.

Prometheus reads Google Calendar through:

```text
prometheus/integrations/google_calendar.py
```

Calendar write proposals are exchanged with Lumen through file-based queues:

```text
../Lumen/runtime/outbox
        ↓
runtime/pending/lumen_calendar
runtime/reviewed/lumen_calendar
runtime/approved/lumen_calendar
runtime/completed/lumen_calendar
```

Calendar writes require explicit approval.

```bash
python -m prometheus.calendar.lumen_router --list-pending

python -m prometheus.calendar.lumen_executor \
  --approve REQUEST_ID

python -m prometheus.calendar.lumen_executor \
  --execute-approved REQUEST_ID
```

## Safety Boundaries

Prometheus includes deterministic safeguards around sensitive execution paths.

Current protections include:

* workspace containment;
* blocking path traversal and unauthorized absolute paths;
* shell-command restrictions;
* Git checkpoint and rollback behavior;
* explicit approval for calendar writes;
* Home Assistant script allowlists;
* post-execution Home Assistant verification;
* test isolation from live runtime state;
* no automatic paid-service calls during the default test suite.

Security and permission decisions should remain outside model control.

## Repository Philosophy

Prometheus favors:

* explicit code over framework magic;
* coherent vertical capability packages;
* deterministic routing where possible;
* local execution where practical;
* bounded runtime state;
* meaningful tests over large test counts;
* deletion of obsolete implementations;
* Git history instead of legacy folders;
* simple architecture that can remain understandable over time.

Do not preserve unused code in `legacy`, `old`, `backup`, or compatibility directories.

## Known Limitations

* Voice requires working audio devices and an OpenAI API key.
* Wake-word detection requires a Porcupine access key.
* Workspace awareness currently depends on X11.
* Home Assistant scripts must exist under approved `script.jarvis_*` or `script.prometheus_*` entity names.
* Some local runtime paths and Home Assistant entities still use legacy `jarvis` naming.
* The Godot HUD is maintained outside the main repository package.
* Full voice, physical-device, and Claude CLI verification require deliberate live testing.
* `execution/tools.py` remains large and may eventually be divided by coherent action domain, but it should not be split solely to reduce file length.

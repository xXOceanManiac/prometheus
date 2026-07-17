# Prometheus — Architecture Reference

A local-first, voice-driven desktop assistant with deterministic tool routing,
persistent memory, and ambient workspace awareness. One process, one entry
point: `main.py` (thin launcher) → `prometheus.core.main.PrometheusCore`.

**Reactive by default.** Prometheus initiates speech in exactly three cases:
the user interacts with it (PTT, wake word, HUD chat), the user explicitly
starts voice mode, or an explicitly enabled scheduled routine fires (morning
routine). There is no proactive loop, no startup briefing, no idle model
calls, no automatic announcements. If machinery for any of those reappears,
readiness gate 10 (`TestGateReactiveByDefault`) fails.

---

## Harness vs. Capabilities

The **harness** owns: receiving requests, routing, orchestration, permissions,
execution context, event emission, workflow coordination, and returning
results. It contains as little domain logic as possible.

**Capabilities** are vertical packages — each contains everything needed to
understand one feature. The harness calls capabilities; capabilities never
import harness internals. **Integrations** are thin adapters for external
systems; they don't know the harness exists.

```
prometheus/
│  # harness
├── core/           PrometheusCore loop, Realtime client, intent overrides,
│                   session context, identity, profile
├── execution/      ToolRegistry (tools.py, ACTION_ENUM), background workers,
│                   workspace policy, verification, response synthesizer
├── planning/       Planner/Executor/Verifier for general background tasks,
│                   decision router, workflow selector + registry
├── sensors/        Event bus, sensor manager, window/clipboard/filesystem/
│                   error/process sensors
├── infra/          config, paths, utils (log_event), llm_router, log_viewer
│
│  # capabilities
├── voice/          MicRecorder/Speaker (audio.py), push-to-talk, wake word
├── coding/         agent_base + Architect/Coder/Tester/Debugger, orchestrated
│                   builds (orchestrator.py), Claude CLI coding agent,
│                   git_safety checkpoints, success criteria
├── calendar/       read_tools + create_flow (Google Calendar), lumen_* modules
│                   (file-queue proposal review/approval/execution)
├── routines/       Morning routine service + adapters + calendar event
│                   trigger engine
├── memory/         memory, memory_core (vault FTS5), working/episodic/
│                   semantic/procedural memory, session summarizer
├── context/        Contextual intent resolver, world model, mission state,
│                   cognition (operational snapshot for LLM planning)
├── workspace/      WorkspaceManager (window/project awareness, Xbox polling),
│                   ProjectResolver
├── hud/            hud_state_writer (dashboard_state.json), visuals
├── news/           Guardian fetch + relevance scoring (HUD news card)
│
│  # integrations
└── integrations/   Google Calendar API adapter, Home Assistant verifier
```

Always-on service objects are wired explicitly in
`core/main.py::PrometheusCore.startup()` with a matching `stop()` in
`shutdown()`. No plugin framework, no registries beyond `ToolRegistry`.

---

## Request Flow

```
Microphone (voice/audio.py MicRecorder)
    │ PCM16 chunks, PTT- or wake-word-gated (core/main.py turn logic)
    ▼
RealtimePrometheusClient (core/realtime_client.py)
    │ on transcription completed:
    ├─► resolve_direct_intent()        core/intent_overrides.py — deterministic,
    │       │                          ~0ms phrase match for known commands
    │       ▼
    │   _run_direct_tool()             ToolRegistry.execute()
    │       ▼
    │   _guarded_response_create()     LLM speaks the verified result
    │
    ├─► _contextual_override()         context/contextual_intent.py — rule-based
    │                                  resolver for vague commands (<50ms, no LLM)
    │
    └─► OpenAI Realtime API            last resort for open-ended requests
            │ function_call → desktop_action
            ▼
        _handle_tool_call()            ToolRegistry.execute()
```

Text requests follow the same path: the HUD writes `chat_input` into
WorkingMemory; `_chat_polling_loop` in the Realtime client picks it up, applies
the same overrides, and answers via `llm_router.chat_completion()`.

The deterministic path is always preferred. Every voice-executable action is
enumerated in `ACTION_ENUM` (execution/tools.py); nothing executes via voice
outside that set.

Both tool paths (direct override and LLM function call) produce spoken
responses through one function —
`execution/response_synthesizer.build_response_instructions()` — so wording
never depends on which path executed the tool.

## Tool Truth Contract

`ToolResult` (execution/tools.py) carries a status that the response layer must
respect:

- `verified_success` — ran AND a post-execution check confirmed the outcome
  (e.g. Home Assistant state re-read by integrations/ha_verifier.py)
- `accepted_unverified` — ran, reported ok, outcome not independently confirmed
- `tool_failure` — failed; the spoken response must say so

`execution/response_synthesizer.py` generates response instructions from this
status so the assistant never claims unverified success.

## Background Execution

```
start_coding_task / start_build / BackgroundWorkerPool.submit
    │ (coding/coding_agent.py, coding/orchestrator.py)
    ▼
Git checkpoint (coding/git_safety.py — commits repo before the run)
    ▼
Claude CLI (coding) or Planner → Executor → Verifier loop (general tasks,
    ProcessPoolExecutor pinned to cores 4-7, execution/background_worker.py)
    ▼
Success criteria evaluated → retry (≤3) → rollback to checkpoint on failure
    ▼
Result → WorkingMemory + desktop notification (no spoken announcement;
    status queryable via get_coding_status / get_build_status)
```

## Event Flow

```
sensors (window/clipboard/filesystem/error/process, async polling)
    ▼
EventBus.publish (sensors/event_bus.py, typed events, priorities)
    ▼
subscribers: core/main.py (session context refresh), context/world_model.py
```

HUD: `hud/hud_state_writer.py` composes assistant state + calendar + news +
system metrics and atomically writes `<ecosystem>/state/dashboard_state.json`;
the Godot dashboard (`../Frontend_Dashboard`) only reads that file — it never
imports capability internals. Visual states: idle/armed, listening,
processing, speaking, background_working. `hud/visuals.py` writes
`~/.jarvis/visual_state.json` for the same purpose at higher frequency.

## Scheduled Routines

The calendar event trigger engine (`routines/calendar_event_triggers.py`)
polls Google Calendar and fires registered `CalendarRoutineRule` handlers at
event start times. Events with no matching rule are ignored — never spoken.

The morning routine is the only registered routine. It has one switch:
`PROMETHEUS_MORNING_ROUTINE_ENABLED` (checked by
`routines/morning_routine.morning_routine_enabled()`). When off,
`PrometheusCore._init_morning_routine()` constructs nothing: no calendar
polling, no Realtime sessions, no speech, no device control.

## Memory Model

| Store | File(s) | Purpose | Retention |
|-------|---------|---------|-----------|
| WorkingMemory | `~/.jarvis/memory_v2/working_memory.json` | current session context, last tool result, chat handoff | overwritten continuously |
| Episodic | `~/.jarvis/memory_v2/` | session/event history | append; summarized |
| Semantic | `~/.jarvis/memory_v2/` | durable facts | append/update |
| Procedural | `~/.jarvis/memory_v2/` | learned routines | append/update |
| Vault | `$VAULT_PATH/data/memory.db` (external Obsidian corpus) | long-term personal memory, 32K+ chunks | queried at runtime via FTS5 (`memory_core.query_vault`), never duplicated |
| Session summaries | vault markdown | written only by the explicit `session_wrapup` tool | permanent |

Logs (`~/.jarvis/logs/*.jsonl`, via `infra/utils.log_event`) are diagnostics,
not memory: one file per day, queried by `run_diagnostics` and the trace
debugger, safe to delete. Log content must never become future speech.

## Permission Model

- **Workspace boundary** — every `write_file` resolves through
  `execution/workspace_policy.resolve_workspace_path()`: paths are
  `.resolve()`d (symlinks followed) then containment-checked against
  `runtime/workspace/`. Traversal, absolute-path escape, and symlink escape
  all raise `PermissionError`.
- **Shell** — `run_shell` accepts only a whitelisted set of first tokens;
  `run_python` blocks destructive builtins and subprocess use.
- **Git** — `git_commit` always requires `confirmed=True`; coding agents
  checkpoint before running and roll back on failure.
- **Calendar writes** — proposals flow through file queues and require
  explicit approval (`prometheus.calendar.lumen_executor --approve` then
  `--execute-approved`); reads are unrestricted.
- **Home Assistant** — deterministic script allowlist (`HARDCODED_HA_SCRIPTS`);
  post-execution state verification; destructive actions require confirmation.

## Adding or Modifying a Capability

1. Add the action name to `ACTION_ENUM` in `execution/tools.py` and implement
   its handler in `ToolRegistry._execute_one_inner`. Return a `ToolResult`
   with an honest status.
2. If the LLM should follow up after the tool, add it to
   `core/tool_followups.FOLLOWUP_ACTIONS`; action-specific spoken wording goes
   in `response_synthesizer.build_response_instructions()`.
3. For latency-sensitive phrases, add a pattern to
   `core/intent_overrides.resolve_direct_intent()`.
4. Long-running work goes through `BackgroundWorkerPool` or a coding-agent
   dispatch — never block the voice loop.
5. New external systems get a small adapter in `integrations/`; wire any
   always-on service object in `PrometheusCore.startup()` with a matching
   `stop()` in `shutdown()`.
6. Test both paths (direct override and LLM function call). Git-touching
   tests must use the `temp_git_repo` fixture — never the real repo.

## Testing

Three layers:

1. **Runtime contract tests** — truth contract, trace IDs, session config,
   reactive-by-default gate (`tests/`, `tests/acceptance/`).
2. **Hermetic integration tests** — real production code paths with only true
   external boundaries mocked (OpenAI, Google, Home Assistant, hardware).
   `tests/conftest.py` sandboxes `HOME` and blanks API keys before any
   prometheus import, so the default suite can never touch real state.
3. **Read-only live smoke tests** — `tests/live/`, opt-in via
   `PROMETHEUS_LIVE_TESTS=1`; verifies the real vault, calendar reads, Lumen
   outbox, service heartbeat, and HUD state freshness. Live mode runs only
   live tests.

`tests/acceptance/test_daily_readiness.py` — 11 scored gates, run via
`scripts/prometheus_daily_readiness.sh`. Manual device-touching diagnostics
live in `scripts/` (morning routine, HA scripts, audio sink).

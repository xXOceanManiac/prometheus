# Prometheus — Architecture Reference

A local-first, voice-driven desktop assistant with deterministic tool routing,
persistent memory, and ambient workspace awareness. One process, one entry
point: `main.py` (thin launcher) → `prometheus.core.main.PrometheusCore`.

---

## Package Structure

```
prometheus/
├── core/           PrometheusCore loop, Realtime client, intent overrides,
│                   session context/briefing, identity, profile, proactive loop
├── voice/          MicRecorder/Speaker (audio.py), push-to-talk, wake word
├── execution/      ToolRegistry (tools.py), coding agent, background workers,
│                   workspace policy, git safety, success criteria
├── planning/       Planner/Executor/Verifier, orchestrator, decision router,
│                   workflow selector + registry
├── agents/         agent_base + Architect/Coder/Tester/Debugger,
│                   Lumen calendar ingestion/router/executor + create flow
├── context/        Contextual intent resolver, world model, mission state,
│                   cognition (operational snapshot for LLM planning)
├── memory/         memory, memory_core (vault FTS5), working/episodic/
│                   semantic/procedural memory, session summarizer,
│                   dream manager, behavior learning
├── sensors/        Event bus, sensor manager, window/clipboard/filesystem/
│                   error/process sensors
├── workspace/      WorkspaceManager — window/project awareness, Xbox polling
├── routines/       Morning routine service + calendar event trigger engine
├── integrations/   Google Calendar adapter, Home Assistant verifier
├── services/       HUD state writer (Godot dashboard), guardian news,
│                   read-only LAN dashboard, visual state controller
├── policies/       Proactive speech presence gate
└── infra/          config, paths, utils (log_event), llm_router, log_viewer
```

Harness vs capabilities: `core/` owns the runtime loop, session state, routing,
and event emission. Domain behavior lives in the other packages and is invoked
through `ToolRegistry` or explicit service objects wired in
`core/main.py::PrometheusCore.startup()`.

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
the same overrides, and answers via `llm_router.chat_completion()` when voice
is unavailable.

The deterministic path is always preferred. All 71 tool actions are enumerated
in `ACTION_ENUM` (execution/tools.py); nothing executes via voice outside that
set.

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
    │ (execution/coding_agent.py, planning/orchestrator.py)
    ▼
Git checkpoint (execution/git_safety.py — commits repo before the run)
    ▼
Claude CLI (coding) or Planner → Executor → Verifier loop (general tasks,
    ProcessPoolExecutor pinned to cores 4-7, execution/background_worker.py)
    ▼
Success criteria evaluated → retry (≤3) → rollback to checkpoint on failure
    ▼
Result → WorkingMemory → spoken announcement (presence-gated)
```

## Event Flow

```
sensors (window/clipboard/filesystem/error/process, async polling)
    ▼
EventBus.publish (sensors/event_bus.py, typed events, priorities)
    ▼
subscribers: core/main.py (session context refresh), context/world_model.py
```

HUD: `services/hud_state_writer.py` composes assistant state + calendar +
news + system metrics and atomically writes
`<ecosystem>/state/dashboard_state.json` once per minute (state changes
immediately); the Godot dashboard (`../Frontend_Dashboard`) only reads that
file. Visual states: idle/armed, listening, processing, speaking,
background_working. `services/visuals.py` writes `~/.jarvis/visual_state.json`
for the same purpose at higher frequency.

## Memory Model

| Store | File(s) | Purpose | Retention |
|-------|---------|---------|-----------|
| WorkingMemory | `~/.jarvis/working_memory.json` | current session context, last tool result, chat handoff | overwritten continuously |
| Episodic | `~/.jarvis/memory_v2/` | session/event history | append; summarized |
| Semantic | `~/.jarvis/memory_v2/` | durable facts | append/update |
| Procedural | `~/.jarvis/memory_v2/` | learned routines | append/update |
| Vault | `$VAULT_PATH/data/memory.db` (external Obsidian corpus) | long-term personal memory, 32K+ chunks | queried at runtime via FTS5 (`memory_core.query_vault`), never duplicated |
| Session summaries | vault markdown | end-of-session wrap-up written by session_summarizer | permanent |

Logs (`~/.jarvis/logs/*.jsonl`, via `infra/utils.log_event`) are diagnostics,
not memory: one file per day, queried by `run_diagnostics` and the trace
debugger, safe to delete.

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
  explicit approval (`lumen_calendar_executor --approve` then
  `--execute-approved`); reads are unrestricted.
- **Home Assistant** — deterministic script allowlist (`HARDCODED_HA_SCRIPTS`);
  post-execution state verification; destructive actions require confirmation.
- **Proactive speech** — `policies/proactive_speech_policy.py` gates all
  unprompted speech on user presence (locked/idle ⇒ suppressed).

## Adding or Modifying a Capability

1. Add the action name to `ACTION_ENUM` in `execution/tools.py` and implement
   its handler in `ToolRegistry._execute_one_inner`. Return a `ToolResult`
   with an honest status.
2. If the LLM should follow up after the tool, add it to
   `core/tool_followups.FOLLOWUP_ACTIONS`.
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

- `tests/` — hermetic pytest suite (~1700 tests); no network, no real claude
  CLI, no real repo commits.
- `tests/acceptance/test_daily_readiness.py` — 11 scored gates, run via
  `scripts/prometheus_daily_readiness.sh`.
- Manual device-touching diagnostics live in `scripts/` (morning routine,
  HA scripts, audio sink).

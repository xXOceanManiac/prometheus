# Prometheus — Architecture Reference

A local-first, voice-driven desktop assistant with deterministic tool routing,
persistent memory, and ambient workspace awareness.

---

## Top-Level Structure

```
prometheus/
├── core/           Voice pipeline, session management, identity, intent routing
├── context/        World model, contextual intent, mission state, workspace awareness
├── planning/       Planner, executor, verifier, orchestrator, decision router
├── execution/      Tool registry, coding agent, background worker, workspace policy
├── memory/         All memory subsystems (episodic, semantic, working, vault)
├── sensors/        Sensor manager, event bus, input sensors
├── ui/             Desktop HUD (PyQt6)
├── voice/          Audio I/O, push-to-talk, wake word
├── infra/          Utils, config, LLM router
├── agents/         Architect, Coder, Tester, Debugger autonomous agents
├── workspace/      Workspace manager and path index
└── legacy/         Compatibility shims
```

Root-level `.py` files are the implementation; `prometheus/domain/module.py` files
are thin re-exports providing the `prometheus.*` namespace for clean imports.

---

## System Flow Overview

```
Microphone audio
    │
    ▼
MicRecorder (audio.py)
    │  PCM16 chunks
    ▼
RealtimePrometheusClient (realtime_client.py)
    │
    ├─► _direct_intent_override()      ← deterministic, zero-latency
    │       │ phrase pattern match
    │       ▼
    │   _run_direct_tool()             ← ToolRegistry.execute()
    │       │
    │       ▼
    │   _guarded_response_create()     ← LLM speaks the result
    │
    ├─► _contextual_override()         ← vague command resolver (<50ms)
    │       │ ContextualIntentResolver (rule-based, no LLM)
    │       ▼
    │   _run_direct_tool() or _speak_text()
    │
    └─► OpenAI Realtime API            ← LLM path (last resort)
            │ function_call → desktop_action
            ▼
        _handle_tool_call()            ← ToolRegistry.execute()
            │
            ▼
        _guarded_response_create()     ← LLM speaks the result
```

---

## Module Responsibilities

### core/
| Module | Responsibility |
|--------|---------------|
| `realtime_client.py` | WebSocket connection to OpenAI Realtime API; session lifecycle; audio streaming; tool call dispatch |
| `main.py` | Entry point; process orchestration; mic recorder; workspace polling; background worker pool |
| `prometheus_identity.py` | Dynamic system prompt builder from workspace + vault + profile |
| `prometheus_profile.py` | Daily-cached user profile; active projects; patterns |
| `session_briefing.py` | Startup briefing; session history loading |
| `intent_overrides.py` | Extracted phrase registries and `resolve_direct_intent()` |
| `session_context.py` | Extracted `build_instructions()` and `build_live_state_block()` |
| `tool_followups.py` | Extracted `FOLLOWUP_ACTIONS` constant |

### context/
| Module | Responsibility |
|--------|---------------|
| `world_model.py` | Real-time snapshot: active window, git state, errors, processes, selected text |
| `contextual_intent.py` | Vague command resolver ("fix that", "continue", "what's wrong") |
| `mission_state.py` | Persistent mission/subtask/blocker tracking |
| `cognition.py` | Higher-order reasoning helpers |
| `workspace/workspace_manager.py` | Window tracking, project inference, Xbox HA polling |

### planning/
| Module | Responsibility |
|--------|---------------|
| `planner/planner.py` | Rule-based fast path + Ollama/OpenAI LLM fallback; builds `Plan` with `PlanStep` list |
| `planner/executor.py` | Runs plan steps via ToolRegistry; 3-retry per step; intermediate WorkingMemory writes |
| `planner/verifier.py` | Checks `all_ok`; builds correction context for Executor retry |
| `planner/decision_router.py` | Routes intents to appropriate planning or execution path |
| `orchestrator.py` | Architect → Coder → Tester → Debugger pipeline for autonomous feature building |

### execution/
| Module | Responsibility |
|--------|---------------|
| `tools.py` | `ToolRegistry` with 59 `ACTION_ENUM` actions; all local machine control |
| `coding_agent.py` | Autonomous coding loop: runs Claude CLI, evaluates success criteria, retries, rolls back |
| `background_worker.py` | ProcessPoolExecutor pinned to CPU cores 4-7; task file tracking; completion callbacks |
| `workspace_policy.py` | `WORKSPACE_ROOT` and `resolve_workspace_path()`: enforces write-only-to-workspace boundary |

### memory/
| Module | Responsibility |
|--------|---------------|
| `memory.py` | Primary named-context store |
| `memory_core.py` | Shared utilities; `query_vault()` FTS5 search over 32K+ Obsidian chunks |
| `working_memory.py` | Temporary active context; last request, tool result, active goal |
| `episodic_memory.py` | Session/event memory |
| `semantic_memory.py` | Facts, concepts, stable knowledge |
| `procedural_memory.py` | Routines, workflows, repeated procedures |
| `session_summarizer.py` | End-of-session vault write; `trigger_wrapup()` |

### sensors/
| Module | Responsibility |
|--------|---------------|
| `sensor_manager.py` | Coordinates all sensor polling |
| `event_bus.py` | Typed pub/sub event bus; priority levels |
| `sensors/window_sensor.py` | Active window tracking via xdotool |
| `sensors/clipboard_sensor.py` | Primary selection monitoring via xclip |
| `sensors/filesystem_sensor.py` | File change events via inotifywait |
| `sensors/error_sensor.py` | Error/exception detection in log streams |
| `sensors/process_sensor.py` | Dev process monitoring |

### voice/
| Module | Responsibility |
|--------|---------------|
| `audio.py` | `MicRecorder` (PCM input), `Speaker` (PCM playback), format conversion |
| `ptt.py` | Push-to-talk controller (ESC / ALT keys) |
| `wakeword.py` | Optional Porcupine wake word detector |

### infra/
| Module | Responsibility |
|--------|---------------|
| `config.py` | `CONFIG` dict; deep-merge of defaults with `~/.jarvis/config.json` |
| `utils.py` | `log_event()`, `command_exists()`, `run_cmd()`, `notify()` |
| `llm_router.py` | Ollama-first, OpenAI fallback; `chat_completion()`, `get_llm()` |

---

## Voice Flow (detailed)

```
1. Audio captured by MicRecorder
2. Sent as PCM16 chunks via send_audio() → OpenAI Realtime API
3. On transcription.completed event:
   a. _direct_intent_override(transcript)
      - Calls resolve_direct_intent() from prometheus/core/intent_overrides.py
      - Pure function; phrase pattern matching; ~0ms latency
      - Returns direct_tool or vault_recall intent, or None
   b. If direct_tool → _run_direct_tool(payload)
      - ToolRegistry.execute(payload)
      - WorkingMemory.set_tool_result()
      - _guarded_response_create() with action-specific response_instructions
   c. If vault_recall → _handle_vault_recall(query)
      - query_vault() → inject_vault_context() → _update_session_instructions()
      - _guarded_response_create()
   d. _contextual_override(transcript)
      - ContextualIntentResolver.resolve(transcript, snapshot, mode="fast")
      - Rule-based only; no LLM; handles "fix that", "continue", "what's wrong"
   e. No override matched → pass to Realtime API LLM
      - Injects _build_live_state_block() as system message
      - _guarded_response_create() with no instructions override
4. LLM may call desktop_action → _handle_tool_call()
5. Speaker plays PCM audio as it streams
```

---

## Planner Flow

```
User intent (voice or background task)
    │
    ▼
Planner.build(intent, context)
    │
    ├─► Rule-based fast path (regex + keyword matching)
    │   confidence ≥ 0.6 → Plan with PlanStep list
    │
    └─► Ollama/OpenAI LLM fallback
        confidence < 0.6 → clarification request
    │
    ▼
Executor.run(plan)
    │  Per step: ToolRegistry.execute() → WorkingMemory write
    │  3 retries per step
    │
    ▼
Verifier.check(result, intent)
    │
    ├─► all_ok → done, notify user
    └─► failure → correction context → Executor retry (max 3 cycles)
```

---

## Event Flow (sensors)

```
Sensor polling (async background tasks)
    │
    ├─ WindowSensor      → EventType.WINDOW_CHANGED
    ├─ ClipboardSensor   → EventType.CLIPBOARD_CHANGED
    ├─ FilesystemSensor  → EventType.FILE_CHANGED
    ├─ ErrorSensor       → EventType.ERROR_DETECTED (high priority)
    └─ ProcessSensor     → EventType.PROCESS_STARTED / STOPPED
    │
    ▼
EventBus.publish(event)
    │
    ▼
Subscribers (world_model.py, WorkspaceManager, etc.)
```

---

## Safety Boundaries

### Workspace write safety
All `write_file` actions route through `workspace_policy.resolve_workspace_path()`:
- Relative paths → `~/PROMETHEUS/workspace/<path>`
- Absolute paths outside workspace → `PermissionError`, logged as `write_file_blocked`
- Path traversal → blocked
- `~/.jarvis`, `/etc`, `/tmp` → blocked

### Tool execution safety
- `run_shell`: token whitelist (18 allowed first tokens); per-command restrictions
- `run_python`: blocks `os.remove`, `shutil.rmtree`, `rm `, `eval()`, `exec()`, `subprocess`
- `git_commit`: always requires `confirmed=True`
- Destructive Home Assistant changes: require confirmation

### Response guard
`_guarded_response_create()` blocks duplicate `response.create` while one is in flight.
Resets on `response.done`, `response.cancelled`, `response.failed`.

---

## Deterministic vs LLM Systems

| System | Type | Latency |
|--------|------|---------|
| `_direct_intent_override()` | Deterministic | ~0ms |
| `_contextual_override()` | Deterministic (rule-based) | <50ms |
| `ToolRegistry.execute()` | Deterministic | varies |
| OpenAI Realtime API LLM | LLM | 200-800ms |
| Planner rule-based path | Deterministic | <10ms |
| Planner LLM fallback | LLM | 1-5s |
| CodingAgent | LLM (Claude Code CLI) | 30-300s |
| Orchestrator | Multi-LLM pipeline | minutes |

The deterministic path is always preferred. LLM is only invoked when no deterministic
override matches, or when planning requires language understanding.

---

## Architectural Rules

1. **Preserve modular boundaries** — `execution/` does not import from `core/`; `memory/` does not import from `sensors/`
2. **Deterministic where possible** — phrase matching beats LLM routing for known commands
3. **Bounded behavior** — all tool actions are enumerated in `ACTION_ENUM`; nothing executes outside this set via voice
4. **Reliability over novelty** — the voice path must never crash; errors are caught, logged, and surfaced gracefully
5. **Avoid giant prompt architecture** — context is injected selectively (workspace, vault, working memory), not as one monolithic blob
6. **Workspace write safety** — code and build output goes to `~/PROMETHEUS/workspace/` only
7. **No silent failures** — all tool results are logged; all errors are caught and reported
8. **Memory is append-not-overwrite** — existing context is preserved; new context is merged

---

## Workspace Safety Model

```
~/PROMETHEUS/workspace/          ← WORKSPACE_ROOT (all file writes land here)
    .prometheus_workspace.json   ← manifest: registered_projects, created_at
    <project>/                   ← coding agent output, build artifacts
```

`write_file` action:
- Relative path `"foo/bar.py"` → `~/PROMETHEUS/workspace/foo/bar.py`
- Absolute path inside workspace → allowed
- Any path outside workspace → blocked, logged, ToolResult(ok=False)

---

## Runtime Processes

Three independent processes; started via `prometheus.sh start`:

1. **`main.py`** — core assistant: voice, tools, memory, workspace polling, background workers
2. **`jarvis_desktop_hud.py`** — PyQt6 HUD; polls 6 JSON files at 120ms; closing stops all processes
3. **`gesture_control/gesture_service.py`** — standalone gesture subsystem

State files read by HUD: `~/.jarvis/visual_state.json`, `~/.jarvis/heartbeat.json`,
`~/.jarvis/audio_levels.json`, `~/.jarvis/activity.jsonl`, `~/.jarvis/background_tasks.json`,
`~/.jarvis/agents.json`

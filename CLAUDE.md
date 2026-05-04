# CLAUDE.md — Prometheus

This file is the north star for Claude Code when working in this repository.
Read it fully before making any changes.

---

## Identity

This project is **Prometheus** — a local-first, voice-driven, autonomous desktop assistant
built for real control over a Linux machine and connected systems.

Prometheus is not a chatbot. It is an ambient operating system companion that:
- Listens and executes with low latency
- Knows what is happening on the machine without being asked
- Plans and executes complex multi-step tasks in the background
- Remembers the user across sessions using a personal memory corpus
- Controls the local environment, smart home, and connected systems
- Grows more capable without becoming fragile

The long-term vision is a private, persistent, context-aware assistant that can operate
the user's digital environment, resume routines, coordinate autonomous workflows safely,
and feel like a composed technical presence — not a verbose AI assistant.

---

## System Environment

- **OS:** Ubuntu / KDE Plasma (primary), Windows dual-boot (secondary)
- **Hardware:** Ryzen 7, 64GB DDR4, 1TB SSD, AMD integrated graphics
- **Python:** 3.12
- **Runtime:** Local desktop, three-process model (see below)
- **Voice API:** OpenAI Realtime API over WebSocket
- **Smart home:** Home Assistant via REST API

---

## Product Goals — Priority Order

1. **Ambient awareness** — Prometheus knows what is open, what project is active,
   and what the user is doing without being told.
2. **Low-latency voice interaction** — commands feel immediate.
3. **Deterministic tool execution** — known actions route directly, no LLM guessing.
4. **Autonomous background execution** — complex tasks run in the background using
   spare CPU/RAM without affecting the active session.
5. **Persistent memory** — personal context, project state, and learned patterns
   survive across sessions, backed by the Obsidian vault corpus.
6. **Local machine control** — files, apps, workspaces, shell commands.
7. **Smart home control** — Home Assistant scripts, deterministic execution.
8. **Safe execution** — destructive or irreversible actions require confirmation.
9. **Visual desktop presence** — HUD reflects real assistant state accurately.
10. **Extensibility** — new tools and capabilities slot in cleanly.

---

## Engineering Priorities

When improving this codebase, work in this order:

1. **Stability** — no crashes, no broken async state, no corrupted memory files.
2. **Latency** — voice commands must feel immediate.
3. **Determinism** — known commands route directly to tools.
4. **Safety** — destructive or external actions require confirmation.
5. **Awareness** — workspace, window, and project context is always current.
6. **Memory** — important context persists and is retrievable.
7. **Background execution** — tasks run without touching the active session.
8. **HUD polish** — visual state reflects actual assistant state.
9. **Extensibility** — new tools are easy to add.
10. **Maintainability** — architecture stays readable and avoids unnecessary rewrites.

---

## Runtime Model

Three independent processes. Do not collapse them unless the task is specifically
about process orchestration.

**Preferred — via systemd (autostarted on login):**
```bash
./prometheus.sh start        # start both core + HUD
./prometheus.sh stop         # stop both
./prometheus.sh restart      # restart both
./prometheus.sh status       # show service status
./prometheus.sh logs         # follow live logs
```

**Manual (dev / debugging):**
```bash
# Core assistant — voice, tool execution, memory, background workers
source .venv/bin/activate
python3 main.py

# Desktop HUD — separate PyQt6 window
python3 jarvis_desktop_hud.py

# Gesture control — run from its subdirectory
cd gesture_control
python3 gesture_service.py
```

**Process responsibilities:**
- `main.py` — assistant brain, audio loop, Realtime client, tool execution, workspace awareness, background worker pool, session summarizer, PID lock (`~/.jarvis/prometheus.pid`), heartbeat writer.
- `jarvis_desktop_hud.py` — visual desktop interface, polls `~/.jarvis/visual_state.json`, `~/.jarvis/background_tasks.json`, `~/.jarvis/heartbeat.json`. Closing the HUD stops all Prometheus processes.
- `gesture_control/gesture_service.py` — gesture subsystem, standalone.

---

## Architecture

### Entry point: `main.py`

`JarvisV4` (rename to `PrometheusCore` when refactoring) owns the main async loop:

- `MicRecorder` — PCM audio input
- `PushToTalkController` — ESC / ALT key push-to-talk
- `WakeWordDetector` — optional Porcupine wake word
- `RealtimeJarvisClient` — WebSocket to OpenAI Realtime API
- `Speaker` — PCM audio playback
- `VisualStateController` — writes state to `~/.jarvis/visual_state.json`
- `WorkspaceManager` — ambient window/project awareness (see below)
- `BackgroundWorkerPool` — CPU-pinned async workers for background tasks (see below)

The main loop must remain stable. Avoid changes that increase async complexity
unless they directly fix a known issue.

### Voice → Tool flow: `realtime_client.py`

1. Audio streamed to OpenAI Realtime API as PCM16.
2. On `conversation.item.input_audio_transcription.completed`, `_direct_intent_override()`
   checks transcript for known patterns.
3. Known patterns short-circuit directly to local tools — no LLM involved.
4. If no override matches, `response.create` is sent to the LLM.
5. LLM may call `desktop_action` via function calling.
6. `_handle_tool_call()` routes through `ToolRegistry.execute()`.

Direct intent overrides are critical for latency, reliability, and safety.
Do not remove them unless replacing with a faster or safer deterministic routing layer.

### Tool layer: `tools.py`

All tool actions go through a single `desktop_action` function with `ACTION_ENUM`.
`ToolRegistry` wraps this with the JSON schema exposed to the LLM.

### Memory layer

All memory files live under `~/.jarvis/`. Treat memory as a core product layer.

| File | Purpose |
|------|---------|
| `memory.py` | Primary store — named contexts and routines |
| `episodic_memory.py` | Session/event memory |
| `semantic_memory.py` | Facts, concepts, stable knowledge |
| `procedural_memory.py` | Routines, workflows, repeated procedures |
| `working_memory.py` | Temporary active context |
| `dream_manager.py` | Offline summarization and reorganization |
| `behavior_learning.py` | Pattern learning from usage |
| `memory_core.py` | Shared utilities — `read_json`, `write_json`, `norm_text` |

When modifying memory:
- Preserve existing context — never destructively overwrite
- Prefer append/update over full replacement
- Validate all JSON writes
- Avoid file corruption on interrupt
- Keep long-term memory searchable

### Obsidian vault memory corpus

The user has a structured Obsidian vault containing 479 ChatGPT conversations,
28,000+ message nodes, and 32,000+ retrieval chunks, exported as JSONL with
a SQLite FTS5 index (`data/memory.db`).

This corpus is the user's **personal long-term memory** and must be treated as such.

Integration path:
- `memory_core.py` exposes a `query_vault(text: str) -> list[dict]` function
  that queries the SQLite FTS5 index.
- Prometheus calls `query_vault()` at session start based on active workspace context.
- Results are injected into `WorkingMemory` for the session.
- New important context is written back to the vault as markdown after sessions.
- Vault path: configurable via `~/.jarvis/config.json` under `vault_path`.

Do not embed or duplicate the vault contents. Query it at runtime.

---

## WorkspaceManager — Ambient Awareness

This is the highest-priority scaffolded subsystem to implement.

**What it must do:**
- Detect the currently focused window and application using `wmctrl` or `python-xlib`.
- Infer the active project from window title, process name, and working directory.
- Track open windows and recently active apps.
- Write current workspace state to `~/.jarvis/visual_state.json` continuously.
- Feed context into `WorkingMemory` so Prometheus always knows what the user is doing.

**Implementation notes:**
- Use `wmctrl -l` for window listing on KDE Plasma.
- Use `xdotool getactivewindow getwindowname` for focused window title.
- Poll every 2–5 seconds in a background async task — do not block the main loop.
- Map known window titles/paths to project names in `~/.jarvis/config.json`.
- Gracefully handle missing tools (wmctrl not installed, X11 not available).

**State written to `~/.jarvis/visual_state.json`:**
```json
{
  "state": "armed",
  "active_window": "VS Code — Prometheus",
  "active_project": "prometheus",
  "active_project_path": "/home/tatel/Desktop/Prometheus",
  "open_windows": ["VS Code", "Firefox", "Konsole"],
  "updated_at": "2026-05-03T14:22:00"
}
```

---

## Background Worker Pool

Prometheus must be able to execute complex multi-step tasks in the background
without affecting voice latency or active session performance.

**Design requirements:**
- Use `concurrent.futures.ProcessPoolExecutor` pinned to specific CPU cores via `psutil`.
- Budget: 4 cores, 12GB RAM maximum for background workers.
- Main audio loop and voice processing are never on worker cores.
- Workers run the Planner → Executor → Verifier loop.
- Workers report status back to `WorkingMemory` and optionally speak a completion notice.
- Workers are gracefully cancelled on shutdown.

**Worker task lifecycle:**
1. User (voice or command) submits a task: *"Research X and write a summary to my vault."*
2. `BackgroundWorkerPool.submit(task_description, context)` is called.
3. Planner builds a `Plan` with concrete `PlanStep` list.
4. Executor runs each step, writing intermediate results.
5. Verifier checks output against intent.
6. On failure, Executor retries with corrected context (max 3 attempts).
7. On completion, result is written to memory and user is notified.

**Safety:**
- Background workers never take destructive actions without confirmation.
- Workers never modify files outside designated project paths without explicit permission.
- Worker resource usage is monitored and capped.

---

## Planner / Executor / Verifier

The scaffolded `planner/` subsystem must be built out as follows:

### `Planner`
- `build(intent, context) -> Plan` must produce real steps, not an empty list.
- Plans are built from intent + workspace context + memory retrieval.
- Steps map directly to `ToolRegistry` actions or shell commands.
- Confidence < 0.6 triggers a clarification request before execution.

### `Executor`
- Runs each `PlanStep` in order via `ToolRegistry.execute()` or subprocess.
- Captures output and errors per step.
- Passes results forward as context for subsequent steps.
- Writes intermediate state to `WorkingMemory`.

### `Verifier`
- Checks that the final output matches the original intent.
- On failure: returns a corrected context dict to the Executor for retry.
- Max 3 retry cycles before surfacing failure to the user.
- Verification is lightweight — not a full LLM call for simple tool actions.

---

## Setup

```bash
bash setup/bootstrap.sh

# Required .env values
OPENAI_API_KEY=...
HOME_ASSISTANT_API_KEY=...
HOME_ASSISTANT_URL=http://homeassistant.local:8123

# Optional
PORCUPINE_ACCESS_KEY=...
VAULT_PATH=/path/to/Tates_Brain/data
```

Runtime config: `~/.jarvis/config.json` — deep-merges with `DEFAULT_CONFIG` in `config.py`.

Nested keys merged: `apps`, `urls`, `modes`, `projects`, `routines`, `vault_path`.
All other keys overwritten by user config.

Do not place secrets in tracked config files.

---

## Home Assistant Integration

Scripts mapped in `HARDCODED_HA_SCRIPTS` in `tools.py`.

Naming convention: `jarvis_<domain>_<intent>_<detail>`

HA entity format: `script.jarvis_<domain>_<intent>_<detail>`

Auth: `HOME_ASSISTANT_API_KEY` via env. Never hardcode tokens or URLs.

When adding scripts:
- Confirm the entity exists in Home Assistant before assuming Prometheus is broken.
- Test with direct phrase matching before LLM function call path.

---

## HUD: `jarvis_desktop_hud.py`

Standalone PyQt6 window. Polls 6 data files every 120ms:
- `~/.jarvis/visual_state.json` — assistant state, workspace, active tab
- `~/.jarvis/audio_levels.json` — audio activity (mic/speaker levels)
- `~/.jarvis/activity.jsonl` — recent log events shown as activity feed
- `~/.jarvis/background_tasks.json` — background task cards (OPS tab)
- `~/.jarvis/agents.json` — agent cards (AGENTS tab, stub)
- `~/.jarvis/heartbeat.json` — alive indicator (written every 5s by core)

**Tab system** — three tabs in the header bar (MAIN / OPS / AGENTS). Tab state persists via `active_hud_tab` in `visual_state.json`.

**Header bar controls (right side, left to right):**
- Status dot — green/red heartbeat indicator (<15s = green). No longer triggers restart.
- ↺ restart button — teal, 24×24px. Shows "Restart Prometheus core?" confirmation before running `systemctl --user restart prometheus`. Does NOT stop the HUD.
- MAIN / OPS / AGENTS tab buttons.

**Compact mode** (window ≤50% screen width AND height):
- Header stays identical — same tabs, restart button, status dot.
- Content area shows PROMETHEUS title + state (left) and 3 circular gauges (right): CPU, RAM, NET stacked vertically with 12px gaps, 8px window margin, 48–64px diameter.
- Center divider line separates left/right panels.
- No logs, no OPS cards, no AGENTS in compact mode.
- Window gets `WindowStaysOnTopHint` in compact mode so it stays visible.
- Tab switching works in compact — tab takes effect immediately when expanded.

**Close behavior:** closing the HUD window runs `systemctl --user stop prometheus prometheus-hud` (3s timeout, then force-kill). If background tasks are running, shows a confirmation dialog first.

**State colors:**
- `idle` / `armed` → blue
- `processing` → purple
- `speaking` → orange
- `background_working` → teal

When modifying the HUD:
- Preserve compact/full-size responsiveness.
- `store.refresh()` is called from `data_timer` (120ms), NOT from `paintEvent`.
- No heavy computation in paint events.
- `setWindowFlags` requires `show()`+`raise_()` to take effect — always call both.
- The HUD must never crash if any JSON files are missing.

---

## Gesture Control: `gesture_control/`

Standalone subsystem. Pipeline:
`CameraInput → HandTracker → GestureEngine → MouseRouter → OverlayHUD`

Uses MediaPipe. Model at `gesture_control/models/hand_landmarker.task`.

Not integrated with the main Prometheus loop unless explicitly added.

---

## Subsystem Status

| Subsystem | Status | Notes |
|-----------|--------|-------|
| `workspace/WorkspaceManager` | Functional | Window tracking + Xbox HA polling with backoff |
| `workspace/PathIndex` | Functional | `rglob` path search |
| `planner/Planner` | Implemented | Rule-based fast path + Ollama/OpenAI LLM fallback |
| `planner/Executor` | Implemented | 3 retries, intermediate WorkingMemory writes, `on_step` callback |
| `planner/Verifier` | Implemented | Checks `all_ok`, builds correction context for retry |
| `BackgroundWorkerPool` | Implemented | ProcessPoolExecutor, CPU-pinned (cores 4-7), task file tracking; per-task `completion_callback` supported |
| Background task verbal notification | Implemented | `_announce_background_task_complete` in `main.py`; speaks result on idle, skips if busy |
| App variant normalization | Implemented | `_APP_OPEN_VARIANTS` in `realtime_client.py`; "vs code", "visual studio", "my files", "file explorer" aliases in `tools.py` |
| `open_app` false-positive fix | Implemented | Requires BOTH pgrep + wmctrl to confirm "already running"; removed `-x` flag |
| `memory_core.query_vault` | Implemented | SQLite FTS5 over `data/memory.db` (32,521 chunks); startup query uses project name + window title |
| `session_summarizer` | Implemented | Writes vault markdown on shutdown; `trigger_wrapup()` for voice-triggered wrap-up |
| `llm_router` | Implemented | Ollama-first (mistral), OpenAI fallback |
| `perception/` | Partially implemented | Inspect before building on |
| HUD tab system | Implemented | MAIN/OPS/AGENTS tabs, status dot, heartbeat indicator |
| Systemd services | Installed | `prometheus.service`, `prometheus-hud.service` |
| KDE packaging | Installed | App launcher entry, autostart, icon at all sizes |
| `Speaker.force_stop()` | Implemented | Calls `sd.stop()` before acquiring lock; used by `_interrupt_assistant()` with 50ms pre-interrupt sleep |
| Session instructions debug log | Implemented | `session_instructions_debug` event replaces `vault_injection_debug`; logs `has_vault`, `has_workspace`, `vault_titles` |
| Web search empty result | Implemented | Speaks "couldn't find a clear answer" instead of hallucinating when summary is empty |
| Xbox watching awareness | Implemented | "what am I watching" / "what's on xbox" phrases route to `screen_context` override |
| `prometheus_identity.build_system_prompt()` | Implemented | Dynamic prompt from workspace + vault + sessions + profile + time-of-day |
| `prometheus_profile.PrometheusProfile` | Implemented | Daily cached profile; reads active_projects from vault session files |
| `session_briefing.SessionBriefing` | Implemented | 3s delayed briefing on startup; cancellable if user speaks first |
| `proactive_loop.ProactiveLoop` | Implemented | 90s ambient awareness; LLM decides if worth surfacing; 10-min per-category cooldown |
| `run_python / run_shell / search_codebase / git_status / git_diff / git_commit` | Implemented | Code execution tools; git_commit always requires confirmed=True |
| `session_wrapup / system_status / get_priorities` | Implemented | Session tools; voice-triggered via direct intent overrides |

**Before modifying any subsystem, read the actual code.**
Do not assume scaffolded or listed functionality is complete without reading.

---

## Adding a New Tool Action

1. Add action name string to `ACTION_ENUM` in `tools.py`.
2. Implement handler in `ToolRegistry.execute()`.
3. If LLM follow-up is needed, add to `followup_actions` in `_handle_tool_call()` and `_run_direct_tool()`.
4. Optionally add phrase matching in `_direct_intent_override()` for latency-sensitive commands.
5. Test both paths: direct intent override AND LLM function call.

Note: `git_commit` always requires `confirmed=True` in payload regardless of any other setting.
Note: Direct intent overrides now include: wrap-up phrases, "what do you know", "what should I focus on", "search codebase", and "check git" / "what changed".

---

## Quality Standards

All code written for Prometheus is production quality. Not prototypes.

- Full error handling on every function — no silent failures.
- Input validation on all external data.
- No `TODO` comments left in delivered code.
- No hardcoded values — use env vars or config.
- Logging on all critical paths via `log_event()`.
- Write tests alongside implementation for new subsystems.
- No broad recursive filesystem operations without explicit scope.
- No uncontrolled async complexity added to the main loop.

---

## Safety Rules

Prometheus controls real local and network-connected systems.

**Always require confirmation before:**
- Deleting files
- Overwriting important files
- Running destructive shell commands
- Shutting down or restarting the machine
- Making irreversible Home Assistant changes
- Sending external messages
- Exposing secrets
- Modifying config in a way that could break startup
- Closing the HUD window when background tasks are running

**Never expose or log:**
- `OPENAI_API_KEY`
- `HOME_ASSISTANT_API_KEY`
- `PORCUPINE_ACCESS_KEY`
- Any token, private URL, or local credential

**For filesystem operations:**
- Prefer safe reads before writes
- Create backups before overwriting important files
- Validate paths — never assume CWD is safe
- Avoid broad recursive operations unless explicitly requested

**For shell commands:**
- Avoid destructive commands unless explicitly requested and confirmed
- Explain risky commands before executing
- Prefer narrow, testable commands

---

## Do Not Do

- Do not rewrite large parts of the architecture unless explicitly asked.
- Do not replace the Realtime API flow without preserving current voice/tool behavior.
- Do not remove deterministic direct intent overrides without a faster/safer replacement.
- Do not add cloud dependencies for local memory unless explicitly requested.
- Do not make destructive filesystem, OS, or Home Assistant changes without confirmation.
- Do not break the three-process runtime model unless the task is specifically about process orchestration.
- Do not modify tracked example configs with private secrets.
- Do not assume scaffolded subsystems are functional.
- Do not make Prometheus verbose by default.
- Do not prioritize abstract architecture over working behavior.
- Do not silently swallow errors.
- Do not introduce major dependencies without a clear reason.
- Do not collapse the Obsidian vault into Prometheus's memory files — query it at runtime.

---

## Preferred Behavior

Prometheus is a composed technical aide.

**Behavior goals:**
- Fast acknowledgement
- Short responses by default
- Deterministic execution
- Calm clarification when needed
- No rambling, no unnecessary apologies, no fake certainty

**Preferred responses:**
- `Confirmed.`
- `Opening it now.`
- `Done.`
- `I need one detail before I do that.`
- `That will overwrite files. Confirm?`
- `Background task started. I'll let you know when it's done.`
- `Task complete. Summary written to vault.`

**Avoid:**
- `Sure thing! I'd be happy to help you with that. Let me just...`

---

## Debugging Order

1. Is the process running?
2. Is the virtual environment active?
3. Are required environment variables loaded?
4. Is the Realtime WebSocket connected?
5. Is audio input working?
6. Is audio playback working?
7. Did transcription complete?
8. Did direct intent override trigger?
9. Did the LLM call `desktop_action`?
10. Did `ToolRegistry.execute()` receive valid arguments?
11. Did the external service respond?
12. Did visual state update correctly?
13. Is WorkspaceManager polling correctly?
14. Is the background worker pool alive and within resource budget?

**Common failure areas:**
- Duplicate tool calls
- `conversation_already_has_active_response`
- Async response state conflicts
- Missing environment variables
- Home Assistant connection/auth failures
- Broken audio devices
- PyQt HUD polling stale state
- JSON memory corruption
- Schema mismatch in Realtime tool definitions
- `wmctrl` or `xdotool` not installed for workspace awareness
- Background task worker crash (check `~/.jarvis/background_tasks.json` for failed entries)
- Heartbeat goes red (HUD status dot) — core process died, click dot or run `./prometheus.sh restart-core`
- `vault_path` missing from `~/.jarvis/config.json` — vault queries silently return empty
- "App already running" false positive — fixed: requires BOTH pgrep + wmctrl to confirm before returning "already open"; if only pgrep matches but wmctrl finds no window, launches fresh
- Web search empty result hallucination — fixed: when summary is empty, speaks "I searched but couldn't find a clear answer for that" instead of inventing an answer

---

## Important Files

**Core assistant:**
`main.py`, `realtime_client.py`, `tools.py`, `config.py`, `audio.py`, `speaker.py`, `visuals.py`
`prometheus_identity.py` — dynamic system prompt builder
`prometheus_profile.py` — personal profile and daily patterns
`session_briefing.py` — startup briefing and session history loading
`proactive_loop.py` — ambient awareness loop (90s cycle)

**Background execution:**
`background_worker.py`, `llm_router.py`, `session_summarizer.py`

**Assets:**
`assets/prometheus_icon.png` — app icon (512×512, used by all .desktop files via absolute path)

**Memory:**
`memory.py`, `memory_core.py`, `episodic_memory.py`, `semantic_memory.py`,
`procedural_memory.py`, `working_memory.py`, `dream_manager.py`, `behavior_learning.py`

**Workspace & planning:**
`workspace/workspace_manager.py`, `workspace/path_index.py`,
`planner/planner.py`, `planner/executor.py`, `planner/verifier.py`

**HUD:**
`jarvis_desktop_hud.py`

**Gesture:**
`gesture_control/gesture_service.py`, `gesture_control/models/hand_landmarker.task`

**System management:**
`prometheus.sh`, `~/.config/systemd/user/prometheus.service`,
`~/.config/systemd/user/prometheus-hud.service`,
`~/.local/share/applications/prometheus.desktop`,
`~/.config/autostart/prometheus.desktop`

**Config / runtime:**
`.env`, `~/.jarvis/config.json`, `~/.jarvis/visual_state.json`,
`~/.jarvis/audio_levels.json`, `~/.jarvis/background_tasks.json`,
`~/.jarvis/agents.json`, `~/.jarvis/heartbeat.json`,
`~/.jarvis/prometheus.pid`, `~/.jarvis/memory_v2/`

**Vault corpus (external):**
`$VAULT_PATH/data/memory.db`, `$VAULT_PATH/data/chunks.jsonl`

---

## Development Philosophy

Make Prometheus more capable without making it fragile.

**Prefer:**
- Small, reliable improvements
- Clear state management
- Deterministic routing
- Safe tool execution
- Fast user feedback
- Readable code
- Simple architecture that works

**Avoid:**
- Speculative rewrites
- Fragile abstractions
- Uncontrolled autonomy
- Excessive dependencies
- Hidden failure states
- Verbose responses

The best improvement is one that makes Prometheus feel faster, safer, more aware,
or more capable — without breaking what already works.
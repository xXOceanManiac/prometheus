# CLAUDE.md — Prometheus

Read this fully before making changes. For structure, flows, and the
permission model, read `prometheus/ARCHITECTURE.md` — keep both files accurate
when you change the system.

## Identity

Prometheus is a local-first, voice-driven desktop assistant with real control
over this Linux machine and connected systems (Home Assistant, Google Calendar
via Lumen, Godot dashboard). It is a composed technical aide, not a chatbot:
fast acknowledgement, short responses, deterministic execution, no fake
certainty.

## Runtime

- **One process, one entry point:** `main.py` (thin launcher) →
  `prometheus/core/main.py::PrometheusCore`. systemd user service
  `prometheus.service` runs it; manage with `./prometheus.sh
  start|stop|restart|status|logs`.
- The Godot dashboard is launched separately
  (`../Frontend_Dashboard/launch_dashboard.sh`) and only reads
  `../state/dashboard_state.json`.
- `gesture_control/` is standalone and not part of the core runtime.
- Secrets come from `.env` (repo root, git-ignored); runtime config from
  `~/.jarvis/config.json`, deep-merged over `DEFAULT_CONFIG` in
  `prometheus/infra/config.py`. Never hardcode or log
  `OPENAI_API_KEY`, `HOME_ASSISTANT_API_KEY`, `PORCUPINE_ACCESS_KEY`, or any
  token.

## Engineering priorities (in order)

1. **Stability** — the voice loop must never crash; no corrupted memory files.
2. **Latency** — known commands route deterministically
   (`core/intent_overrides.py`); do not remove direct overrides without a
   faster, safer replacement.
3. **Determinism** — every voice-executable action is enumerated in
   `ACTION_ENUM` (`execution/tools.py`).
4. **Safety** — destructive or external actions require confirmation;
   workspace writes go through `execution/workspace_policy.py`; `git_commit`
   always requires `confirmed=True`.
5. **Truthfulness** — respect the `ToolResult` status contract
   (`verified_success` / `accepted_unverified` / failure); never let the
   assistant claim unverified success.

## Testing rules

- `pytest tests/` must stay hermetic: no network, no real claude CLI, no
  commits to this repository. Anything touching git uses the `temp_git_repo`
  fixture from `tests/conftest.py`.
- `./scripts/prometheus_daily_readiness.sh` runs the 11 acceptance gates.
- Scripts in `scripts/test_morning_*.py` trigger real Home Assistant devices —
  never run them from automated tests.
- A passing suite is not proof the assistant works; verify the live path via
  `./prometheus.sh restart` + `./prometheus.sh logs` when you change startup
  or the voice loop.

## Adding a tool action

1. Add the name to `ACTION_ENUM` and a handler in
   `ToolRegistry._execute_one_inner` (`execution/tools.py`).
2. Optional LLM follow-up: `core/tool_followups.py`.
3. Optional fast phrase: `core/intent_overrides.py`.
4. Test both the direct-override path and the LLM function-call path.

## Do not

- Rewrite working subsystems for architectural elegance.
- Add async complexity to the main loop.
- Bypass deterministic safety checks with model output.
- Collapse the Obsidian vault into local memory files — query it at runtime
  (`memory/memory_core.query_vault`).
- Leave TODOs, silent exception swallowing, or duplicate implementations.
- Make Prometheus verbose by default.

## Debugging order

1. `./prometheus.sh status` / `logs` — is the core running, did startup log
   errors (missing env, audio devices, Realtime quota)?
2. `~/.jarvis/logs/*.jsonl` — every tool call and routing decision is logged
   via `log_event`; `scripts/prometheus_trace_debug.py --last 1` prints the
   last trace.
3. Did the direct intent override match, or did it fall through to the LLM?
4. Did `ToolRegistry.execute()` get valid arguments? What `ToolResult` status
   came back?
5. External services: HA reachable? `~/.jarvis/config.json` `vault_path` set?
   Godot dashboard reading a fresh `dashboard_state.json`?

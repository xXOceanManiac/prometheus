# Prometheus Path Repair Report

**Generated:** 2026-05-13  
**Session purpose:** Repair hardcoded paths after project moved from `~/Desktop/Jarvis.v5.1` to `~/Desktop/PROMETHEUS/Prometheus_Main`

---

## Detected Path Constants

| Constant | Resolved Path |
|----------|---------------|
| `PROJECT_ROOT` | `/home/tatel/Desktop/PROMETHEUS/Prometheus_Main` |
| `RUNTIME_ROOT` | `/home/tatel/Desktop/PROMETHEUS/Prometheus_Main/runtime` |
| `REPORTS_DIR` | `/home/tatel/Desktop/PROMETHEUS/Prometheus_Main/runtime/reports` |
| `WORKSPACE_ROOT` | `/home/tatel/Desktop/PROMETHEUS/Prometheus_Main/runtime/workspace` |
| `LOGS_DIR` | `/home/tatel/Desktop/PROMETHEUS/Prometheus_Main/runtime/logs` |
| `JARVIS_STATE_DIR` | `/home/tatel/.jarvis` (user state — unchanged) |

`find_project_root()` walks parent dirs from `paths.py` location until it finds a dir containing both `prometheus/` and `tests/` subdirectories. Result is always relative to file location — survives future moves.

---

## Files Changed

### Created
- `prometheus/infra/paths.py` — central path constants module

### Modified (active code)
- `workspace_policy.py` — replaced `Path.home() / "PROMETHEUS" / "workspace"` with `from prometheus.infra.paths import WORKSPACE_ROOT`
- `tests/audit_prometheus.py` line 38 — replaced `Path.home() / "PROMETHEUS" / "reports" / "current_capability_audit.md"` with `REPORTS_DIR / "current_capability_audit.md"`
- `tests/test_workspace_policy.py` — updated docstring; replaced direct `WORKSPACE_ROOT` import with import from `prometheus.infra.paths`; added 7 `TestPathConstants` tests
- `main.py` — added `ensure_runtime_dirs()` call in `amain()` before startup
- `prometheus/execution/tool_capability_registry.py` line 354 — updated `safe_when` string from `~/PROMETHEUS/workspace/` to `runtime/workspace/`
- `prometheus.sh` line 5 — updated `PROJ` variable to new project path

### Modified (system/launch config)
- `~/.config/systemd/user/prometheus.service` — updated `WorkingDirectory`, `EnvironmentFile`, `ExecStart`
- `~/.config/systemd/user/prometheus-hud.service` — updated `WorkingDirectory`, `ExecStart`
- `~/.local/share/applications/prometheus.desktop` — updated `Icon`, `Exec`
- `~/.config/autostart/prometheus.desktop` — updated `Icon`, `Exec`

---

## Hardcoded Paths Found

### Active paths replaced
| File | Line | Old value | Replacement |
|------|------|-----------|-------------|
| `workspace_policy.py` | 3 | `Path.home() / "PROMETHEUS" / "workspace"` | `WORKSPACE_ROOT` from `prometheus.infra.paths` |
| `tests/audit_prometheus.py` | 38 | `Path.home() / "PROMETHEUS" / "reports" / ...` | `REPORTS_DIR / ...` |
| `prometheus/execution/tool_capability_registry.py` | 354 | `~/PROMETHEUS/workspace/` | `runtime/workspace/` |
| `prometheus.sh` | 5 | `/home/tatel/Desktop/Jarvis.v5.1` | `/home/tatel/Desktop/PROMETHEUS/Prometheus_Main` |
| `prometheus.service` | 9–11 | `/home/tatel/Desktop/Jarvis.v5.1` | `/home/tatel/Desktop/PROMETHEUS/Prometheus_Main` |
| `prometheus-hud.service` | 9–10 | `/home/tatel/Desktop/Jarvis.v5.1` | `/home/tatel/Desktop/PROMETHEUS/Prometheus_Main` |
| `prometheus.desktop` (launcher + autostart) | — | `/home/tatel/Desktop/Jarvis.v5.1` | `/home/tatel/Desktop/PROMETHEUS/Prometheus_Main` |

### Historical paths intentionally left alone
| File | Lines | Reason |
|------|-------|--------|
| `tests/audit_prometheus.py` | 4, 7, 1135–1136 | Docstring and historical issue text in capability table — not path-generating code |
| `tests/test_workspace_policy.py` | 4 (old) | Replaced in docstring update above |
| `tests/test_sensors.py` | 223–266 | Test input data simulating filesystem events; paths are example strings, not generated paths |
| `tests/test_contextual_intent.py` | 63, 149, 259, … | `focused_project_path` values in test context dicts are example input data, not generated paths |
| `tests/test_workflow_selector.py` | 99, 115, 141, … | `active_project_path` values in test context dicts are example input data |
| `cost_tracker.py` | 17 | `Path.home() / ".prometheus"` — user-state dir (like `.jarvis`), not project runtime |
| `launch.py` | 27, 56, 62, 276 | Same `.prometheus` user-state dir — not project runtime |

---

## Test Commands Run

```bash
.venv/bin/python3 -m pytest tests/ -v
.venv/bin/python3 tests/audit_prometheus.py
.venv/bin/python3 tests/score_contextual_intent.py
.venv/bin/python3 tests/score_workflows.py
```

## Pass/Fail Results

| Command | Result |
|---------|--------|
| `pytest tests/` | **470 passed, 0 failed** (1 unrelated collection warning) |
| `audit_prometheus.py` | **120/120 passed** — report written to `runtime/reports/current_capability_audit.md` |
| `score_contextual_intent.py` | **All examples passed** — 100% on all policy fields |
| `score_workflows.py` | **All targets met** — 100% classification, 0% wrong/dangerous, 100% clarification |

---

## Manual Validation

- `runtime/` — exists ✓
- `runtime/reports/` — exists ✓
- `runtime/workspace/` — exists ✓
- `runtime/logs/` — exists ✓
- Write test: `write_file("path_repair_test.txt", ...)` → landed at `runtime/workspace/path_repair_test.txt` ✓
- `~/.jarvis` — untouched ✓
- Systemd daemon reloaded: `systemctl --user daemon-reload` ✓

---

## Remaining Path Risks

1. **`tests/test_sensors.py`, `test_contextual_intent.py`, `test_workflow_selector.py`** — contain `Jarvis.v5.1` as test input data strings. Not a runtime risk. If the old path is deleted these tests still pass (they don't require the path to exist).
2. **`cost_tracker.py` / `launch.py`** — write to `Path.home() / ".prometheus"` for cost tracking and launch logging. This is user-state, not project runtime. Intentionally left as-is per session constraints (treat like `.jarvis`).
3. **Obsidian vault path** — configured via `~/.jarvis/config.json` `vault_path` key. Not touched.
4. **Old `~/Desktop/PROMETHEUS/workspace/` and `~/Desktop/PROMETHEUS/reports/`** — these are the pre-repair output directories at the PROMETHEUS top level. They still exist and contain historical reports. They are not deleted per session constraints. New output goes to `runtime/`.

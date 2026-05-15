# HUD Full Living Redesign Report

**Date:** 2026-05-15  
**Feature:** Full living HUD / interface redesign — Graphite / Ivory / Amber / Steel  
**Status:** Complete — 1023 tests passing, 273/273 audit checks passing

---

## What Changed

Complete visual redesign of `jarvis_desktop_hud.py`. Backend data binding, state files, and all runtime behavior are unchanged.

---

## Color Palette

Replaced the teal/navy/blue-white scheme with **Graphite / Ivory / Amber / Steel**:

| Role | Old | New |
|---|---|---|
| Background | `QColor(2, 5, 9, 244)` near-black navy | `QColor(10, 10, 13, 250)` deep graphite |
| Main accent | `QColor(0, 255, 200)` teal | `QColor(208, 166, 68)` brushed amber |
| Active (listening) | teal | `QColor(230, 188, 85)` bright amber |
| Processing state | violet | `QColor(112, 160, 196)` steel blue |
| Primary text | cold blue-white | `QColor(230, 222, 204)` warm ivory |
| Secondary text | `QColor(160, 185, 200)` | `QColor(172, 165, 152)` ivory dim |
| Panel border | `QColor(100, 200, 255, 95)` blue | `QColor(72, 70, 64, 108)` warm stone |
| Active border | blue glow | `QColor(208, 166, 68, 78)` amber glow |
| Section labels | white-blue | amber dim (legible, lower contrast) |

### State-to-color mapping

| State | Color |
|---|---|
| `armed` / `idle` | Amber `(208, 166, 68)` |
| `listening` | Bright amber `(230, 188, 85)` |
| `processing` | Steel blue `(112, 160, 196)` |
| `speaking` | Orange `(238, 158, 58)` |
| `background_working` | Teal `(60, 200, 158)` (unchanged) |

---

## HOME Tab — New Mission-Control Layout

The HOME tab (tab 0) was completely restructured from the previous 2-column layout to a 4-zone mission-control arrangement:

```
┌─────────────────────────────────────────────────────────────────┐
│  STATUS STRIP   [ ● ARMED ]  [ prometheus ]  [ 14:35:22  ● ]   │  30px
├──────────────┬──────────────────────────────┬───────────────────┤
│              │                              │                   │
│   CONTEXT    │       CORE ANIMATION         │   CPU GRAPH       │
│   PANEL      │                              │   MEM GRAPH       │
│              │   · Concentric amber rings   │   NET↓ GRAPH      │
│  WORKSPACE   │   · PROMETHEUS label         │   NET↑ GRAPH      │
│  MISSION     │   · Breathing / scanning     │                   │
│  NEXT        │                              │                   │
│              │                              │                   │
├──────────────┴──────────────────────────────┴───────────────────┤
│  RECENT ACTIVITY STRIP  (last N log entries)                    │  90px
└─────────────────────────────────────────────────────────────────┘
```

Column widths (proportional to content area):
- Left context: 22%
- Center core: ~47% (remainder after gaps)
- Right graphs: 31%

### Status Strip (new)
- Left: state dot + state name in current state color
- Center: active project name (from `visual_state.json`)
- Right: `HH:MM:SS` + heartbeat dot (green/red)

### Context Panel (new)
Shows live workspace and mission context:
- **WORKSPACE** section: active project (amber), active window
- **MISSION** section: word-wrapped current mission/goal text
- **NEXT** section: next action
- Footer: worker count badge (amber when workers are running)

### Bottom Activity Strip (new)
Replaces the old full-height log panel at the bottom of HOME tab. Shows the last N activity entries in compact single-line format. Inset `_C_BG_INSET` background.

### Graphs Panel (new)
Four graphs stacked vertically (CPU, MEM, NET↓, NET↑) in the right column. Each graph is a self-contained `_draw_graph()` panel with amber fill/line color.

---

## Living State Behaviors

### New `_breath_scale()` method

Controls core node size as a function of state and phase:

| State | Behavior |
|---|---|
| `idle` / `armed` | Slow sinusoidal ±3.2% — gentle breathing at orbital rate |
| `listening` | ±5.5% pulse at 1.5× rate — alert, responsive |
| `speaking` | ±5.5% abs-sin + drive amplification — reacts to audio |
| `processing` | ±1.8% at 3.8× rate — tight micro-oscillation |

### Ambient glow (new)

- **Idle/armed**: radial gradient glow around core, breathing with phase
- **Processing**: static radial glow in steel-blue, scanning ring animation

### Core animation — amber rings

All concentric rings, segmented bands, and arc highlights now render in the current state color (amber for idle, steel for processing, orange for speaking). The orbital scan in processing state remains negative-velocity (counter-rotation).

---

## Other Visual Improvements

### Header bar
- PROMETHEUS wordmark added in amber-dim (full mode only, left of content area)
- Restart button: amber border/glow instead of teal
- Separator line: warm stone `_C_SEP` instead of blue

### Sidebar
- Active tab: amber fill + amber glow border
- Inactive icons: warm ivory faint
- Background: deepest graphite `_C_BG_SIDEBAR`

### Panel style
- Background gradient: graphite `(16,16,20)` → `(11,11,15)`
- Border: warm stone `_C_BORDER`
- Section titles: amber dim with 1.5px letter spacing

### Chat tab
- User messages: amber text
- Prometheus messages: steel text (distinct from user)
- Input border: amber glow on focus

### Activity filter buttons (ACTV tab)
- Active filter: amber glow border + amber text
- Inactive: ivory faint

### Store additions (UI-binding only)
Added three new fields to `Store` (read from existing `visual_state.json` — no backend changes):
- `store.active_project` — shown in status strip and context panel
- `store.active_window` — shown in context panel
- `store.open_windows` — available for future panels

---

## What Was NOT Changed

Per hard constraints:
- Calendar logic, executor, Lumen behavior — unchanged
- Home Assistant logic — unchanged
- Safety/risk/executor behavior — unchanged
- Planner architecture — unchanged
- Vault backend — unchanged
- Logs backend — unchanged
- `format_log_event()` — unchanged
- `Store.refresh()` data reading logic — unchanged (only added 3 new fields)
- `SystemStats` — unchanged
- Compact mode behavior — unchanged (prescribed in CLAUDE.md)
- Close behavior, restart behavior — unchanged
- All 7-tab sidebar navigation — unchanged
- Tab content data (ops cards, agents, diagnostics, system, cost) — same data, re-themed

---

## Test Results

| Suite | Count | Result |
|---|---|---|
| Full test suite | 1023 tests, 1 skipped | All pass |
| Audit | 273/273 | All pass |
| `py_compile` | `jarvis_desktop_hud.py` | OK |

"""
log_viewer.py — Read Prometheus runtime logs from JARVIS_LOGS_DIR.

Logs are JSONL files written by utils.log_event(), named YYYY-MM-DD.jsonl.
All paths are validated against JARVIS_LOGS_DIR before any read is attempted.
No subprocess, no shell execution.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from prometheus.infra.paths import JARVIS_LOGS_DIR


def list_log_files() -> list[dict[str, Any]]:
    """Return metadata for all .jsonl log files in JARVIS_LOGS_DIR, newest first."""
    if not JARVIS_LOGS_DIR.exists():
        return []
    files: list[dict[str, Any]] = []
    for p in sorted(JARVIS_LOGS_DIR.glob("*.jsonl"), reverse=True):
        try:
            stat = p.stat()
            files.append({
                "name": p.name,
                "path": str(p),
                "size_bytes": stat.st_size,
                "modified": _iso(stat.st_mtime),
            })
        except OSError:
            pass
    return files


def read_latest_log_tail(tail_lines: int = 50) -> tuple[str, str]:
    """Read the tail of the most recent log file.

    Returns (filename, formatted_text). formatted_text is empty string
    if no log files exist.
    """
    if not JARVIS_LOGS_DIR.exists():
        return ("", "")
    candidates = sorted(JARVIS_LOGS_DIR.glob("*.jsonl"), reverse=True)
    if not candidates:
        return ("", "")
    latest = candidates[0]
    return (latest.name, read_log_tail(latest.name, tail_lines=tail_lines))


def read_log_tail(filename: str, tail_lines: int = 50) -> str:
    """Read the last `tail_lines` lines of a log file in JARVIS_LOGS_DIR.

    `filename` must be a bare filename (no path separators) that exists
    inside JARVIS_LOGS_DIR. Raises ValueError for path traversal attempts.
    Returns formatted string, empty string if file is empty or unreadable.
    """
    safe_path = _resolve_safe(filename)
    if not safe_path.exists():
        return ""
    try:
        text = safe_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""

    raw_lines = text.splitlines()
    tail = raw_lines[-tail_lines:] if len(raw_lines) > tail_lines else raw_lines
    return _format_jsonl(tail)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _resolve_safe(filename: str) -> Path:
    """Resolve filename to an absolute path and assert it stays inside JARVIS_LOGS_DIR."""
    if "/" in filename or "\\" in filename or ".." in filename:
        raise ValueError(f"log_viewer: unsafe filename: {filename!r}")
    resolved = (JARVIS_LOGS_DIR / filename).resolve()
    if not str(resolved).startswith(str(JARVIS_LOGS_DIR.resolve())):
        raise ValueError(f"log_viewer: path escape attempt: {filename!r}")
    return resolved


def _format_jsonl(lines: list[str]) -> str:
    """Convert raw JSONL lines to human-readable log lines."""
    formatted: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            ts = str(rec.get("ts", ""))
            kind = str(rec.get("kind", ""))
            # Build a concise payload string (exclude ts and kind)
            payload = {k: v for k, v in rec.items() if k not in ("ts", "kind")}
            payload_str = ""
            if payload:
                payload_str = " | " + ", ".join(
                    f"{k}={str(v)[:60]}" for k, v in list(payload.items())[:4]
                )
            formatted.append(f"{ts[11:19] if len(ts) >= 19 else ts}  {kind}{payload_str}")
        except (json.JSONDecodeError, ValueError):
            formatted.append(line[:200])
    return "\n".join(formatted)


def _iso(mtime: float) -> str:
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(mtime))


# ── CLI entry point ───────────────────────────────────────────────────────────

def _main(argv: list[str] | None = None) -> None:
    import sys
    import json as _json
    args = argv if argv is not None else sys.argv[1:]
    cmd = args[0] if args else "--latest"

    if cmd in ("--latest", "latest"):
        fname, text = read_latest_log_tail(tail_lines=50)
        if not fname:
            print(_json.dumps({"ok": True, "files_found": 0, "message": f"No log files in {JARVIS_LOGS_DIR}"}))
        else:
            lines = [ln for ln in text.splitlines() if ln.strip()]
            print(_json.dumps({
                "ok": True,
                "logs_dir": str(JARVIS_LOGS_DIR),
                "latest_file": fname,
                "lines_returned": len(lines),
                "entries": lines,
            }, indent=2))

    elif cmd in ("--list", "list"):
        files = list_log_files()
        print(_json.dumps({"ok": True, "files": files}, indent=2))

    elif cmd in ("--tail", "tail") and len(args) >= 2:
        filename = args[1]
        n = int(args[2]) if len(args) >= 3 else 50
        try:
            text = read_log_tail(filename, tail_lines=n)
            lines = [ln for ln in text.splitlines() if ln.strip()]
            print(_json.dumps({"ok": True, "file": filename, "entries": lines}, indent=2))
        except ValueError as exc:
            print(_json.dumps({"ok": False, "error": str(exc)}))
            sys.exit(1)

    else:
        print(
            "Usage:\n"
            "  python -m prometheus.infra.log_viewer --latest\n"
            "  python -m prometheus.infra.log_viewer --list\n"
            "  python -m prometheus.infra.log_viewer --tail FILENAME [N]"
        )
        sys.exit(1)


if __name__ == "__main__":
    _main()

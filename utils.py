from __future__ import annotations

import json
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

import numpy as np

from config import LOG_DIR


def ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _json_safe(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def log_event(kind: str, payload: dict[str, Any]) -> None:
    rec = {"ts": ts(), "kind": kind, **_json_safe(payload)}
    path = LOG_DIR / f"{time.strftime('%Y-%m-%d')}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def run_cmd(
    cmd: list[str] | str,
    check: bool = False,
    capture: bool = False,
) -> subprocess.CompletedProcess:
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
    )


def notify(message: str) -> None:
    try:
        run_cmd(["notify-send", "Prometheus", message])
    except Exception:
        pass
    print(f"[Prometheus] {message}")


def command_exists(name: str) -> bool:
    return (
        subprocess.run(
            ["bash", "-lc", f"command -v {shlex.quote(name)} >/dev/null 2>&1"]
        ).returncode
        == 0
    )


def ensure_dir(path: str | Path) -> Path:
    p = Path(path).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def kill_existing(process_name: str) -> None:
    subprocess.run(
        ["pkill", "-f", process_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from prometheus.memory.memory_core import MEMORY_DIR, now_iso

EPISODES_PATH = MEMORY_DIR / "episodes.jsonl"


class EpisodicMemory:
    def __init__(self) -> None:
        self.path = EPISODES_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(
        self,
        kind: str,
        summary: str,
        *,
        tags: list[str] | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        rec = {
            "ts": now_iso(),
            "kind": kind,
            "summary": summary,
            "tags": tags or [],
            "data": data or {},
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")

    def tail(self, limit: int = 100) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        out: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out

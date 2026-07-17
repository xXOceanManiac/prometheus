from __future__ import annotations

from typing import Any

from prometheus.memory.memory_core import MEMORY_DIR, norm_text, now_iso, read_json, write_json

PROCEDURAL_PATH = MEMORY_DIR / "procedural_memory.json"


class ProceduralMemory:
    def __init__(self) -> None:
        self.path = PROCEDURAL_PATH

    def read(self) -> dict[str, Any]:
        return read_json(
            self.path,
            {
                "updated_at": None,
                "routines": [],
                "aliases": {},
            },
        )

    def save_routine(
        self,
        name: str,
        *,
        description: str = "",
        triggers: list[str] | None = None,
        steps: list[dict[str, Any]] | None = None,
        tags: list[str] | None = None,
    ) -> None:
        data = self.read()
        nn = norm_text(name)
        existing = None
        for routine in data["routines"]:
            if norm_text(routine.get("name", "")) == nn:
                existing = routine
                break

        payload = {
            "name": name,
            "description": description,
            "triggers": triggers or [],
            "steps": steps or [],
            "tags": tags or [],
            "updated_at": now_iso(),
        }

        if existing is None:
            data["routines"].append(payload)
        else:
            existing.update(payload)

        data["updated_at"] = now_iso()
        write_json(self.path, data)

    def get_routine(self, query: str) -> dict[str, Any] | None:
        data = self.read()
        nq = norm_text(query)

        for routine in data["routines"]:
            if norm_text(routine.get("name", "")) == nq:
                return routine

        for routine in data["routines"]:
            hay = " ".join(
                [routine.get("name", ""), routine.get("description", "")]
                + routine.get("triggers", [])
                + routine.get("tags", [])
            )
            if nq in norm_text(hay):
                return routine

        return None

from __future__ import annotations

from typing import Any

from prometheus.memory.memory_core import MEMORY_DIR, now_iso, read_json, write_json

WORKING_MEMORY_PATH = MEMORY_DIR / "working_memory.json"


class WorkingMemory:
    def __init__(self) -> None:
        self.path = WORKING_MEMORY_PATH

    def _default_payload(self) -> dict[str, Any]:
        return {
            "updated_at": None,
            "active_goal": "",
            "active_workspace": "",
            "active_context_name": "",
            "active_media_flow": "",
            "current_mode": "",
            "pending_confirmation": "",
            "last_user_request": "",
            "last_user_transcript": "",
            "last_response_text": "",
            "last_tool_action": "",
            "last_tool_result": {},
            "last_plan": {},
            "last_preference_edit": {},
            "screen_focus": {},
        }

    def read(self) -> dict[str, Any]:
        return read_json(self.path, self._default_payload())

    def write(self, data: dict[str, Any]) -> None:
        payload = self.read()
        payload.update(data)
        payload["updated_at"] = now_iso()
        write_json(self.path, payload)

    def set_user_request(self, text: str) -> None:
        clean = str(text).strip()
        self.write({"last_user_request": clean, "last_user_transcript": clean})

    def set_response_text(self, text: str) -> None:
        self.write({"last_response_text": str(text).strip()})

    def set_plan(self, plan: dict[str, Any]) -> None:
        self.write({"last_plan": plan or {}})

    def set_preference_edit(self, edit: dict[str, Any]) -> None:
        self.write({"last_preference_edit": edit or {}})

    def set_tool_result(self, *, action: str, ok: bool, message: str, data: dict[str, Any] | None = None) -> None:
        self.write(
            {
                "last_tool_action": str(action).strip(),
                "last_tool_result": {
                    "ok": bool(ok),
                    "message": str(message).strip(),
                    "data": data or {},
                    "updated_at": now_iso(),
                },
            }
        )

    def set_screen_focus(self, focus: dict[str, Any]) -> None:
        self.write({"screen_focus": focus or {}})

    def clear(self) -> None:
        payload = self._default_payload()
        payload["updated_at"] = now_iso()
        write_json(self.path, payload)

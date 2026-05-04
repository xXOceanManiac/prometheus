from __future__ import annotations

import os
from typing import Any

from config import CONFIG
from utils import log_event


class _OllamaClient:
    def __init__(self, model: str) -> None:
        self.model = model

    def complete(self, prompt: str, system: str = "") -> str:
        import ollama

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        resp = ollama.chat(model=self.model, messages=messages)
        return str(resp["message"]["content"])


class _OpenAIFallbackClient:
    def __init__(self, api_key: str, model: str = "gpt-4o-mini") -> None:
        self.api_key = api_key
        self.model = model

    def complete(self, prompt: str, system: str = "") -> str:
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key)
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        resp = client.chat.completions.create(
            model=self.model, messages=messages, timeout=30
        )
        return resp.choices[0].message.content or ""


def get_llm(task_type: str = "background") -> Any | None:
    """
    Return an LLM client for background/planning/summarize tasks.
    Tries Ollama first; falls back to OpenAI chat API.
    Returns None if neither is available — callers must handle this gracefully.
    Never raises.
    """
    if task_type == "realtime":
        raise ValueError(
            "Realtime tasks use RealtimeJarvisClient, not get_llm()"
        )

    ollama_url = str(CONFIG.get("ollama_url", "http://localhost:11434")).strip()
    ollama_model = str(CONFIG.get("ollama_model", "mistral")).strip()

    try:
        import requests as _req

        resp = _req.get(f"{ollama_url}/api/tags", timeout=2.0)
        if resp.status_code == 200:
            import ollama  # noqa: F401 — verify importable

            return _OllamaClient(model=ollama_model)
    except Exception as exc:
        log_event(
            "ollama_unavailable",
            {"error": str(exc)[:120], "fallback": "openai"},
        )

    api_key = (
        str(CONFIG.get("openai_api_key", "")).strip()
        or os.getenv("OPENAI_API_KEY", "").strip()
    )
    if api_key:
        try:
            import openai  # noqa: F401 — verify importable

            return _OpenAIFallbackClient(api_key=api_key)
        except Exception as exc:
            log_event("openai_fallback_error", {"error": str(exc)[:120]})

    log_event("llm_router_no_client", {"task_type": task_type})
    return None

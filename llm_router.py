from __future__ import annotations

import os
from typing import Any

from config import CONFIG
from utils import log_event

_CHAT_SYSTEM_BASE = (
    "You are Prometheus — a composed, intelligent local desktop assistant. "
    "You are answering through a text chat interface. "
    "Be concise and direct. No preamble. No apologies. "
    "You have access to live workspace context provided in this prompt."
)


def chat_completion(
    user_message: str,
    context: dict | None = None,
    history: list[dict] | None = None,
) -> str:
    """
    Generate a text response for the HUD chat tab.

    Priority: Anthropic claude-opus-4-5 → Ollama mistral → static fallback.
    Logs chat_completion_start and chat_completion_done.
    Never raises.
    """
    context = context or {}
    history = history or []

    # Build system prompt with live context
    system_parts = [_CHAT_SYSTEM_BASE]
    if context.get("active_project"):
        system_parts.append(f"Active project: {context['active_project']}")
    if context.get("last_tool_result"):
        import json as _json
        system_parts.append(f"Last tool result: {_json.dumps(context['last_tool_result'])[:200]}")
    system_prompt = "\n".join(system_parts)

    # Build conversation messages (last 20 entries = 10 exchanges)
    conv_messages: list[dict[str, str]] = []
    for entry in (history or [])[-20:]:
        role = "user" if entry.get("role") == "user" else "assistant"
        content = str(entry.get("content", "")).strip()
        if content:
            conv_messages.append({"role": role, "content": content})
    conv_messages.append({"role": "user", "content": user_message})

    log_event("chat_completion_start", {
        "model": "claude-opus-4-5",
        "turns": len(conv_messages),
        "user_msg": user_message[:80],
    })

    # ── Primary: Anthropic SDK ─────────────────────────────────────────────
    api_key = (
        os.getenv("ANTHROPIC_API_KEY", "").strip()
        or str(CONFIG.get("anthropic_api_key", "")).strip()
    )
    if api_key:
        try:
            import anthropic as _anthropic
            client = _anthropic.Anthropic(api_key=api_key)
            resp = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=1024,
                system=system_prompt,
                messages=conv_messages,
            )
            text = resp.content[0].text.strip() if resp.content else ""
            log_event("chat_completion_done", {
                "model": "claude-opus-4-5",
                "input_tokens": resp.usage.input_tokens,
                "output_tokens": resp.usage.output_tokens,
            })
            return text
        except Exception as exc:
            log_event("chat_completion_anthropic_failed", {"error": str(exc)[:200]})

    # ── Fallback: Ollama ───────────────────────────────────────────────────
    ollama_url = str(CONFIG.get("ollama_url", "http://localhost:11434")).strip()
    ollama_model = str(CONFIG.get("ollama_model", "mistral")).strip()
    try:
        import requests as _req
        r = _req.get(f"{ollama_url}/api/tags", timeout=2)
        if r.status_code == 200:
            import ollama as _ollama
            messages_with_sys = [{"role": "system", "content": system_prompt}] + conv_messages
            resp2 = _ollama.chat(model=ollama_model, messages=messages_with_sys)
            text = str(resp2["message"]["content"]).strip()
            log_event("chat_completion_done", {
                "model": ollama_model,
                "fallback": "ollama",
            })
            return text
    except Exception as exc:
        log_event("chat_completion_ollama_failed", {"error": str(exc)[:200]})

    log_event("chat_completion_failed", {"reason": "all providers failed"})
    return "I'm having trouble connecting right now."


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
            "Realtime tasks use RealtimePrometheusClient, not get_llm()"
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

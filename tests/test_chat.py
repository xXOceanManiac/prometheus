"""
tests/test_chat.py — Session 5 chat tab tests.
All 5 must pass.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure project root is on path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ── Helpers ────────────────────────────────────────────────────────────────────

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ── Test 1: chat_completion returns text from Claude ──────────────────────────

class Test1ChatCompletionClaude(unittest.TestCase):
    def test_returns_text_from_claude(self):
        mock_content = MagicMock()
        mock_content.text = "Prometheus is a local-first autonomous desktop assistant."

        mock_usage = MagicMock()
        mock_usage.input_tokens = 50
        mock_usage.output_tokens = 20

        mock_response = MagicMock()
        mock_response.content = [mock_content]
        mock_response.usage = mock_usage

        logged_events = []

        def fake_log(kind, payload=None):
            logged_events.append({"kind": kind, **(payload or {})})

        with patch("prometheus.infra.llm_router.os.getenv", return_value="sk-test-key"), \
             patch("prometheus.infra.llm_router.log_event", side_effect=fake_log), \
             patch("prometheus.infra.llm_router.CONFIG", {"ollama_model": "mistral", "ollama_url": "http://localhost:11434"}):
            import importlib
            import prometheus.infra.llm_router as llm_router
            importlib.reload(llm_router)

            mock_client = MagicMock()
            mock_client.messages.create.return_value = mock_response

            with patch("anthropic.Anthropic", return_value=mock_client):
                with patch("prometheus.infra.llm_router.os.getenv", return_value="sk-test-key"), \
                     patch("prometheus.infra.llm_router.log_event", side_effect=fake_log):
                    result = llm_router.chat_completion(
                        "what is the Prometheus project?", {}
                    )

        self.assertEqual(result, "Prometheus is a local-first autonomous desktop assistant.")
        done_events = [e for e in logged_events if e.get("kind") == "chat_completion_done"]
        self.assertTrue(any(e.get("model") == "claude-opus-4-5" for e in done_events))
        print("Test 1 ✅ — chat_completion returned text from Claude mock")


# ── Test 2: chat_completion falls back to Ollama ──────────────────────────────

class Test2ChatCompletionOllamaFallback(unittest.TestCase):
    def test_falls_back_to_ollama(self):
        logged_events = []

        def fake_log(kind, payload=None):
            logged_events.append({"kind": kind, **(payload or {})})

        mock_ollama_resp = {"message": {"content": "Hello from Ollama."}}

        import importlib
        import prometheus.infra.llm_router as llm_router
        importlib.reload(llm_router)

        with patch("prometheus.infra.llm_router.os.getenv", return_value=""):
            with patch("prometheus.infra.llm_router.log_event", side_effect=fake_log):
                with patch("prometheus.infra.llm_router.CONFIG", {
                    "ollama_url": "http://localhost:11434",
                    "ollama_model": "mistral",
                }):
                    mock_req = MagicMock()
                    mock_req.status_code = 200
                    mock_requests = MagicMock()
                    mock_requests.get.return_value = mock_req

                    mock_ollama_mod = MagicMock()
                    mock_ollama_mod.chat.return_value = mock_ollama_resp

                    with patch.dict("sys.modules", {"requests": mock_requests, "ollama": mock_ollama_mod}):
                        result = llm_router.chat_completion("hello", {})

        self.assertEqual(result, "Hello from Ollama.")
        done_events = [e for e in logged_events if e.get("kind") == "chat_completion_done"]
        self.assertTrue(any(e.get("fallback") == "ollama" for e in done_events))
        print("Test 2 ✅ — chat_completion fell back to Ollama correctly")


# ── Test 3: Tool action in chat returns formatted text ─────────────────────────

class Test3ToolActionFormattedText(unittest.TestCase):
    def test_git_status_chat_format(self):
        from prometheus.execution.tools import ToolRegistry, ToolResult

        registry = ToolRegistry()

        mock_result = ToolResult(
            ok=True,
            message="Git status",
            data={"status": " M tools.py\n M realtime_client.py\n?? new_file.py", "clean": False},
        )

        with patch.object(registry, "_execute_one", return_value=mock_result):
            result = registry.execute({"action": "git_status"}, chat_format=True)

        self.assertIn("changed file", result.message)
        self.assertIn("tools.py", result.message)
        self.assertNotIn("{", result.message[:50])  # not a raw dict dump
        print("Test 3 ✅ — Tool action formatted as readable text for chat")


# ── Helpers for driving the real chat polling loop ────────────────────────────

def _make_client():
    from prometheus.core.realtime_client import RealtimePrometheusClient
    mock_speaker = MagicMock()
    mock_tools = MagicMock()
    mock_tools.schemas.return_value = []
    with patch("prometheus.core.realtime_client.CONFIG", {
        "openai_api_key": "sk-test",
        "realtime_model": "gpt-realtime",
        "voice": "alloy",
    }):
        return RealtimePrometheusClient(speaker=mock_speaker, tools=mock_tools)


async def _run_chat_cycle(client, wm, text: str, ts: str, timeout: float = 5.0) -> dict:
    """Write chat_input, run the REAL _chat_polling_loop until it responds.

    Completion is detected via chat_history: the loop appends the user message
    and the assistant response as its final step.
    """
    wm.write({"chat_input": {"text": text, "ts": ts}})
    client.connected = True
    task = asyncio.ensure_future(client._chat_polling_loop())
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            data = wm.read()
            history = data.get("chat_history") or []
            if any(h.get("role") == "user" and h.get("content") == text for h in history):
                return data.get("chat_response") or {}
        raise AssertionError("chat polling loop produced no response in time")
    finally:
        client.connected = False
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


# ── Test 4: Conversational message routes through the real polling loop ───────

class Test4ConversationalRoutesToLLM(unittest.TestCase):
    def test_conversational_uses_text_model(self):
        from prometheus.memory.working_memory import WorkingMemory
        wm = WorkingMemory()
        wm.write({"chat_history": [], "chat_input": None, "chat_response": None})

        client = _make_client()

        # Conversational phrase must not match a deterministic tool override
        self.assertIsNone(client._direct_intent_override("what are you?"))

        chat_completion_calls = []

        def fake_completion(msg, context=None, history=None):
            chat_completion_calls.append(msg)
            return "I am Prometheus, your desktop assistant."

        with patch("prometheus.infra.llm_router.chat_completion", side_effect=fake_completion):
            resp = asyncio.run(_run_chat_cycle(client, wm, "what are you?", _now()))

        self.assertEqual(chat_completion_calls, ["what are you?"])
        self.assertEqual(resp.get("text"), "I am Prometheus, your desktop assistant.")
        print("Test 4 ✅ — real polling loop routed conversational chat to text model")


# ── Test 5: Chat history accumulates through the real polling loop ────────────

class Test5ChatHistoryAccumulates(unittest.TestCase):
    def test_history_grows_correctly(self):
        from prometheus.memory.working_memory import WorkingMemory
        wm = WorkingMemory()
        wm.write({"chat_history": [], "chat_input": None, "chat_response": None})

        client = _make_client()
        # "what project am I on?" matches a direct tool override, so the real
        # loop routes it to the tool registry — give that boundary a real result.
        from prometheus.execution.tools import ToolResult
        client.tools.execute.return_value = ToolResult(
            ok=True, message="Active project: PROMETHEUS", data={}
        )

        def fake_completion(msg, context=None, history=None):
            return f"Response to: {msg}"

        messages = ["hello", "how are you?", "what project am I on?"]

        async def _drive():
            for i, msg in enumerate(messages):
                # Distinct ascending ts per message so the loop treats each as new
                ts = _now() + f".{i}"
                with patch("prometheus.infra.llm_router.chat_completion", side_effect=fake_completion):
                    await _run_chat_cycle(client, wm, msg, ts)

        asyncio.run(_drive())

        final_history = wm.read().get("chat_history", [])
        self.assertEqual(len(final_history), 6)
        self.assertEqual(final_history[0]["role"], "user")
        self.assertEqual(final_history[0]["content"], "hello")
        self.assertEqual(final_history[1]["role"], "assistant")
        self.assertEqual(final_history[1]["content"], "Response to: hello")
        self.assertEqual(final_history[2]["content"], "how are you?")
        self.assertEqual(final_history[4]["content"], "what project am I on?")
        # The override phrase was answered by the tool path, not the LLM
        self.assertEqual(final_history[5]["content"], "Active project: PROMETHEUS")
        print("Test 5 ✅ — real polling loop accumulated 6 history entries in order")


if __name__ == "__main__":
    unittest.main(verbosity=2)

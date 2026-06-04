"""
tools/test_morning_speaker.py — Smoke test for morning routine speaker path.

Connects directly to the OpenAI Realtime API and sends the morning routine
speak pattern: conversation.item.create + response.create. Waits for
response.done to confirm the API accepted and processed the request.

Usage:
    cd /home/tatel/Desktop/PROMETHEUS/Prometheus_Main
    source .venv/bin/activate
    python3 tools/test_morning_speaker.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


_TEST_TEXT = "Good morning Tate. This is a morning routine speaker test."
_MODEL = "gpt-4o-realtime-preview"
_TIMEOUT = 30.0


async def run() -> int:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("[SPEAKER TEST] FAIL — OPENAI_API_KEY not set")
        return 1

    try:
        import websockets
    except ImportError:
        print("[SPEAKER TEST] FAIL — websockets package not installed")
        return 1

    url = f"wss://api.openai.com/v1/realtime?model={_MODEL}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "OpenAI-Beta": "realtime=v1",
    }

    print(f"[SPEAKER TEST] Connecting to {url}")
    try:
        ws = await asyncio.wait_for(
            websockets.connect(url, additional_headers=headers),
            timeout=10.0,
        )
    except Exception as exc:
        print(f"[SPEAKER TEST] FAIL — connect error: {exc}")
        return 1

    print("[SPEAKER TEST] Connected")
    errors: list[str] = []

    async with ws:
        # Configure session
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": "You are a test assistant. Speak the text you are given exactly.",
            },
        }))
        print("[SPEAKER TEST] session.update sent")

        # Send conversation item
        await ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "system",
                "content": [{"type": "input_text", "text": f"[MORNING_ROUTINE] {_TEST_TEXT}"}],
            },
        }))
        print("[SPEAKER TEST] conversation.item.create sent")

        # Request response
        await ws.send(json.dumps({
            "type": "response.create",
            "response": {
                "instructions": f"Say exactly: {_TEST_TEXT}",
            },
        }))
        print("[SPEAKER TEST] response.create sent — waiting for response.done...")

        deadline = asyncio.get_event_loop().time() + _TIMEOUT
        response_done = False
        audio_chunks = 0

        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            except asyncio.TimeoutError:
                print("[SPEAKER TEST] waiting...")
                continue
            except Exception as exc:
                print(f"[SPEAKER TEST] recv error: {exc}")
                break

            event = json.loads(raw)
            etype = event.get("type", "")
            print(f"[SPEAKER TEST] recv: {etype}")

            if etype == "error":
                err = event.get("error", {})
                msg = f"code={err.get('code')} message={err.get('message')}"
                print(f"[SPEAKER TEST] API ERROR: {msg}")
                errors.append(msg)

            elif etype in ("response.output_audio.delta", "response.audio.delta"):
                audio_chunks += 1

            elif etype == "response.done":
                response_done = True
                print(f"[SPEAKER TEST] response.done received — audio_chunks={audio_chunks}")
                break

    if response_done and not errors:
        print(f"[SPEAKER TEST] PASS — response completed, {audio_chunks} audio delta(s) received")
        return 0
    elif errors:
        print(f"[SPEAKER TEST] FAIL — API errors: {errors}")
        return 1
    else:
        print("[SPEAKER TEST] FAIL — response.done not received within timeout")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))

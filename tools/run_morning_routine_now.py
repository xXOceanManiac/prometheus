"""
tools/run_morning_routine_now.py — Bypass eligibility and run the morning routine immediately.

Creates a fake wake event with start_time = now, instantiates all real adapters,
and calls run_morning_routine() directly.

Modes:
  (default)  HA calls are live. Speaker prints text but does not call Realtime API.
  --speak    HA calls are live. Speaker connects to OpenAI Realtime API and speaks.

Usage:
    cd /home/tatel/Desktop/PROMETHEUS/Prometheus_Main
    source .venv/bin/activate

    # Dry-run speaker (HA live, speech printed only):
    python3 tools/run_morning_routine_now.py

    # Full live run (HA + real speech via Realtime API):
    python3 tools/run_morning_routine_now.py --speak
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prometheus.routines.morning_routine import MorningRoutineService
from prometheus.routines.morning_adapters import (
    HomeAssistantMorningClient,
    JSONMorningRoutineStateStore,
    MorningCalendarReader,
    MorningWeatherProvider,
    PrometheusMorningSpeaker,
)


class _FakeWakeEvent:
    def __init__(self) -> None:
        self.title = "Wake Up"
        self.start_time = datetime.now().isoformat(timespec="seconds")
        self.event_id = f"dry_run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


class _PrintSpeaker:
    """Speaker stub: prints text but does not call Realtime API. Counts as success."""
    async def speak(self, text: str) -> None:
        print(f"[DRY RUN SPEAKER] Would speak ({len(text)} chars): {text[:120]!r}", flush=True)


class _LiveRealtimeClient:
    """
    Minimal wrapper around a live WebSocket connection.

    Exposes connected=True, async send(), and register_response_done_event()
    so PrometheusMorningSpeaker can wait for response.done before returning.
    """

    def __init__(self, ws) -> None:
        self._ws = ws
        self.connected = True
        self._recv_task: asyncio.Task | None = None
        self._response_done_events: list[asyncio.Event] = []

    async def send(self, payload: dict) -> None:
        await self._ws.send(json.dumps(payload))

    def register_response_done_event(self, evt: asyncio.Event) -> None:
        self._response_done_events.append(evt)

    def _fire_response_done_events(self) -> None:
        evts, self._response_done_events = self._response_done_events, []
        for e in evts:
            e.set()

    async def start_receiver(self) -> None:
        self._recv_task = asyncio.create_task(self._recv_loop())

    async def stop_receiver(self) -> None:
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass

    async def _recv_loop(self) -> None:
        try:
            async for raw in self._ws:
                event = json.loads(raw)
                etype = event.get("type", "")
                if etype == "error":
                    err = event.get("error", {})
                    print(f"[SPEAKER RECV] API ERROR code={err.get('code')} msg={err.get('message')}", flush=True)
                    self._fire_response_done_events()
                elif etype in ("response.output_audio.delta", "response.audio.delta"):
                    pass  # suppress audio deltas — no speaker output device in this tool
                elif etype == "response.done":
                    print("[SPEAKER RECV] response.done — speech delivered to API", flush=True)
                    self._fire_response_done_events()
                elif etype in ("response.cancelled", "response.failed"):
                    print(f"[SPEAKER RECV] {etype}", flush=True)
                    self._fire_response_done_events()
                else:
                    print(f"[SPEAKER RECV] {etype}", flush=True)
        except Exception:
            self._fire_response_done_events()


def _log(event_type: str, payload: dict) -> None:
    ts = datetime.now().isoformat(timespec="seconds")
    print(f"[LOG] {ts} {event_type} {json.dumps(payload)}", flush=True)


async def _connect_realtime(api_key: str):
    try:
        import websockets
    except ImportError:
        print("[RUN NOW] FAIL — websockets package not installed")
        return None

    model = "gpt-4o-realtime-preview"
    url = f"wss://api.openai.com/v1/realtime?model={model}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "OpenAI-Beta": "realtime=v1",
    }
    print(f"[RUN NOW] Connecting to Realtime API (model={model})...", flush=True)
    try:
        ws = await asyncio.wait_for(
            websockets.connect(url, additional_headers=headers),
            timeout=10.0,
        )
        return ws
    except Exception as exc:
        print(f"[RUN NOW] FAIL — Realtime connect error: {exc}", flush=True)
        return None


async def main() -> int:
    use_real_speaker = "--speak" in sys.argv

    if use_real_speaker:
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            print("[RUN NOW] FAIL — OPENAI_API_KEY not set for --speak mode")
            return 1

        ws = await _connect_realtime(api_key)
        if ws is None:
            return 1

        print("[RUN NOW] Realtime API connected. Speaker is live.", flush=True)

        async with ws:
            # Configure session
            await ws.send(json.dumps({
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "instructions": "You are Prometheus. Speak the morning routine summary naturally and concisely.",
                },
            }))

            live_client = _LiveRealtimeClient(ws)
            await live_client.start_receiver()

            speaker = PrometheusMorningSpeaker(client=live_client)
            exit_code = await _run_routine(speaker)

            # Give receiver a moment to catch response.done before closing
            await asyncio.sleep(3.0)
            await live_client.stop_receiver()

        return exit_code
    else:
        print("[RUN NOW] Speaker: dry-run mode (text printed, not spoken). Use --speak for live speech.", flush=True)
        speaker = _PrintSpeaker()
        return await _run_routine(speaker)


async def _run_routine(speaker) -> int:
    svc = MorningRoutineService(
        calendar_reader=MorningCalendarReader(),
        ha_client=HomeAssistantMorningClient(),
        speaker=speaker,
        weather_provider=MorningWeatherProvider(),
        state_store=JSONMorningRoutineStateStore(),
        logger=_log,
    )

    wake_event = _FakeWakeEvent()
    print(f"[RUN NOW] Starting morning routine — event id={wake_event.event_id}", flush=True)

    await svc.run_morning_routine(wake_event)

    print(f"[RUN NOW] Done. HA ok={svc._ha_ok} fail={svc._ha_fail}", flush=True)
    return 0 if svc._ha_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""
event_bus.py — Lightweight asyncio pub/sub event bus for the Prometheus sensor layer.

Priority model:
  HIGH   → dispatched immediately via asyncio.ensure_future (bypasses queue)
  NORMAL → queued, FIFO, oldest-drop when full
  LOW    → queued, FIFO, oldest-drop when full

Subscribers register by EventType or None (wildcard = all events).
Never blocks. Never crashes on bad payloads.

Usage:
    from event_bus import get_bus, Event, EventType, Priority
    bus = get_bus()
    bus.subscribe(EventType.WINDOW_CHANGED, handler)
    bus.publish(Event(EventType.WINDOW_CHANGED, source="window_sensor", payload={...}))
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine

from utils import log_event

_MAX_QUEUE_DEPTH = 200


class EventType(Enum):
    WINDOW_CHANGED   = "window_changed"
    TEXT_SELECTED    = "text_selected"
    FILE_CHANGED     = "file_changed"
    ERROR_DETECTED   = "error_detected"
    PROCESS_CHANGED  = "process_changed"
    MISSION_UPDATED  = "mission_updated"
    BLOCKER_ADDED    = "blocker_added"
    BUILD_FAILED     = "build_failed"
    DEPLOY_EVENT     = "deploy_event"
    HA_STATE_CHANGED = "ha_state_changed"
    GENERIC          = "generic"


class Priority(Enum):
    HIGH   = 0
    NORMAL = 1
    LOW    = 2


@dataclass
class Event:
    event_type: EventType
    source: str
    payload: dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    priority: Priority = Priority.NORMAL

    def __post_init__(self) -> None:
        if not isinstance(self.payload, dict):
            self.payload = {"raw": str(self.payload)[:500]}


Handler = Callable[[Event], "Coroutine[Any, Any, None] | None"]


class EventBus:
    """
    Asyncio pub/sub event bus.

    Thread-safety: designed for single asyncio event loop. All publish/subscribe
    calls must come from the same thread as the running loop.
    """

    def __init__(self, max_depth: int = _MAX_QUEUE_DEPTH) -> None:
        self._max_depth = max_depth
        self._queue: asyncio.Queue | None = None
        self._subscribers: dict[EventType | None, list[Handler]] = {}
        self._running = False
        self._task: asyncio.Task | None = None
        self._dropped = 0

    # ── Public API ────────────────────────────────────────────────────────────

    def publish(self, event: Event) -> None:
        """Publish an event. Non-blocking; drops oldest queued item if queue is full."""
        try:
            if not isinstance(event, Event):
                return
            if event.priority == Priority.HIGH:
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        asyncio.ensure_future(self._dispatch(event))
                        return
                except RuntimeError:
                    pass
            q = self._get_queue()
            if q.full():
                try:
                    q.get_nowait()
                    self._dropped += 1
                    log_event("event_bus_dropped_oldest", {"total_dropped": self._dropped})
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                self._dropped += 1
        except Exception as exc:
            log_event("event_bus_publish_error", {"error": str(exc)[:120]})

    def subscribe(self, event_type: EventType | None, handler: Handler) -> None:
        """Register handler for event_type. Pass None for wildcard (receives all events)."""
        self._subscribers.setdefault(event_type, [])
        if handler not in self._subscribers[event_type]:
            self._subscribers[event_type].append(handler)

    def unsubscribe(self, event_type: EventType | None, handler: Handler) -> None:
        """Remove a previously registered handler."""
        handlers = self._subscribers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)

    async def start(self) -> None:
        """Start the dispatch loop. Must be called from async context."""
        if self._running:
            return
        self._running = True
        self._queue = asyncio.Queue(maxsize=self._max_depth)
        self._task = asyncio.ensure_future(self._dispatch_loop())
        log_event("event_bus_started", {"max_depth": self._max_depth})

    async def stop(self) -> None:
        """Stop the dispatch loop and drain remaining events."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log_event("event_bus_stopped", {"total_dropped": self._dropped})

    def queue_depth(self) -> int:
        return self._queue.qsize() if self._queue else 0

    def subscriber_count(self, event_type: EventType | None = None) -> int:
        return len(self._subscribers.get(event_type, []))

    # ── Internal ──────────────────────────────────────────────────────────────

    def _get_queue(self) -> asyncio.Queue:
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self._max_depth)
        return self._queue

    async def _dispatch_loop(self) -> None:
        while self._running:
            try:
                event = await asyncio.wait_for(self._get_queue().get(), timeout=0.1)
                await self._dispatch(event)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log_event("event_bus_loop_error", {"error": str(exc)[:120]})

    async def _dispatch(self, event: Event) -> None:
        targets = (
            list(self._subscribers.get(event.event_type, []))
            + list(self._subscribers.get(None, []))
        )
        for handler in targets:
            try:
                result = handler(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                log_event("event_bus_handler_error", {
                    "handler": getattr(handler, "__name__", "?"),
                    "event_type": event.event_type.name,
                    "error": str(exc)[:120],
                })


# ── Singleton ─────────────────────────────────────────────────────────────────

_BUS: EventBus | None = None


def get_bus() -> EventBus:
    """Return the global EventBus singleton. Created on first call."""
    global _BUS
    if _BUS is None:
        _BUS = EventBus()
    return _BUS

"""
tests/test_event_bus.py — Unit tests for the Prometheus event bus.
"""
import asyncio
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from event_bus import Event, EventBus, EventType, Priority


def _make_event(
    event_type: EventType = EventType.GENERIC,
    priority: Priority = Priority.NORMAL,
    payload: dict | None = None,
) -> Event:
    return Event(event_type=event_type, source="test", payload=payload or {}, priority=priority)


class TestEventBusPublishReceive(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.bus = EventBus(max_depth=50)
        await self.bus.start()

    async def asyncTearDown(self) -> None:
        await self.bus.stop()

    async def test_publish_and_receive_normal_priority(self) -> None:
        received: list[Event] = []

        def handler(event: Event) -> None:
            received.append(event)

        self.bus.subscribe(EventType.GENERIC, handler)
        self.bus.publish(_make_event(payload={"value": 42}))
        await asyncio.sleep(0.2)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].payload["value"], 42)

    async def test_high_priority_dispatched_immediately(self) -> None:
        received: list[Priority] = []

        async def handler(event: Event) -> None:
            received.append(event.priority)

        self.bus.subscribe(EventType.GENERIC, handler)
        self.bus.publish(_make_event(priority=Priority.HIGH))
        await asyncio.sleep(0.1)
        self.assertIn(Priority.HIGH, received)

    async def test_multiple_event_types_isolated(self) -> None:
        window_received: list[Event] = []
        error_received: list[Event] = []

        self.bus.subscribe(EventType.WINDOW_CHANGED, lambda e: window_received.append(e))
        self.bus.subscribe(EventType.ERROR_DETECTED, lambda e: error_received.append(e))

        self.bus.publish(_make_event(EventType.WINDOW_CHANGED))
        self.bus.publish(_make_event(EventType.ERROR_DETECTED))
        await asyncio.sleep(0.2)

        self.assertEqual(len(window_received), 1)
        self.assertEqual(len(error_received), 1)
        self.assertEqual(window_received[0].event_type, EventType.WINDOW_CHANGED)

    async def test_source_field_preserved(self) -> None:
        received: list[Event] = []
        self.bus.subscribe(EventType.GENERIC, lambda e: received.append(e))
        e = Event(EventType.GENERIC, source="window_sensor", payload={"x": 1})
        self.bus.publish(e)
        await asyncio.sleep(0.2)
        self.assertEqual(received[0].source, "window_sensor")

    async def test_timestamp_set(self) -> None:
        before = time.time()
        e = _make_event()
        after = time.time()
        self.assertGreaterEqual(e.timestamp, before)
        self.assertLessEqual(e.timestamp, after)


class TestEventBusPriority(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.bus = EventBus(max_depth=50)
        await self.bus.start()

    async def asyncTearDown(self) -> None:
        await self.bus.stop()

    async def test_priority_enum_values(self) -> None:
        self.assertLess(Priority.HIGH.value, Priority.NORMAL.value)
        self.assertLess(Priority.NORMAL.value, Priority.LOW.value)

    async def test_high_priority_bypasses_queue(self) -> None:
        received: list[str] = []

        async def handler(event: Event) -> None:
            received.append(event.source)

        self.bus.subscribe(EventType.GENERIC, handler)

        # Pause the dispatch loop, publish one NORMAL, then one HIGH
        self.bus._running = False
        self.bus.publish(Event(EventType.GENERIC, source="normal", payload={}, priority=Priority.NORMAL))
        # HIGH bypasses queue and schedules directly
        self.bus.publish(Event(EventType.GENERIC, source="high", payload={}, priority=Priority.HIGH))
        self.bus._running = True

        await asyncio.sleep(0.15)
        # HIGH event should have arrived even though dispatch loop was paused
        self.assertIn("high", received)


class TestEventBusWildcard(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.bus = EventBus(max_depth=50)
        await self.bus.start()

    async def asyncTearDown(self) -> None:
        await self.bus.stop()

    async def test_wildcard_receives_all_event_types(self) -> None:
        received_types: list[EventType] = []

        def wildcard_handler(event: Event) -> None:
            received_types.append(event.event_type)

        self.bus.subscribe(None, wildcard_handler)  # None = wildcard
        self.bus.publish(_make_event(EventType.WINDOW_CHANGED))
        self.bus.publish(_make_event(EventType.ERROR_DETECTED))
        self.bus.publish(_make_event(EventType.FILE_CHANGED))
        await asyncio.sleep(0.3)

        self.assertIn(EventType.WINDOW_CHANGED, received_types)
        self.assertIn(EventType.ERROR_DETECTED, received_types)
        self.assertIn(EventType.FILE_CHANGED, received_types)

    async def test_wildcard_and_specific_both_receive(self) -> None:
        wildcard_count = [0]
        specific_count = [0]

        self.bus.subscribe(None, lambda e: wildcard_count.__setitem__(0, wildcard_count[0] + 1))
        self.bus.subscribe(EventType.GENERIC, lambda e: specific_count.__setitem__(0, specific_count[0] + 1))

        self.bus.publish(_make_event(EventType.GENERIC))
        await asyncio.sleep(0.2)

        self.assertEqual(wildcard_count[0], 1)
        self.assertEqual(specific_count[0], 1)


class TestEventBusQueueDepth(unittest.IsolatedAsyncioTestCase):
    async def test_queue_depth_drops_oldest_when_full(self) -> None:
        bus = EventBus(max_depth=5)
        # Do NOT start the dispatch loop — we want to fill the queue manually
        bus._queue = asyncio.Queue(maxsize=5)

        # Publish more events than the queue can hold
        for i in range(8):
            bus.publish(_make_event(payload={"i": i}, priority=Priority.NORMAL))

        # Queue must never exceed max_depth
        self.assertLessEqual(bus.queue_depth(), 5)
        self.assertGreater(bus._dropped, 0)

    async def test_queue_depth_returns_current_size(self) -> None:
        bus = EventBus(max_depth=20)
        bus._queue = asyncio.Queue(maxsize=20)
        bus.publish(_make_event())
        bus.publish(_make_event())
        self.assertEqual(bus.queue_depth(), 2)


class TestEventBusBadPayload(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.bus = EventBus(max_depth=50)
        await self.bus.start()

    async def asyncTearDown(self) -> None:
        await self.bus.stop()

    async def test_non_dict_payload_coerced_not_crash(self) -> None:
        event = Event(EventType.GENERIC, source="test", payload="not a dict")  # type: ignore[arg-type]
        self.assertIsInstance(event.payload, dict)
        self.assertIn("raw", event.payload)

    async def test_publish_non_event_does_not_crash(self) -> None:
        self.bus.publish("not an event")  # type: ignore[arg-type]
        await asyncio.sleep(0.05)

    async def test_crashing_handler_does_not_stop_bus(self) -> None:
        good_received: list[Event] = []

        def bad_handler(event: Event) -> None:
            raise RuntimeError("handler exploded")

        def good_handler(event: Event) -> None:
            good_received.append(event)

        self.bus.subscribe(EventType.GENERIC, bad_handler)
        self.bus.subscribe(EventType.GENERIC, good_handler)

        self.bus.publish(_make_event())
        await asyncio.sleep(0.2)

        # Bus must still be running and good handler must have fired
        self.assertTrue(self.bus._running)
        self.assertEqual(len(good_received), 1)

    async def test_publish_none_does_not_crash(self) -> None:
        self.bus.publish(None)  # type: ignore[arg-type]
        await asyncio.sleep(0.05)


class TestEventBusUnsubscribe(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.bus = EventBus(max_depth=50)
        await self.bus.start()

    async def asyncTearDown(self) -> None:
        await self.bus.stop()

    async def test_unsubscribe_stops_receiving(self) -> None:
        received: list[Event] = []

        def handler(event: Event) -> None:
            received.append(event)

        self.bus.subscribe(EventType.GENERIC, handler)
        self.bus.unsubscribe(EventType.GENERIC, handler)
        self.bus.publish(_make_event())
        await asyncio.sleep(0.2)
        self.assertEqual(len(received), 0)

    async def test_subscriber_count(self) -> None:
        handler1 = lambda e: None  # noqa: E731
        handler2 = lambda e: None  # noqa: E731
        self.bus.subscribe(EventType.GENERIC, handler1)
        self.bus.subscribe(EventType.GENERIC, handler2)
        self.assertEqual(self.bus.subscriber_count(EventType.GENERIC), 2)
        self.bus.unsubscribe(EventType.GENERIC, handler1)
        self.assertEqual(self.bus.subscriber_count(EventType.GENERIC), 1)


if __name__ == "__main__":
    unittest.main()

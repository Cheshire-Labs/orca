"""Tests for EventChannel and EventChannelRegistry.

Counter-based latch pub/sub for cross-thread communication.
Supports emit/on/branch patterns in V5.1 code-first execution model.
"""

import asyncio

import pytest

from orca.events.event_channel import EventChannel, EventChannelRegistry
from tests.test_helpers import wait_until


class TestPublishAndWait:

    @pytest.mark.asyncio
    async def test_publish_then_wait_returns_value(self) -> None:
        channel = EventChannel("test")
        await channel.publish(value="dilute", data={"plate_map": {"A1": 75}})
        counter, value, data = await channel.wait(seen_counter=0, timeout=1.0)
        assert value == "dilute"
        assert data == {"plate_map": {"A1": 75}}
        assert counter == 1

    @pytest.mark.asyncio
    async def test_wait_then_publish_unblocks(self) -> None:
        channel = EventChannel("test")

        async def delayed_publish() -> None:
            await wait_until(lambda: channel.waiter_count >= 1, timeout=5.0)
            await channel.publish(value="proceed", data={})

        asyncio.create_task(delayed_publish())
        counter, value, data = await channel.wait(seen_counter=0, timeout=2.0)
        assert value == "proceed"
        assert counter == 1

    @pytest.mark.asyncio
    async def test_multiple_waiters_all_receive(self) -> None:
        channel = EventChannel("test")
        results: list[tuple[int, str, dict[str, object]]] = []

        async def waiter() -> None:
            result = await channel.wait(seen_counter=0, timeout=2.0)
            results.append(result)

        t1 = asyncio.create_task(waiter())
        t2 = asyncio.create_task(waiter())
        t3 = asyncio.create_task(waiter())
        await wait_until(lambda: channel.waiter_count >= 3, timeout=5.0)

        await channel.publish(value="broadcast", data={"n": 3})
        await asyncio.gather(t1, t2, t3)

        assert len(results) == 3
        for counter, value, data in results:
            assert value == "broadcast"
            assert counter == 1

    @pytest.mark.asyncio
    async def test_timeout_raises(self) -> None:
        channel = EventChannel("test")
        with pytest.raises(asyncio.TimeoutError):
            await channel.wait(seen_counter=0, timeout=0.1)

    @pytest.mark.asyncio
    async def test_publish_before_wait_not_missed(self) -> None:
        """Counter-based latch: publish increments counter. Wait with
        seen_counter=0 returns immediately if counter > 0."""
        channel = EventChannel("test")
        await channel.publish(value="early", data={})
        # Wait should return immediately since counter (1) > seen_counter (0)
        counter, value, data = await channel.wait(seen_counter=0, timeout=0.1)
        assert value == "early"
        assert counter == 1

    @pytest.mark.asyncio
    async def test_second_wait_blocks_until_second_publish(self) -> None:
        channel = EventChannel("test")
        await channel.publish(value="first", data={})

        counter1, value1, _ = await channel.wait(seen_counter=0, timeout=1.0)
        assert value1 == "first"
        assert counter1 == 1

        # Second wait with seen_counter=1 should block until second publish
        async def delayed_publish() -> None:
            await wait_until(lambda: channel.waiter_count >= 1, timeout=5.0)
            await channel.publish(value="second", data={})

        asyncio.create_task(delayed_publish())
        counter2, value2, _ = await channel.wait(seen_counter=counter1, timeout=2.0)
        assert value2 == "second"
        assert counter2 == 2

    @pytest.mark.asyncio
    async def test_multiple_publishes_waiter_sees_latest(self) -> None:
        channel = EventChannel("test")
        await channel.publish(value="v1", data={})
        await channel.publish(value="v2", data={})
        await channel.publish(value="v3", data={})

        counter, value, _ = await channel.wait(seen_counter=0, timeout=1.0)
        assert value == "v3"
        assert counter == 3


class TestEventChannelRegistry:

    @pytest.mark.asyncio
    async def test_creates_on_first_access(self) -> None:
        registry = EventChannelRegistry()
        channel = registry.get_or_create("conc_result")
        assert isinstance(channel, EventChannel)

    @pytest.mark.asyncio
    async def test_returns_same_channel(self) -> None:
        registry = EventChannelRegistry()
        c1 = registry.get_or_create("conc_result")
        c2 = registry.get_or_create("conc_result")
        assert c1 is c2

    @pytest.mark.asyncio
    async def test_different_names_different_channels(self) -> None:
        registry = EventChannelRegistry()
        c1 = registry.get_or_create("event_a")
        c2 = registry.get_or_create("event_b")
        assert c1 is not c2

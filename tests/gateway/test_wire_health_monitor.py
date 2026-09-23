"""Tests for the periodic stale-connection sweep.

WireHealthMonitor was extracted from ConnectionManager in the timeouts
4-layer refactor. The monitor schedules sweep ticks at its configured
interval and delegates the actual prune call to its ConnectionManager.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from orca.gateway.websocket.health_monitor import WireHealthMonitor
from orca.gateway.websocket.manager import ConnectionManager

from tests.test_helpers import wait_until


@pytest.fixture
def manager() -> ConnectionManager:
    return ConnectionManager()


@pytest.mark.asyncio
class TestWireHealthMonitorLifecycle:
    async def test_start_then_stop_clean(self, manager):
        monitor = WireHealthMonitor(manager, interval_seconds=10.0, timeout_seconds=90)

        async def _noop(_dropped):
            return None

        monitor.start(_noop)
        assert monitor.is_running

        await monitor.stop()
        assert not monitor.is_running

    async def test_stop_without_start_is_safe(self, manager):
        monitor = WireHealthMonitor(manager)
        # Must not raise even though the monitor was never started.
        await monitor.stop()
        assert not monitor.is_running

    async def test_double_stop_is_safe(self, manager):
        monitor = WireHealthMonitor(manager, interval_seconds=10.0)

        async def _noop(_dropped):
            return None

        monitor.start(_noop)
        await monitor.stop()
        await monitor.stop()
        assert not monitor.is_running

    async def test_double_start_is_idempotent(self, manager):
        monitor = WireHealthMonitor(manager, interval_seconds=10.0)

        async def _noop(_dropped):
            return None

        monitor.start(_noop)
        first_task = monitor._task  # internal handle for assertion only
        monitor.start(_noop)
        # Second start must not replace the first task.
        assert monitor._task is first_task
        await monitor.stop()

    async def test_timeout_seconds_governs_stale_drop_decision(self, manager):
        """The configured timeout, not just its getter, decides a drop.

        A connection ~100s past its last heartbeat survives a monitor
        whose timeout exceeds that age, and is dropped by one whose
        timeout is below it.
        """
        ws = Mock()
        ws.close = AsyncMock()
        await manager.connect(
            websocket=ws,
            client_id="lab1-client",
            site="boston", lab="molbio", workcell=None,
        )
        await manager.attach_devices("lab1-client", ["shaker_1"])
        conn = await manager.get_connection("lab1-client")
        conn.last_heartbeat = datetime.now(timezone.utc) - timedelta(seconds=100)

        seen: list[list[tuple[str, list[str]]]] = []

        async def on_dropped(dropped):
            seen.append(dropped)

        tolerant = WireHealthMonitor(
            manager, interval_seconds=0.05, timeout_seconds=200,
        )
        tolerant.start(on_dropped)
        await wait_until(lambda: tolerant.sweep_count >= 1, timeout=5.0)
        await tolerant.stop()
        assert seen == []
        assert manager.get_active_connection_count() == 1

        strict = WireHealthMonitor(
            manager, interval_seconds=0.05, timeout_seconds=30,
        )
        strict.start(on_dropped)
        await wait_until(lambda: len(seen) >= 1, timeout=5.0)
        await strict.stop()
        assert seen == [[("lab1-client", ["shaker_1"])]]
        assert manager.get_active_connection_count() == 0

    async def test_stop_clears_state_when_task_cancelled_externally(self, manager):
        monitor = WireHealthMonitor(manager, interval_seconds=10.0)

        async def _noop(_dropped):
            return None

        monitor.start(_noop)
        # Simulate the task being cancelled by something other than stop()
        # (e.g. event loop shutdown racing the lifespan handler).
        assert monitor._task is not None
        monitor._task.cancel()

        # stop() must not raise the propagated CancelledError, and must
        # leave the monitor in a fully reset state for a subsequent start.
        await monitor.stop()
        assert monitor._task is None
        assert monitor._stop_event is None
        assert not monitor.is_running

        # A follow-up stop is still a no-op.
        await monitor.stop()


@pytest.mark.asyncio
class TestWireHealthMonitorTickBehavior:
    async def test_tick_invokes_callback_with_dropped_pairs(self, manager):
        ws = Mock()
        ws.close = AsyncMock()

        await manager.connect(
            websocket=ws,
            client_id="lab1-client",
            site="boston", lab="molbio", workcell=None,
        )
        await manager.attach_devices("lab1-client", ["shaker_1"])
        conn = await manager.get_connection("lab1-client")
        conn.last_heartbeat = datetime.now(timezone.utc) - timedelta(seconds=300)

        seen: list[list[tuple[str, list[str]]]] = []

        async def on_dropped(dropped):
            seen.append(dropped)

        # Tight interval so the test does not have to wait long.
        monitor = WireHealthMonitor(
            manager, interval_seconds=0.05, timeout_seconds=90,
        )
        monitor.start(on_dropped)
        await wait_until(lambda: len(seen) >= 1, timeout=5.0)
        await monitor.stop()

        assert len(seen) >= 1
        assert seen[0] == [("lab1-client", ["shaker_1"])]

    async def test_no_callback_when_nothing_stale(self, manager):
        ws = Mock()
        ws.close = AsyncMock()

        await manager.connect(
            websocket=ws,
            client_id="lab1-client",
            site="boston", lab="molbio", workcell=None,
        )

        seen: list[list[tuple[str, list[str]]]] = []

        async def on_dropped(dropped):
            seen.append(dropped)

        monitor = WireHealthMonitor(
            manager, interval_seconds=0.05, timeout_seconds=90,
        )
        monitor.start(on_dropped)
        await wait_until(lambda: monitor.sweep_count >= 1, timeout=5.0)
        await monitor.stop()

        assert seen == []

    async def test_callback_exception_does_not_kill_loop(self, manager):
        ws = Mock()
        ws.close = AsyncMock()

        await manager.connect(
            websocket=ws,
            client_id="lab1-client",
            site="boston", lab="molbio", workcell=None,
        )
        conn = await manager.get_connection("lab1-client")
        conn.last_heartbeat = datetime.now(timezone.utc) - timedelta(seconds=300)

        invocations = 0

        async def on_dropped(_dropped):
            nonlocal invocations
            invocations += 1
            raise RuntimeError("boom")

        monitor = WireHealthMonitor(
            manager, interval_seconds=0.05, timeout_seconds=90,
        )
        monitor.start(on_dropped)
        # First tick prunes the stale entry + raises; wait for that callback,
        # then confirm the loop continued past the exception.
        await wait_until(lambda: invocations >= 1, timeout=5.0)
        await monitor.stop()

        assert invocations >= 1
        assert monitor._task is None

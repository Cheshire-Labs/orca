"""Tests for the periodic stale-connection sweep.

The monitor schedules sweep ticks at its configured interval and asks its
ConnectionManager to close each connection that missed its heartbeats.
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

        monitor.start()
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

        monitor.start()
        await monitor.stop()
        await monitor.stop()
        assert not monitor.is_running

    async def test_double_start_is_idempotent(self, manager):
        monitor = WireHealthMonitor(manager, interval_seconds=10.0)

        monitor.start()
        first_task = monitor._task  # internal handle for assertion only
        monitor.start()
        # Second start must not replace the first task.
        assert monitor._task is first_task
        await monitor.stop()

    async def test_timeout_seconds_governs_stale_drop_decision(self, manager):
        """The configured timeout, not just its getter, decides a drop.

        A connection ~100s past its last heartbeat survives a monitor
        whose timeout exceeds that age, and is closed by one whose
        timeout is below it.
        """
        ws = Mock()
        ws.close = AsyncMock()
        await manager.connect(
            websocket=ws,
            client_id="lab1-client",
            site="boston", lab="molbio", workcell=None,
        )
        conn = await manager.get_connection("lab1-client")
        conn.last_heartbeat = datetime.now(timezone.utc) - timedelta(seconds=100)

        tolerant = WireHealthMonitor(
            manager, interval_seconds=0.05, timeout_seconds=200,
        )
        tolerant.start()
        await wait_until(lambda: tolerant.sweep_count >= 1, timeout=5.0)
        await tolerant.stop()
        ws.close.assert_not_awaited()

        strict = WireHealthMonitor(
            manager, interval_seconds=0.05, timeout_seconds=30,
        )
        strict.start()
        await wait_until(lambda: ws.close.await_count >= 1, timeout=5.0)
        await strict.stop()
        ws.close.assert_awaited_once_with(code=1000, reason="Heartbeat timeout")

    async def test_stop_clears_state_when_task_cancelled_externally(self, manager):
        monitor = WireHealthMonitor(manager, interval_seconds=10.0)

        monitor.start()
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
    async def test_a_tick_closes_a_stale_connection(self, manager):
        ws = Mock()
        ws.close = AsyncMock()
        await manager.connect(
            websocket=ws,
            client_id="lab1-client",
            site="boston", lab="molbio", workcell=None,
        )
        conn = await manager.get_connection("lab1-client")
        conn.last_heartbeat = datetime.now(timezone.utc) - timedelta(seconds=300)

        monitor = WireHealthMonitor(
            manager, interval_seconds=0.05, timeout_seconds=90,
        )
        monitor.start()
        await wait_until(lambda: monitor.sweep_count >= 2, timeout=5.0)
        await monitor.stop()

        ws.close.assert_awaited_once_with(code=1000, reason="Heartbeat timeout")

    async def test_nothing_is_closed_when_nothing_is_stale(self, manager):
        ws = Mock()
        ws.close = AsyncMock()
        await manager.connect(
            websocket=ws,
            client_id="lab1-client",
            site="boston", lab="molbio", workcell=None,
        )

        monitor = WireHealthMonitor(
            manager, interval_seconds=0.05, timeout_seconds=90,
        )
        monitor.start()
        await wait_until(lambda: monitor.sweep_count >= 1, timeout=5.0)
        await monitor.stop()

        ws.close.assert_not_awaited()

    async def test_a_failed_tick_does_not_stop_the_sweep(self):
        failing = Mock()
        failing.close_stale_connections = AsyncMock(side_effect=RuntimeError("boom"))

        monitor = WireHealthMonitor(failing, interval_seconds=0.05, timeout_seconds=90)
        monitor.start()
        await wait_until(lambda: monitor.sweep_count >= 2, timeout=5.0)
        await monitor.stop()

        assert failing.close_stale_connections.await_count >= 2

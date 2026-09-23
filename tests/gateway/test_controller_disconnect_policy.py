"""Tests for the PAUSE_AND_WAIT(timeout) -> FAIL disconnect policy."""

import asyncio
from datetime import datetime
from typing import Awaitable, Callable, Optional
from unittest.mock import AsyncMock, Mock, patch

import pytest

from orca.runtime.run_modes import WorkflowRunMode

from orca.gateway.controller import (
    DeviceController,
    DeviceOfflineError,
)
from orca.gateway.controller.controller import _PendingCommand
from orca.gateway.controller.disconnect_grace import DisconnectGrace
from cheshire_drivers.gateway_protocol import ResponseMessage
from orca.gateway.registry.snapshot import DeviceSnapshot


def _grace_with_resolver(
    resolver: Callable[[str], Awaitable[Optional[float]]],
) -> DisconnectGrace:
    """Build a DisconnectGrace pre-wired with a topology resolver for tests."""
    grace = DisconnectGrace()
    grace.set_topology_resolver(resolver)
    return grace


def _snapshot(
    device_id: str = "shaker_1",
    type: str = "shaker",
    interfaces: list[str] | None = None,
    capabilities: list[str] | None = None,
) -> DeviceSnapshot:
    return DeviceSnapshot(
        type=type,
        name=device_id,
        interfaces=interfaces or ["IShaker"],
        capabilities=capabilities or [],
        provides_state=False,
        methods={},
        site="test", lab="test", workcell=None,
        status="ready", last_seen=datetime.utcnow(),
    )


@pytest.fixture
def controller() -> DeviceController:
    return DeviceController()


@pytest.mark.asyncio
class TestDisconnectTimeoutResolution:
    """The controller delegates disconnect-grace resolution to its
    ``DisconnectGrace`` instance. The grace-class precedence (topology
    override -> fallback) is covered in ``test_disconnect_grace.py``;
    these tests verify the controller is wired through correctly."""

    async def test_resolver_value_flows_through_grace(
        self, controller: DeviceController,
    ):
        async def resolver(_device_id: str) -> float | None:
            return 42.0
        controller.set_disconnect_grace(_grace_with_resolver(resolver))

        out = await controller._resolve_disconnect_timeout("shaker_1")

        assert out == 42.0

    async def test_default_grace_uses_fallback(
        self, controller: DeviceController,
    ):
        """A freshly-constructed controller has no topology resolver
        installed; its DisconnectGrace falls back to the conservative
        default."""
        out = await controller._resolve_disconnect_timeout("shaker_1")

        assert out == DisconnectGrace.DEFAULT_FALLBACK_SECONDS


def _make_pending(
    device_id: str = "shaker_1",
    timeout_seconds: float = 30.0,
) -> _PendingCommand:
    """Build a _PendingCommand with a fresh future + no timers running."""
    return _PendingCommand(
        command_id="cmd_1",
        device_id=device_id,
        command="shake",
        params={"speed": 500.0, "duration": 1.0},
        effective_mode=WorkflowRunMode.LIVE,
        timeout_seconds=timeout_seconds,
        future=asyncio.Future(),
    )


@pytest.mark.asyncio
class TestOnDeviceDisconnected:
    async def test_no_pending_command_is_noop(self, controller: DeviceController):
        # Should not raise even when nothing is in-flight.
        await controller.on_device_disconnected("shaker_1")

    async def test_disconnect_arms_disconnect_timer(
        self, controller: DeviceController,
    ):
        """When a command is pending, the disconnect listener arms a timer.

        Use a tiny per-kind default by short-circuiting the resolver so
        the test doesn't have to wait for the real shaker default.
        """
        pending = _make_pending()
        controller._pending["shaker_1"] = pending
        # Resolver returns 0.05s so we can verify the future fails fast.
        async def resolver(_device_id: str) -> float | None:
            return 0.05
        controller.set_disconnect_grace(_grace_with_resolver(resolver))

        await controller.on_device_disconnected("shaker_1")

        assert pending.disconnect_timer is not None
        # Wait for the timer to fire. After grace, the future fails with
        # DeviceOfflineError.
        with pytest.raises(DeviceOfflineError):
            await asyncio.wait_for(pending.future, timeout=1.0)

    async def test_double_disconnect_is_idempotent(
        self, controller: DeviceController,
    ):
        pending = _make_pending()
        controller._pending["shaker_1"] = pending
        async def resolver(_device_id: str) -> float | None:
            return 5.0
        controller.set_disconnect_grace(_grace_with_resolver(resolver))

        await controller.on_device_disconnected("shaker_1")
        first_timer = pending.disconnect_timer
        await controller.on_device_disconnected("shaker_1")

        # Same timer; not replaced. (Don't await it -- it would fire
        # DeviceOfflineError after 5s.)
        assert pending.disconnect_timer is first_timer
        # Cleanup so the test process doesn't have a runaway task.
        if pending.disconnect_timer is not None:
            pending.disconnect_timer.cancel()


@pytest.mark.asyncio
class TestOnDeviceReconnected:
    async def test_no_pending_is_noop(self, controller: DeviceController):
        await controller.on_device_reconnected("shaker_1")

    async def test_reconnect_without_prior_disconnect_is_noop(
        self, controller: DeviceController,
    ):
        """A connect signal that didn't follow a disconnect is just a regular connect."""
        pending = _make_pending()
        controller._pending["shaker_1"] = pending
        # Pending is in-flight but never disconnected.
        await controller.on_device_reconnected("shaker_1")

        # No state change.
        assert pending.disconnect_timer is None
        assert not pending.future.done()

    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_reconnect_resends_command_and_cancels_disconnect_timer(
        self,
        mock_registry: Mock,
        mock_conn_manager: Mock,
        controller: DeviceController,
    ):
        """Reconnect within grace cancels the disconnect timer + re-sends."""
        pending = _make_pending()
        controller._pending["shaker_1"] = pending
        # Simulate a prior disconnect: arm the timer with a long timeout
        # so it doesn't fire during the test.
        async def resolver(_device_id: str) -> float | None:
            return 60.0
        controller.set_disconnect_grace(_grace_with_resolver(resolver))
        await controller.on_device_disconnected("shaker_1")
        prior_timer = pending.disconnect_timer
        assert prior_timer is not None

        # Reconnect plumbing: tracker reports a new client_id; manager
        # accepts the resend.
        mock_registry.get_client_for_device = AsyncMock(return_value="client_2")
        mock_conn_manager.send_to_client = AsyncMock(return_value=True)

        await controller.on_device_reconnected("shaker_1")
        # Yield the loop so the cancelled timer transitions to done().
        await asyncio.sleep(0)

        assert prior_timer.cancelled() or prior_timer.done()
        assert pending.disconnect_timer is None
        # New command timer armed for the post-reconnect command timeout.
        assert pending.command_timer is not None
        # The resend went out.
        mock_conn_manager.send_to_client.assert_awaited_once()

        # Cleanup.
        if pending.command_timer is not None:
            pending.command_timer.cancel()

    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_reconnect_with_no_client_holds_command(
        self,
        mock_registry: Mock,
        mock_conn_manager: Mock,
        controller: DeviceController,
    ):
        """If reconnect signal lands but no client is registered yet, hold."""
        pending = _make_pending()
        controller._pending["shaker_1"] = pending
        async def resolver(_device_id: str) -> float | None:
            return 60.0
        controller.set_disconnect_grace(_grace_with_resolver(resolver))
        await controller.on_device_disconnected("shaker_1")

        mock_registry.get_client_for_device = AsyncMock(return_value=None)

        await controller.on_device_reconnected("shaker_1")

        # Command not resent; future remains pending.
        mock_conn_manager.send_to_client.assert_not_called()
        assert not pending.future.done()

        # Cleanup.
        if pending.disconnect_timer is not None:
            pending.disconnect_timer.cancel()


@pytest.mark.asyncio
class TestDisconnectThenResponse:
    """Race: a response lands while the disconnect-timer resolver is awaited."""

    async def test_response_during_disconnect_resolves_normally(
        self, controller: DeviceController,
    ):
        pending = _make_pending()
        controller._pending["shaker_1"] = pending
        controller._command_futures["cmd_1"] = pending.future

        # Slow resolver so we can race a response in.
        gate = asyncio.Event()
        resolver_entered = asyncio.Event()

        async def resolver(_device_id: str) -> float | None:
            resolver_entered.set()
            await gate.wait()
            return 0.05

        controller.set_disconnect_grace(_grace_with_resolver(resolver))

        # Kick off disconnect; it'll wait inside the resolver.
        disc_task = asyncio.create_task(
            controller.on_device_disconnected("shaker_1"),
        )
        await resolver_entered.wait()
        # Response lands while resolver is gated.
        await controller.handle_response(
            ResponseMessage(
                command_id="cmd_1",
                success=True,
                result={"ok": True},
                error=None,
                error_type=None,
            )
        )
        # Release resolver.
        gate.set()
        await disc_task

        # Future resolved with the response, not DeviceOfflineError.
        assert pending.future.done()
        assert pending.future.result() == {"ok": True}
        # Disconnect timer not armed because future was already done.
        assert pending.disconnect_timer is None

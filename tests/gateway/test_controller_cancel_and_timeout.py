"""Controller timeout and wire-cancel behaviour.

The engine owns recoverable timeouts; the controller no longer declares
incidents or holds operator decisions. What the controller DOES own:

* ad-hoc (no-execution_id) commands get a fail-fast ``command_timer`` that,
  on expiry, fails the future AND sends a ``CancelMessage`` so the device
  bridge drops the in-flight command;
* workflow (execution_id set) commands arm NO gateway timer -- the engine
  coordinator bounds them -- and on engine abort/mark-complete the awaiting
  ``execute_command`` is cancelled, which sends a ``CancelMessage``;
* the disconnect/reconnect resend re-arms the fail-fast timer only for
  ad-hoc commands;
* remote drivers tag every outbound command with the thread's execution id.
"""

import asyncio
from datetime import datetime
from typing import Any, Dict
from unittest.mock import AsyncMock, Mock, patch

import pytest

from cheshire_drivers.command_timings import collect_command_timings
from cheshire_drivers.gateway_protocol import CancelMessage, MessageEnvelope
from orca.runtime.run_modes import WorkflowRunMode, current_execution_id

from orca.gateway.controller.controller import DeviceController, _PendingCommand
from orca.gateway.controller.disconnect_grace import DisconnectGrace
from orca.gateway.controller.exceptions import CommandTimeoutError
from orca.gateway.registry.snapshot import DeviceSnapshot
from tests.test_helpers import wait_until


@pytest.fixture
def controller() -> DeviceController:
    return DeviceController()


def _snapshot(device_id: str = "shaker_1") -> DeviceSnapshot:
    return DeviceSnapshot(
        type="shaker", name=device_id, interfaces=["IShaker"], capabilities=[],
        provides_state=False, methods={}, site="test", lab="test", workcell=None,
        status="ready", last_seen=datetime.utcnow(),
    )


def _make_pending(
    *, execution_id: str | None = None, timeout_seconds: float = 30.0,
) -> _PendingCommand:
    return _PendingCommand(
        command_id="cmd_1", device_id="shaker_1", command="shake", params={},
        effective_mode=WorkflowRunMode.LIVE, timeout_seconds=timeout_seconds,
        future=asyncio.Future(), execution_id=execution_id,
    )


def _grace_with_resolver(seconds: float) -> DisconnectGrace:
    grace = DisconnectGrace()

    async def resolver(_device_id: str) -> float | None:
        return seconds

    grace.set_topology_resolver(resolver)
    return grace


def _decode_cancel(payload: str) -> CancelMessage:
    msg = MessageEnvelope.model_validate_json(payload).unwrap()
    assert isinstance(msg, CancelMessage)
    return msg


@pytest.mark.asyncio
class TestSendCancel:
    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_send_cancel_emits_cancel_message(
        self, mock_tracker: Mock, mock_mgr: Mock, controller: DeviceController,
    ) -> None:
        mock_tracker.get_client_for_device = AsyncMock(return_value="client_1")
        sent: list[tuple[str, str]] = []

        async def capture(client_id: str, payload: str) -> bool:
            sent.append((client_id, payload))
            return True

        mock_mgr.send_to_client = AsyncMock(side_effect=capture)

        await controller._send_cancel("shaker_1", "cmd_42")

        assert len(sent) == 1
        client_id, payload = sent[0]
        assert client_id == "client_1"
        assert _decode_cancel(payload).command_id == "cmd_42"

    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_send_cancel_no_client_is_noop(
        self, mock_tracker: Mock, mock_mgr: Mock, controller: DeviceController,
    ) -> None:
        mock_tracker.get_client_for_device = AsyncMock(return_value=None)
        mock_mgr.send_to_client = AsyncMock(return_value=True)

        await controller._send_cancel("shaker_1", "cmd_42")

        mock_mgr.send_to_client.assert_not_called()


@pytest.mark.asyncio
class TestAdHocCommandTimer:
    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_adhoc_timeout_fails_future_and_sends_cancel(
        self, mock_tracker: Mock, mock_mgr: Mock, controller: DeviceController,
    ) -> None:
        mock_tracker.get_client_for_device = AsyncMock(return_value="client_1")
        sent: list[str] = []

        async def capture(client_id: str, payload: str) -> bool:
            sent.append(payload)
            return True

        mock_mgr.send_to_client = AsyncMock(side_effect=capture)

        pending = _make_pending(execution_id=None, timeout_seconds=0.05)
        await controller._command_timeout_watcher(pending)

        assert pending.future.done()
        with pytest.raises(CommandTimeoutError):
            pending.future.result()
        # Wait for the scheduled _send_cancel to reach the wire.
        await wait_until(lambda: len(sent) == 1)
        assert len(sent) == 1
        assert _decode_cancel(sent[0]).command_id == "cmd_1"

    async def test_watcher_cancelled_before_fire_is_noop(
        self, controller: DeviceController,
    ) -> None:
        pending = _make_pending(execution_id=None, timeout_seconds=5.0)
        task = asyncio.create_task(controller._command_timeout_watcher(pending))
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert not pending.future.done()


@pytest.mark.asyncio
class TestExecuteCommandCancel:
    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_cancel_during_await_emits_wire_cancel_and_reraises(
        self, mock_tracker: Mock, mock_mgr: Mock, controller: DeviceController,
    ) -> None:
        mock_tracker.get_client_for_device = AsyncMock(return_value="client_1")
        sent: list[str] = []

        async def capture(client_id: str, payload: str) -> bool:
            sent.append(payload)
            return True

        mock_mgr.send_to_client = AsyncMock(side_effect=capture)

        dispatched: Dict[str, str] = {}

        async def fake_dispatch(
            device_id: str, command_id: str, *args: Any, **kwargs: Any,
        ) -> None:
            dispatched["command_id"] = command_id

        controller._validate_command = AsyncMock(return_value=_snapshot())
        controller._reserve_device = AsyncMock(return_value=None)
        controller._dispatch_command = fake_dispatch  # type: ignore[method-assign]

        task = asyncio.create_task(
            controller.execute_command(
                device_id="shaker_1", command="shake", params={},
                timeout_seconds=5.0, effective_mode=WorkflowRunMode.LIVE,
                execution_id="exec-1",
            )
        )
        # Wait for dispatch so the task has reached `await command_future`.
        await wait_until(lambda: "command_id" in dispatched)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Wait for the scheduled _send_cancel to reach the wire.
        await wait_until(lambda: len(sent) == 1)

        assert len(sent) == 1
        assert _decode_cancel(sent[0]).command_id == dispatched["command_id"]


    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_the_scheduled_cancel_is_something_a_caller_can_wait_for(
        self, mock_tracker: Mock, mock_mgr: Mock, controller: DeviceController,
    ) -> None:
        """The cancel is scheduled, not awaited, so the unwind does not block on
        the wire. Nothing held that task: it could be collected mid-send, and
        shutdown had no way to wait for it before closing the connection."""
        mock_tracker.get_client_for_device = AsyncMock(return_value="client_1")
        sent: list[str] = []
        release = asyncio.Event()

        async def capture(client_id: str, payload: str) -> bool:
            await release.wait()
            sent.append(payload)
            return True

        mock_mgr.send_to_client = AsyncMock(side_effect=capture)

        dispatched: Dict[str, str] = {}

        async def fake_dispatch(
            device_id: str, command_id: str, *args: Any, **kwargs: Any,
        ) -> None:
            dispatched["command_id"] = command_id

        controller._validate_command = AsyncMock(return_value=_snapshot())
        controller._reserve_device = AsyncMock(return_value=None)
        controller._dispatch_command = fake_dispatch  # type: ignore[method-assign]

        task = asyncio.create_task(
            controller.execute_command(
                device_id="shaker_1", command="shake", params={},
                timeout_seconds=5.0, effective_mode=WorkflowRunMode.LIVE,
                execution_id="exec-1",
            )
        )
        await wait_until(lambda: "command_id" in dispatched)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert sent == [], "the send is still held up on the wire"
        drain = asyncio.create_task(controller.await_cancels_sent())
        release.set()
        await asyncio.wait_for(drain, timeout=10.0)

        assert len(sent) == 1, "the wait returned before the cancel went out"
        assert _decode_cancel(sent[0]).command_id == dispatched["command_id"]


    async def test_a_cancel_scheduled_while_draining_is_waited_for_too(
        self, controller: DeviceController,
    ) -> None:
        """A shutdown tears failed executions down after it drains, and a
        teardown cancelling a thread mid-command schedules one more cancel. A
        drain that read the set once returned before that one went anywhere."""
        first, second = asyncio.Event(), asyncio.Event()
        sent: list[str] = []

        async def blocking_send(device_id: str, command_id: str) -> None:
            if command_id == "cmd_1":
                await first.wait()
                controller._send_cancel_detached("shaker_1", "cmd_2")
            else:
                await second.wait()
            sent.append(command_id)

        controller._send_cancel = blocking_send  # type: ignore[method-assign]
        controller._send_cancel_detached("shaker_1", "cmd_1")
        drain = asyncio.create_task(controller.await_cancels_sent())
        # One turn: the first send parks, then the drain reads the set with
        # only that send in it. The second send does not exist yet.
        await asyncio.sleep(0)
        first.set()
        await wait_until(lambda: sent == ["cmd_1"])

        assert not drain.done(), "the drain returned at the cancels it read first"
        second.set()
        await asyncio.wait_for(drain, timeout=10.0)

        assert sent == ["cmd_1", "cmd_2"]


def test_a_cancel_left_behind_by_a_closed_loop_does_not_break_the_next_drain() -> None:
    """The controller is a module singleton, so its sends outlive the loop that
    made them. Waiting on another loop's task raises, and a shutdown that raises
    here never gets to close anything."""
    controller = DeviceController()

    async def never_returns(device_id: str, command_id: str) -> None:
        await asyncio.Event().wait()

    controller._send_cancel = never_returns  # type: ignore[method-assign]
    loop = asyncio.new_event_loop()

    async def schedule() -> None:
        controller._send_cancel_detached("shaker_1", "cmd_1")

    loop.run_until_complete(schedule())
    loop.close()
    assert controller._cancel_sends, "the test needs a send left behind"

    asyncio.run(controller.await_cancels_sent())

    assert controller._cancel_sends == set(), (
        "a send on a closed loop can never call back and must be dropped"
    )


@pytest.mark.asyncio
class TestTimerArmingDiscriminator:
    async def _drive_until_await(
        self, controller: DeviceController, execution_id: str | None,
    ) -> tuple[asyncio.Task[Any], AsyncMock]:
        arm_spy = AsyncMock()
        controller._validate_command = AsyncMock(return_value=_snapshot())
        controller._reserve_device = AsyncMock(return_value=None)
        dispatch_spy = AsyncMock(return_value=None)
        controller._dispatch_command = dispatch_spy
        controller._arm_command_timer = arm_spy  # type: ignore[method-assign]
        task = asyncio.create_task(
            controller.execute_command(
                device_id="shaker_1", command="shake", params={},
                timeout_seconds=5.0, effective_mode=WorkflowRunMode.LIVE,
                execution_id=execution_id,
            )
        )
        await wait_until(lambda: dispatch_spy.called)
        return task, arm_spy

    async def test_workflow_command_arms_no_gateway_timer(
        self, controller: DeviceController,
    ) -> None:
        task, arm_spy = await self._drive_until_await(controller, "exec-1")
        arm_spy.assert_not_awaited()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_adhoc_command_arms_gateway_timer(
        self, controller: DeviceController,
    ) -> None:
        task, arm_spy = await self._drive_until_await(controller, None)
        arm_spy.assert_awaited_once()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
class TestReconnectReArmGate:
    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_workflow_reconnect_does_not_rearm_command_timer(
        self, mock_tracker: Mock, mock_mgr: Mock, controller: DeviceController,
    ) -> None:
        pending = _make_pending(execution_id="exec-1")
        controller._pending["shaker_1"] = pending
        controller.set_disconnect_grace(_grace_with_resolver(60.0))
        await controller.on_device_disconnected("shaker_1")

        mock_tracker.get_client_for_device = AsyncMock(return_value="client_2")
        mock_mgr.send_to_client = AsyncMock(return_value=True)

        await controller.on_device_reconnected("shaker_1")
        await asyncio.sleep(0)

        # Engine bounds the workflow command; no gateway timer re-armed...
        assert pending.command_timer is None
        # ...but the resend still happens.
        mock_mgr.send_to_client.assert_awaited_once()
        if pending.disconnect_timer is not None:
            pending.disconnect_timer.cancel()


@pytest.mark.asyncio
class TestRemoteDriverExecutionIdTagging:
    async def test_send_tags_execution_id_from_contextvar(self) -> None:
        from orca.gateway.remote_drivers import RemoteShakerDriver

        recorded: Dict[str, Any] = {}

        class _FakeController(DeviceController):
            async def execute_command(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
                recorded.update(kwargs)
                return {}

        driver = RemoteShakerDriver(
            "shaker_1", _FakeController(), lambda name: WorkflowRunMode.LIVE,
        )
        token = current_execution_id.set("exec-77")
        try:
            await driver.stop()
        finally:
            current_execution_id.reset(token)

        assert recorded["execution_id"] == "exec-77"
        assert recorded["device_id"] == "shaker_1"

    async def test_send_execution_id_none_outside_any_thread(self) -> None:
        from orca.gateway.remote_drivers import RemoteShakerDriver

        recorded: Dict[str, Any] = {}

        class _FakeController(DeviceController):
            async def execute_command(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
                recorded.update(kwargs)
                return {}

        driver = RemoteShakerDriver(
            "shaker_1", _FakeController(), lambda name: WorkflowRunMode.LIVE,
        )
        await driver.stop()

        assert recorded["execution_id"] is None


def test_remote_shaker_shake_timing_is_advertised() -> None:
    """L2 pin: cloud commands stay engine-bounded only because the IShaker
    interface's ``@command_timing`` survives the RemoteShakerDriver MRO."""
    from orca.gateway.remote_drivers import RemoteShakerDriver

    timings = collect_command_timings(RemoteShakerDriver)
    assert "shake" in timings
    assert timings["shake"].max_seconds is not None

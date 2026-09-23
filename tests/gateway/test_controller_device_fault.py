"""A command that does not come back clean latches a fault on its device.

The bench case these pin: a gripper move failed mid-hop and left a plate in the
jaws, the device went straight back to ready, and the next thread's park moved
the gantry with the plate still held. Nothing between the two commands knew the
machine had been left part-way through something.
"""

import asyncio
from datetime import datetime
from typing import Any, Dict
from unittest.mock import AsyncMock, Mock, patch

import pytest

from orca.gateway.controller.command_kind import CommandKind
from orca.runtime.status_models import (
    DeviceUnionEntry,
    GatewayDeviceEntry,
    TopologyDeviceEntry,
)
from orca.gateway import adhoc
from orca.gateway.controller.controller import DeviceController
from orca.gateway.device_fault import FAULTED_STATUS, DeviceFaultOutcome
from orca.gateway.controller.exceptions import (
    CommandExecutionError,
    CommandTimeoutError,
    DeviceFaultedError,
    DeviceOfflineError,
    InvalidCommandError,
)
from orca.gateway.registry.snapshot import DeviceSnapshot
from orca.daemon.schemas import DeviceDTO, DeviceRegistryEntryDTO
from orca.cli.control_plane import (
    DeviceRegistryEntryDTO as CliDeviceRegistryEntryDTO,
)
from cheshire_drivers.driver_errors import InstrumentOutcome
from orca.runtime.run_modes import WorkflowRunMode
from tests.test_helpers import wait_until


@pytest.fixture
def controller() -> DeviceController:
    return DeviceController()


def _snapshot(device_id: str = "flex_1") -> DeviceSnapshot:
    return DeviceSnapshot(
        type="liquid_handler", name=device_id, interfaces=["ILiquidHandlerDriver"],
        capabilities=[], provides_state=False, methods={}, site="test", lab="test",
        workcell=None, status="ready", last_seen=datetime.utcnow(),
    )


def _stub_preflight(controller: DeviceController) -> Dict[str, str]:
    """Let execute_command reach the wire without a registry, and say when."""
    dispatched: Dict[str, str] = {}

    async def fake_dispatch(
        device_id: str, command_id: str, *args: Any, **kwargs: Any,
    ) -> None:
        dispatched["command_id"] = command_id

    setattr(controller, "_validate_command", AsyncMock(return_value=_snapshot()))
    setattr(controller, "_dispatch_command", fake_dispatch)
    return dispatched


async def _run_and_fail(
    controller: DeviceController,
    error: BaseException,
    *,
    command: str = "move_plate",
    execution_id: str | None = "exec-1",
    kind: CommandKind = CommandKind.ACTUATION,
) -> None:
    """Dispatch a command, then settle its future with ``error``."""
    dispatched = _stub_preflight(controller)
    task = asyncio.create_task(
        controller.execute_command(
            device_id="flex_1", command=command, params={},
            timeout_seconds=5.0, effective_mode=WorkflowRunMode.LIVE,
            execution_id=execution_id, kind=kind,
        )
    )
    await wait_until(lambda: "command_id" in dispatched)
    controller._command_futures[dispatched["command_id"]].set_exception(error)
    with pytest.raises(type(error)):
        await task


async def _run_and_succeed(
    controller: DeviceController,
    *,
    command: str,
    execution_id: str | None = None,
) -> None:
    """Dispatch a command and settle its future clean."""
    dispatched = _stub_preflight(controller)
    task = asyncio.create_task(
        controller.execute_command(
            device_id="flex_1", command=command, params={},
            timeout_seconds=5.0, effective_mode=WorkflowRunMode.LIVE,
            execution_id=execution_id,
        )
    )
    await wait_until(lambda: "command_id" in dispatched)
    controller._command_futures[dispatched["command_id"]].set_result(None)
    await task


@pytest.mark.asyncio
class TestACommandThatFailsLatchesAFault:
    async def test_driver_failure_latches_a_failed_fault_naming_the_command(
        self, controller: DeviceController,
    ) -> None:
        await _run_and_fail(
            controller,
            CommandExecutionError("Stall or Collision Detected", "CommandExecutionError"),
        )

        fault = controller.fault("flex_1")
        assert fault is not None
        assert fault.outcome is DeviceFaultOutcome.FAILED
        assert fault.command == "move_plate"
        assert fault.may_still_be_moving is False
        assert "Stall or Collision Detected" in fault.error

    async def test_a_timeout_latches_an_unknown_fault(
        self, controller: DeviceController,
    ) -> None:
        await _run_and_fail(controller, CommandTimeoutError("no answer in 60s"))

        fault = controller.fault("flex_1")
        assert fault is not None
        assert fault.outcome is DeviceFaultOutcome.UNKNOWN
        assert fault.may_still_be_moving is True

    async def test_a_device_that_drops_and_stays_away_latches_an_unknown_fault(
        self, controller: DeviceController,
    ) -> None:
        await _run_and_fail(controller, DeviceOfflineError("flex_1 disconnected"))

        fault = controller.fault("flex_1")
        assert fault is not None
        assert fault.outcome is DeviceFaultOutcome.UNKNOWN

    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_a_cancel_mid_flight_latches_an_unknown_fault(
        self, mock_tracker: Mock, mock_mgr: Mock, controller: DeviceController,
    ) -> None:
        mock_tracker.get_client_for_device = AsyncMock(return_value="client_1")
        mock_mgr.send_to_client = AsyncMock(return_value=True)
        dispatched = _stub_preflight(controller)

        task = asyncio.create_task(
            controller.execute_command(
                device_id="flex_1", command="move_plate", params={},
                timeout_seconds=5.0, effective_mode=WorkflowRunMode.LIVE,
                execution_id="exec-1",
            )
        )
        await wait_until(lambda: "command_id" in dispatched)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        fault = controller.fault("flex_1")
        assert fault is not None
        assert fault.outcome is DeviceFaultOutcome.UNKNOWN

    async def test_the_first_fault_is_the_one_kept(
        self, controller: DeviceController,
    ) -> None:
        await _run_and_fail(
            controller, CommandExecutionError("stall", "CommandExecutionError"),
            command="move_plate",
        )
        await _run_and_fail(
            controller, CommandExecutionError("later trouble", "CommandExecutionError"),
            command="park_gantry", execution_id=None,
        )

        fault = controller.fault("flex_1")
        assert fault is not None
        assert fault.command == "move_plate"


@pytest.mark.asyncio
class TestNothingReachedTheMachineLeavesNoFault:
    async def test_a_command_refused_before_dispatch_latches_nothing(
        self, controller: DeviceController,
    ) -> None:
        controller._validate_command = AsyncMock(
            side_effect=InvalidCommandError("flex_1 cannot shake")
        )

        with pytest.raises(InvalidCommandError):
            await controller.execute_command(
                device_id="flex_1", command="shake", params={},
                effective_mode=WorkflowRunMode.LIVE, execution_id="exec-1",
            )

        assert controller.fault("flex_1") is None

    async def test_a_send_that_never_left_latches_nothing(
        self, controller: DeviceController,
    ) -> None:
        controller._validate_command = AsyncMock(return_value=_snapshot())
        controller._dispatch_command = AsyncMock(
            side_effect=DeviceOfflineError("flex_1 has no connected client")
        )

        with pytest.raises(DeviceOfflineError):
            await controller.execute_command(
                device_id="flex_1", command="move_plate", params={},
                effective_mode=WorkflowRunMode.LIVE, execution_id="exec-1",
            )

        assert controller.fault("flex_1") is None


@pytest.mark.asyncio
class TestAFaultedDeviceRefusesTheEngine:
    async def test_engine_command_is_refused_and_never_reaches_the_wire(
        self, controller: DeviceController,
    ) -> None:
        await _run_and_fail(
            controller, CommandExecutionError("stall", "CommandExecutionError"),
        )
        dispatch_spy = AsyncMock(return_value=None)
        controller._dispatch_command = dispatch_spy

        # Bounded so a regression that lets the command through fails here
        # instead of hanging on a future nothing will settle.
        with pytest.raises(DeviceFaultedError) as raised:
            await asyncio.wait_for(
                controller.execute_command(
                    device_id="flex_1", command="park_gantry", params={},
                    effective_mode=WorkflowRunMode.LIVE, execution_id="exec-1",
                ),
                timeout=5.0,
            )

        dispatch_spy.assert_not_called()
        assert raised.value.fault.command == "move_plate"
        assert "move_plate" in str(raised.value)

    async def test_an_operator_command_still_reaches_the_device(
        self, controller: DeviceController,
    ) -> None:
        await _run_and_fail(
            controller, CommandExecutionError("stall", "CommandExecutionError"),
        )
        dispatched = _stub_preflight(controller)

        task = asyncio.create_task(
            controller.execute_command(
                device_id="flex_1", command="release_jaw", params={},
                timeout_seconds=5.0, effective_mode=WorkflowRunMode.LIVE,
                execution_id=None,
            )
        )
        await wait_until(lambda: "command_id" in dispatched)
        controller._command_futures[dispatched["command_id"]].set_result(None)
        assert await task is None

    async def test_another_device_is_unaffected(
        self, controller: DeviceController,
    ) -> None:
        await _run_and_fail(
            controller, CommandExecutionError("stall", "CommandExecutionError"),
        )
        assert controller.fault("pf400_1") is None

    async def test_clearing_gives_the_device_back(
        self, controller: DeviceController,
    ) -> None:
        await _run_and_fail(
            controller, CommandExecutionError("stall", "CommandExecutionError"),
        )

        cleared = await controller.clear_fault("flex_1")
        assert cleared is not None
        assert cleared.command == "move_plate"
        assert controller.fault("flex_1") is None
        assert await controller.clear_fault("flex_1") is None


@pytest.mark.asyncio
class TestAWorldSyncOpLeavesNoFault:
    """A world-sync op pushes deck state into a driver's own model of the deck
    and moves no hardware, so no failure of one can have left the instrument
    part-way through anything.

    Every fault-latching site is guarded on that, and a world-sync op that
    SUCCEEDS reaches none of them. These fail one on purpose, each by the
    route that reaches a different guard.
    """

    async def test_a_world_sync_op_runs_while_the_device_is_faulted(
        self, controller: DeviceController,
    ) -> None:
        """The engine will not drive a faulted device, but it still has to be
        able to correct its model of one."""
        await _run_and_fail(
            controller, CommandExecutionError("stall", "CommandExecutionError"),
        )
        dispatched = _stub_preflight(controller)

        task = asyncio.create_task(
            controller.execute_command(
                device_id="flex_1", command="ensure_seeded", params={},
                timeout_seconds=5.0, effective_mode=WorkflowRunMode.LIVE,
                execution_id="exec-1", kind=CommandKind.WORLD_SYNC,
            )
        )
        await wait_until(lambda: "command_id" in dispatched)
        controller._command_futures[dispatched["command_id"]].set_result(None)
        assert await task is None

        standing = controller.fault("flex_1")
        assert standing is not None
        assert standing.command == "move_plate"

    async def test_a_world_sync_op_that_fails_latches_no_fault(
        self, controller: DeviceController,
    ) -> None:
        await _run_and_fail(
            controller,
            CommandExecutionError("driver refused the seed", "CommandExecutionError"),
            command="ensure_seeded", kind=CommandKind.WORLD_SYNC,
        )

        assert controller.fault("flex_1") is None

    async def test_a_world_sync_op_that_never_answers_latches_no_fault(
        self, controller: DeviceController,
    ) -> None:
        """A slow websocket is not a stalled instrument. The same timeout on a
        move latches UNKNOWN, because that one may still be moving."""
        await _run_and_fail(
            controller, CommandTimeoutError("no answer in 5s"),
            command="seed_position", kind=CommandKind.WORLD_SYNC,
        )

        assert controller.fault("flex_1") is None

    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_a_cancelled_world_sync_op_latches_no_fault(
        self, mock_tracker: Mock, mock_mgr: Mock, controller: DeviceController,
    ) -> None:
        """A cancel of a move latches UNKNOWN because the device bridge task
        stops and the motion does not. A seed had no motion to leave running."""
        mock_tracker.get_client_for_device = AsyncMock(return_value="client_1")
        mock_mgr.send_to_client = AsyncMock(return_value=True)
        dispatched = _stub_preflight(controller)

        task = asyncio.create_task(
            controller.execute_command(
                device_id="flex_1", command="ensure_seeded", params={},
                timeout_seconds=5.0, effective_mode=WorkflowRunMode.LIVE,
                execution_id="exec-1", kind=CommandKind.WORLD_SYNC,
            )
        )
        await wait_until(lambda: "command_id" in dispatched)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert controller.fault("flex_1") is None

    async def test_a_world_sync_op_does_not_clear_a_fault(
        self, controller: DeviceController,
    ) -> None:
        """Clearing on a clean run is for the two commands an operator sends
        to put a machine right. A world-sync op is not refused on a faulted
        device, so it reaches the success path with the engine behind it and
        nobody having looked at the machine.
        """
        await _run_and_fail(
            controller, CommandExecutionError("stall", "CommandExecutionError"),
        )
        dispatched = _stub_preflight(controller)

        task = asyncio.create_task(
            controller.execute_command(
                device_id="flex_1", command="initialize", params={},
                timeout_seconds=5.0, effective_mode=WorkflowRunMode.LIVE,
                execution_id="exec-1", kind=CommandKind.WORLD_SYNC,
            )
        )
        await wait_until(lambda: "command_id" in dispatched)
        controller._command_futures[dispatched["command_id"]].set_result(None)
        assert await task is None

        standing = controller.fault("flex_1")
        assert standing is not None
        assert standing.command == "move_plate"

    async def test_a_world_sync_op_does_not_take_over_the_running_command_timer(
        self, controller: DeviceController,
    ) -> None:
        """It registers no ``_pending`` entry, so arming a timer would arm it
        on whatever command holds the device. That command would then carry
        two live watchers and the release path can only cancel one.
        """
        dispatched = _stub_preflight(controller)
        move = asyncio.create_task(
            controller.execute_command(
                device_id="flex_1", command="move_plate", params={},
                timeout_seconds=5.0, effective_mode=WorkflowRunMode.LIVE,
                execution_id=None,
            )
        )
        await wait_until(lambda: "command_id" in dispatched)
        await wait_until(
            lambda: controller._pending["flex_1"].command_timer is not None
        )
        armed = controller._pending["flex_1"].command_timer
        assert armed is not None
        move_command_id = dispatched["command_id"]

        dispatched.clear()
        sync = asyncio.create_task(
            controller.execute_command(
                device_id="flex_1", command="ensure_seeded", params={},
                timeout_seconds=5.0, effective_mode=WorkflowRunMode.LIVE,
                execution_id=None, kind=CommandKind.WORLD_SYNC,
            )
        )
        await wait_until(lambda: "command_id" in dispatched)
        controller._command_futures[dispatched["command_id"]].set_result(None)
        assert await sync is None

        assert controller._pending["flex_1"].command_timer is armed
        assert not armed.done()

        controller._command_futures[move_command_id].set_result(None)
        assert await move is None


class TestTheListSaysWhichDevicesAreFaulted:
    """One stop cancels every in-flight dispatch, so it can fault several
    devices at once. Without a flag on the list an operator has to read each
    device by name to find out which, and the fault is per-device only.
    """

    def _entries(self, faulted_names: set[str]) -> list[DeviceUnionEntry]:
        from orca.runtime.facades.devices import _merge_union_view

        topology = [
            TopologyDeviceEntry(
                name=name, kind="shaker",
                interfaces=("IShaker",), position_ids=(),
                interfaces_are_class_defaults=False,
            )
            for name in ("shaker_1", "shaker_2")
        ]
        return _merge_union_view(topology, [], lambda n: n in faulted_names)

    def test_a_faulted_device_is_flagged(self) -> None:
        entries = {e.name: e for e in self._entries({"shaker_2"})}

        assert entries["shaker_2"].faulted is True
        assert entries["shaker_1"].faulted is False

    def test_nothing_faulted_reads_false_everywhere(self) -> None:
        assert all(e.faulted is False for e in self._entries(set()))

    def _with_agent(self, faulted_names: set[str]) -> dict[str, DeviceUnionEntry]:
        from orca.runtime.facades.devices import _merge_union_view

        topology = [
            TopologyDeviceEntry(
                name="shaker_1", kind="shaker",
                interfaces=("IShaker",), position_ids=(),
                interfaces_are_class_defaults=False,
            )
        ]
        gateway = [
            GatewayDeviceEntry(
                name="shaker_1", driver_class_observed="SimShaker",
                interfaces=("IShaker",), last_heartbeat=None,
                connection_id="c1", status="ready",
            )
        ]
        entries = _merge_union_view(topology, gateway, lambda n: n in faulted_names)
        return {e.name: e for e in entries}

    def test_a_faulted_device_does_not_read_ready(self) -> None:
        """The device bridge says "ready" for a faulted device, because the
        driver behind it is idle and answering. Every other device list folds
        the fault into the status, and a reader comparing two lists that disagree
        believes the one that says the machine is fine."""
        entries = self._with_agent({"shaker_1"})

        assert entries["shaker_1"].status == FAULTED_STATUS
        assert entries["shaker_1"].faulted is True

    def test_a_healthy_device_still_reports_what_its_agent_said(self) -> None:
        entries = self._with_agent(set())

        assert entries["shaker_1"].status == "ready"


@pytest.mark.asyncio
class TestARefusalThatMovedNothingLatchesNothing:
    """A driver that checked its own state and declined actuated nothing, so
    there is nothing for an operator to go and look at.

    On the bench one of those latched a fault, and the next thing refused was
    an unrelated thread's healthy dispense on the same handler. Four threads
    paused and eight tips sat holding sample.

    The driver says which it was. Reading the class name instead covered only
    the one refusal that had a name here, and every other refusal the device
    bridge makes before a driver runs still faulted a machine that never moved.
    """

    async def test_a_busy_refusal_off_the_wire_latches_nothing(
        self, controller: DeviceController,
    ) -> None:
        # The wire keeps the class name, not the class, so this is the shape
        # the bench actually produces.
        await _run_and_fail(
            controller,
            CommandExecutionError(
                "park_gantry: FlexHead8 still holds tips, so a transfer is "
                "under way. Ask again once the tips are off.",
                "GantryBusyError",
                InstrumentOutcome.REFUSED,
            ),
            command="park_gantry",
        )

        assert controller.fault("flex_1") is None

    @pytest.mark.parametrize("error_type, outcome", [
        ("ParameterError", InstrumentOutcome.REJECTED),
        ("DeviceNotFoundError", InstrumentOutcome.REJECTED),
        ("SecurityError", InstrumentOutcome.REJECTED),
        ("NotAsyncError", InstrumentOutcome.REJECTED),
        ("SomeOtherHandlerBusyError", InstrumentOutcome.REFUSED),
    ])
    async def test_a_failure_that_moved_nothing_latches_nothing(
        self, controller: DeviceController,
        error_type: str, outcome: InstrumentOutcome,
    ) -> None:
        """The device bridge turns commands away in several places before a
        driver runs.

        A mistyped pipetting parameter is the common one, and faulting the
        handler for it stops every other thread's work on that handler.

        Both outcomes, because both moved nothing. The device bridge answers
        REJECTED for what it could not act on at all and REFUSED for a driver
        that declined, and only the second is worth asking again. The fault
        question does not care which: neither touched the instrument.
        """
        await _run_and_fail(
            controller,
            CommandExecutionError("turned away", error_type, outcome),
            command="aspirate",
        )

        assert controller.fault("flex_1") is None

    async def test_a_driver_that_actually_failed_still_latches(
        self, controller: DeviceController,
    ) -> None:
        """Control: the exemption is the outcome, not the fact of an error."""
        await _run_and_fail(
            controller,
            CommandExecutionError("stalled mid-move", "GantryBusyError",
                                  InstrumentOutcome.FAILED),
            command="park_gantry",
        )

        latched = controller.fault("flex_1")
        assert latched is not None
        assert latched.outcome is DeviceFaultOutcome.FAILED

    async def test_a_failure_the_driver_did_not_classify_still_latches(
        self, controller: DeviceController,
    ) -> None:
        """Control: silence is read as failed, the assumption that costs least."""
        await _run_and_fail(
            controller,
            CommandExecutionError("something went wrong", "RuntimeError"),
            command="aspirate",
        )

        latched = controller.fault("flex_1")
        assert latched is not None
        assert latched.outcome is DeviceFaultOutcome.FAILED

    async def test_the_refusal_still_reaches_the_caller_to_wait_on(
        self, controller: DeviceController,
    ) -> None:
        # step_aside_for waits on this exception and asks again. Swallowing it
        # would send the arm in while the gantry is still over the deck.
        dispatched = _stub_preflight(controller)
        task = asyncio.create_task(
            controller.execute_command(
                device_id="flex_1", command="park_gantry", params={},
                timeout_seconds=5.0, effective_mode=WorkflowRunMode.LIVE,
                execution_id="exec-1",
            )
        )
        await wait_until(lambda: "command_id" in dispatched)
        controller._command_futures[dispatched["command_id"]].set_exception(
            CommandExecutionError("still holds tips", "GantryBusyError",
                                  InstrumentOutcome.REFUSED)
        )

        with pytest.raises(CommandExecutionError) as raised:
            await task
        assert raised.value.error_type == "GantryBusyError"

    async def test_the_next_engine_command_is_not_refused(
        self, controller: DeviceController,
    ) -> None:
        await _run_and_fail(
            controller,
            CommandExecutionError("still holds tips", "GantryBusyError",
                                  InstrumentOutcome.REFUSED),
            command="park_gantry",
        )
        dispatched = _stub_preflight(controller)

        task = asyncio.create_task(
            controller.execute_command(
                device_id="flex_1", command="dispense", params={},
                timeout_seconds=5.0, effective_mode=WorkflowRunMode.LIVE,
                execution_id="exec-2",
            )
        )
        await wait_until(lambda: "command_id" in dispatched)
        controller._command_futures[dispatched["command_id"]].set_result(None)
        assert await task is None

    async def test_a_driver_failure_named_anything_else_still_latches(
        self, controller: DeviceController,
    ) -> None:
        await _run_and_fail(
            controller,
            CommandExecutionError("Stall or Collision Detected", "OpentronsCommandError"),
            command="park_gantry",
        )

        assert controller.fault("flex_1") is not None


@pytest.mark.asyncio
class TestTheDeviceListSaysTheDeviceIsFaulted:
    """The device list is the surface an operator and an assistant read first.

    On the bench it reported `status: ready` on a device with four threads
    paused citing its fault, because the fault was only on the per-device
    status read. The list was believed twice and the diagnosis went the wrong
    way both times.
    """

    async def _faulted(self, controller: DeviceController) -> None:
        await _run_and_fail(
            controller,
            CommandExecutionError("Stall or Collision Detected", "OpentronsCommandError"),
        )

    async def test_a_healthy_device_reads_the_agent_word_and_no_fault(
        self, controller: DeviceController,
    ) -> None:
        with patch.object(adhoc, "device_controller", controller):
            read = adhoc.with_fault(_snapshot())

        assert read.status == "ready"
        assert read.fault is None

    async def test_a_faulted_device_does_not_read_ready(
        self, controller: DeviceController,
    ) -> None:
        await self._faulted(controller)

        with patch.object(adhoc, "device_controller", controller):
            read = adhoc.with_fault(_snapshot())

        assert read.status == FAULTED_STATUS
        assert read.fault is not None
        assert read.fault.command == "move_plate"
        assert read.fault.may_still_be_moving is False
        assert "Stall or Collision Detected" in read.fault.error

    async def test_the_registry_keeps_the_agent_report_untouched(
        self, controller: DeviceController,
    ) -> None:
        """The device bridge's word is what the registry is for. Overwriting it
        there would lose whether the driver itself is busy."""
        await self._faulted(controller)
        stored = _snapshot()

        with patch.object(adhoc, "device_controller", controller):
            adhoc.with_fault(stored)

        assert stored.status == "ready"
        assert stored.fault is None

    async def test_the_list_filters_on_what_the_reader_is_shown(
        self, controller: DeviceController,
    ) -> None:
        """A status filter that ran before the fold would answer "ready" for a
        faulted device and drop it from the faulted list."""
        await self._faulted(controller)
        listed = AsyncMock(return_value=[_snapshot(), _snapshot("pf400_1")])

        with patch.object(adhoc, "device_controller", controller), \
                patch.object(adhoc.device_connection_tracker, "list_devices", listed):
            ready = await adhoc.list_devices(status="ready")
            faulted = await adhoc.list_devices(status=FAULTED_STATUS)
            everything = await adhoc.list_devices()

        assert [d.name for d in ready] == ["pf400_1"]
        assert [d.name for d in faulted] == ["flex_1"]
        assert [d.name for d in everything] == ["flex_1", "pf400_1"]


class TestEveryDeviceReadCarriesTheFault:
    """A faulted device answers every connection flag healthy, because the
    driver behind it is idle and answering. A read without the fault therefore
    says the machine is fine, and on the bench that read was believed twice.

    So the fault belongs on every device shape an operator can reach, and the
    CLI keeps its own mirror of each, which is where one of them was missed.
    """

    def test_both_daemon_device_shapes_carry_it(self) -> None:
        assert "fault" in DeviceDTO.model_fields
        assert "fault" in DeviceRegistryEntryDTO.model_fields

    def test_the_cli_mirrors_match_the_daemon_shapes(self) -> None:
        """The CLI renders its own copy, so a field only the server has is a
        field the operator never sees."""
        assert "fault" in CliDeviceRegistryEntryDTO.model_fields
        assert (
            DeviceRegistryEntryDTO.model_fields["fault"].annotation
            == CliDeviceRegistryEntryDTO.model_fields["fault"].annotation
        )


@pytest.mark.asyncio
class TestTheFailureCarriesTheFaultItLeft:
    """A caller holding only the exception can name the fault its pause is
    about. Without it a failed move names nothing: a move is not a device call,
    so there is no dispatched-device name to read off the thread, and the
    operator's first RETRY was refused by the fault they had already dealt with.
    """

    async def test_the_error_that_faulted_the_device_carries_that_fault(
        self, controller: DeviceController,
    ) -> None:
        error = CommandExecutionError("Stall or Collision Detected", "OpentronsCommandError")

        await _run_and_fail(controller, error, command="pick")

        assert error.device_fault is controller.fault("flex_1")

    async def test_a_refusal_carries_no_fault_because_it_left_none(
        self, controller: DeviceController,
    ) -> None:
        error = CommandExecutionError("still holds tips", "GantryBusyError",
                                      InstrumentOutcome.REFUSED)

        await _run_and_fail(controller, error, command="park_gantry")

        assert error.device_fault is None

    async def test_a_second_failure_carries_the_fault_that_was_kept(
        self, controller: DeviceController,
    ) -> None:
        """The FIRST fault is the one kept, so a later failure must not name
        itself: recovering on it would clear the record of what stopped the
        machine."""
        await _run_and_fail(
            controller, CommandExecutionError("stall", "OpentronsCommandError"),
            command="move_plate",
        )
        later = CommandExecutionError("later trouble", "OpentronsCommandError")

        await _run_and_fail(controller, later, command="pick", execution_id=None)

        assert later.device_fault is None
        standing = controller.fault("flex_1")
        assert standing is not None
        assert standing.command == "move_plate"


@pytest.mark.asyncio
class TestTheRuntimeDeviceRowCarriesTheFault:
    """`orca device list` and a hosted deployment's `GET /api/runtime/devices` both render
    `RegistryFacade`'s row, not the per-device read. A fault only on the latter
    leaves the list saying nothing is wrong, which is the surface this whole
    change exists to fix.
    """

    def _row(self, controller: DeviceController):
        from orca.runtime.facades.registry import RegistryFacade

        device = Mock()
        device.name = "flex_1"
        device.mode_under.return_value = WorkflowRunMode.LIVE
        device.in_use = False
        device.locations = []
        device.all_loaded_labware_ids = ()
        device.under_external_control = False
        device.external_control_hold = None

        facade = RegistryFacade.__new__(RegistryFacade)
        setattr(facade, "_links", Mock(is_initialized=Mock(return_value=True)))
        with patch.object(adhoc, "device_controller", controller):
            return facade._device_snapshot(device)

    async def test_a_faulted_device_row_names_the_command(
        self, controller: DeviceController,
    ) -> None:
        await _run_and_fail(
            controller,
            CommandExecutionError("Stall or Collision Detected", "OpentronsCommandError"),
        )

        row = self._row(controller)

        assert row.fault is not None
        assert row.fault.command == "move_plate"

    async def test_a_healthy_device_row_carries_none(
        self, controller: DeviceController,
    ) -> None:
        assert self._row(controller).fault is None


@pytest.mark.asyncio
class TestACleanBringUpOrHomeClearsTheFault:
    """`initialize` and `home` are what an operator runs to put a machine right.

    Every other command coming back clean proves only that the device answers.
    These two put it into a known state, so finishing one cleanly is the
    operator saying they dealt with it. The engine cannot reach a faulted
    device at all, so a command that gets this far is an operator's own.
    """

    async def _fault_it(self, controller: DeviceController) -> None:
        await _run_and_fail(
            controller, CommandExecutionError("stall", "CommandExecutionError"),
        )

    @pytest.mark.parametrize("command", ["initialize", "home"])
    async def test_a_clean_run_gives_the_device_back(
        self, controller: DeviceController, command: str,
    ) -> None:
        await self._fault_it(controller)

        await _run_and_succeed(controller, command=command)

        assert controller.fault("flex_1") is None

    async def test_any_other_command_leaves_the_fault_standing(
        self, controller: DeviceController,
    ) -> None:
        """A gripper that opens still says nothing about where the plate went."""
        await self._fault_it(controller)

        await _run_and_succeed(controller, command="release_jaw")

        fault = controller.fault("flex_1")
        assert fault is not None
        assert fault.command == "move_plate"

    async def test_an_initialize_that_fails_leaves_the_first_fault(
        self, controller: DeviceController,
    ) -> None:
        await self._fault_it(controller)

        await _run_and_fail(
            controller,
            CommandExecutionError("no answer", "CommandExecutionError"),
            command="initialize",
            execution_id=None,
        )

        fault = controller.fault("flex_1")
        assert fault is not None
        assert fault.command == "move_plate"

    async def test_a_device_with_no_fault_is_untouched(
        self, controller: DeviceController,
    ) -> None:
        await _run_and_succeed(controller, command="home")

        assert controller.fault("flex_1") is None

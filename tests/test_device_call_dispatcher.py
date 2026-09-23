"""DeviceCallDispatcher + OperationLogBuilder contract tests.

These pin the three invariants the per-request dispatch body has to
hold (originally enforced in ``ActionBodyLocationAction.execute`` and
verified end-to-end via SMC; now pinned at the unit level so a
regression localizes to the dispatcher rather than the integration
suite):

1. ``__error__`` short-circuit: a request carrying an error from the
   user task surfaces that exception with ``request.completion`` set,
   never resolves a device method.
2. Failure path always sets ``request.completion`` before propagating,
   on every raise site (method-not-found AttributeError, external-
   control gate, device-method raise, interpreter-raise) -- so the
   user task parked on ``request.completion.wait()`` always wakes.
3. PartialChannelFailureError: per-channel records extend the
   operation log before the exception raises.
"""

import asyncio
from typing import Any, Awaitable, Callable, List, Sequence
from unittest.mock import MagicMock

import pytest

from cheshire_drivers.liquid_handler_models import (
    LabwareStateResponse,
    ReconcileHardwareStateResponse,
)

from orca.gateway.controller.exceptions import (
    CommandExecutionError,
    DeviceOfflineError,
)
from orca.resource_models.device_error import (
    DeviceStateDivergenceError,
    DeviceUnderExternalControlError,
    TipsNotPresentError,
)
from orca.resource_models.labware import LabwareInstance, TipRackInstance
from orca.state.provenance import Provenance
from orca.workflow_models.actions.operation_recovery import device_op_recovery_handler
from orca.workflow_models.status_enums import RecoveryDecision
from orca.state.ops_history import OpsHistory
from orca.state.records import ObservationGapCause, OperationRecord
from tests.test_helpers import bind_ledger
from tests.unit.test_labware_can_continue import _append_pickup, _seed_rack
from orca.runtime.recoverable_timeout import (
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    recoverable_timeout_coordinator,
)
from orca.runtime.run_modes import current_execution_id
from orca.runtime.sim_labware import SimPlate, SimTipRack
from orca.plugins.liquid_handler_interpreter import LiquidHandlerInterpreter
from orca.resource_models.tracked_lock import TrackedLock
from orca.workflow_models.actions.device_call_dispatcher import (
    DeviceCallDispatcher,
    OperationLogBuilder,
    PartialChannelFailureError,
)
from orca.workflow_models.device_handle import ActionRequest


def _make_request(command: str = "shake", error: Exception | None = None) -> ActionRequest:
    return ActionRequest(
        device_name="dev", command=command, args=(), kwargs={}, error=error,
    )


class _FakeDevice:
    """Minimal device shape for dispatcher tests: name, lock, under_external_control,
    and one callable method attached at construction. ``getattr(self, missing, None)``
    returns None for any other attribute (mirrors a real device whose methods are
    declared on the class). ``shake_calls`` records each invocation so tests can
    assert the device method was (not) reached."""
    __slots__ = ("name", "lock", "under_external_control", "shake", "shake_calls")

    def __init__(self, method_result: Any = None,
                  method_raises: Exception | None = None,
                  under_external_control: bool = False) -> None:
        self.name = "dev"
        self.lock = TrackedLock("dev device lock")
        self.under_external_control = under_external_control
        self.shake_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        if method_raises is not None:
            async def raise_it(*args: Any, **kwargs: Any) -> Any:
                self.shake_calls.append((args, kwargs))
                raise method_raises
            self.shake = raise_it
        else:
            async def return_result(*args: Any, **kwargs: Any) -> Any:
                self.shake_calls.append((args, kwargs))
                return method_result
            self.shake = return_result

    def command_max_seconds(self, command: str) -> float | None:
        # No recoverable-timeout coordinator is seeded in these unit tests, so
        # this is never consulted; present to satisfy IDispatchableDevice.
        return None


def _make_device(*args: Any, **kwargs: Any) -> _FakeDevice:
    return _FakeDevice(*args, **kwargs)


class TestDispatcherErrorShortCircuit:
    """Pin 1: a request whose ``error`` is set on arrival raises that
    error without resolving any method, with ``completion`` set."""

    @pytest.mark.asyncio
    async def test_error_request_raises_and_sets_completion(self) -> None:
        device = _make_device()
        dispatcher = DeviceCallDispatcher(
            device=device, interpreter=None, operation_log=[], action_id="a1",
            thread_id="t1", execution_id="e1",
        )
        boom = RuntimeError("from user task")
        request = _make_request(command="__error__", error=boom)

        with pytest.raises(RuntimeError, match="from user task"):
            await dispatcher.dispatch(request, affected_labware=[], affected_labware_ids=[])

        assert request.completion.is_set()
        assert request.error is boom
        assert request.result is None


class TestDispatcherCompletionInvariant:
    """Pin 2: ``request.completion`` set before exception propagates,
    on every failure surface."""

    @pytest.mark.asyncio
    async def test_method_not_found_sets_completion(self) -> None:
        device = _make_device()  # only has shake
        dispatcher = DeviceCallDispatcher(
            device=device, interpreter=None, operation_log=[], action_id="a1",
            thread_id="t1", execution_id="e1",
        )
        request = _make_request(command="dispense")  # not on device

        with pytest.raises(AttributeError, match="has no method 'dispense'"):
            await dispatcher.dispatch(request, affected_labware=[], affected_labware_ids=[])

        assert request.completion.is_set()
        assert isinstance(request.error, AttributeError)

    @pytest.mark.asyncio
    async def test_external_control_pre_lock_sets_completion(self) -> None:
        device = _make_device(under_external_control=True)
        dispatcher = DeviceCallDispatcher(
            device=device, interpreter=None, operation_log=[], action_id="a1",
            thread_id="t1", execution_id="e1",
        )
        request = _make_request()

        with pytest.raises(DeviceUnderExternalControlError):
            await dispatcher.dispatch(request, affected_labware=[], affected_labware_ids=[])

        assert request.completion.is_set()

    @pytest.mark.asyncio
    async def test_external_control_taken_while_waiting_for_lock(self) -> None:
        """The under-lock re-check: the gateway flag-write is NOT synchronized
        with the device lock, so a take that lands while dispatch is queued for
        the lock must still be honored. Hold the lock, start dispatch (passes
        the pre-lock check), flip the flag, release the lock -- the under-lock
        re-check must raise, set completion, and never call the device method."""
        device = _make_device()  # flag starts False; pre-lock check passes
        dispatcher = DeviceCallDispatcher(
            device=device, interpreter=None, operation_log=[], action_id="a1",
            thread_id="t1", execution_id="e1",
        )
        request = _make_request()

        released = asyncio.Event()

        async def hold_the_device() -> None:
            async with device.lock.held_for("an earlier command"):
                await released.wait()

        holding = asyncio.create_task(hold_the_device())
        await asyncio.sleep(0)
        dispatch_task = asyncio.create_task(
            dispatcher.dispatch(request, affected_labware=[], affected_labware_ids=[])
        )
        # Let the task reach the device lock and block there.
        await asyncio.sleep(0)
        device.under_external_control = True
        released.set()
        await holding

        with pytest.raises(DeviceUnderExternalControlError):
            await dispatch_task

        assert request.completion.is_set()
        assert device.shake_calls == []

    @pytest.mark.asyncio
    async def test_device_method_raise_sets_completion(self) -> None:
        boom = ValueError("driver said no")
        device = _make_device(method_raises=boom)
        dispatcher = DeviceCallDispatcher(
            device=device, interpreter=None, operation_log=[], action_id="a1",
            thread_id="t1", execution_id="e1",
        )
        request = _make_request()

        with pytest.raises(ValueError, match="driver said no"):
            await dispatcher.dispatch(request, affected_labware=[], affected_labware_ids=[])

        assert request.completion.is_set()
        assert request.error is boom


class TestDispatcherSuccessPath:
    """Success path: result populated, completion set, no error."""

    @pytest.mark.asyncio
    async def test_success_sets_result_and_completion(self) -> None:
        device = _make_device(method_result={"ok": True})
        dispatcher = DeviceCallDispatcher(
            device=device, interpreter=None, operation_log=[], action_id="a1",
            thread_id="t1", execution_id="e1",
        )
        request = _make_request()

        await dispatcher.dispatch(request, affected_labware=[], affected_labware_ids=[])

        assert request.completion.is_set()
        assert request.result == {"ok": True}
        assert request.error is None


class _RecordingCoordinator:
    """Records the max_seconds the dispatcher resolves, then runs the call."""

    def __init__(self) -> None:
        self.max_seconds_calls: List[float] = []

    async def run_with_timeout(
        self, execution_id: str, device_id: str, command: str,
        max_seconds: float, coro_factory: Callable[[], Awaitable[Any]],
    ) -> Any:
        self.max_seconds_calls.append(max_seconds)
        return await coro_factory()

    def extend(self, incident_id: str, additional_seconds: float) -> None: ...

    def abort(self, incident_id: str, operator: str, reason: str) -> None: ...

    def mark_complete(self, incident_id: str, operator: str, reason: str) -> None: ...


class _TimedDevice:
    """Device shape whose advertised per-command bound is configurable."""
    __slots__ = ("name", "lock", "under_external_control", "shake", "_max")

    def __init__(self, max_seconds: float | None) -> None:
        self.name = "dev"
        self.lock = TrackedLock("dev device lock")
        self.under_external_control = False
        self._max = max_seconds

        async def shake(*args: Any, **kwargs: Any) -> Any:
            return {"ok": True}

        self.shake = shake

    def command_max_seconds(self, command: str) -> float | None:
        return self._max


class TestDispatcherRecoverableTimeoutFallback:
    """C1: with a coordinator seeded, an unannotated command (no advertised
    max_seconds) is still bounded -- by ``DEFAULT_COMMAND_TIMEOUT_SECONDS`` --
    so it can never hang the execution unboundedly."""

    @pytest.mark.asyncio
    async def test_unannotated_command_bounded_by_default(self) -> None:
        device = _TimedDevice(max_seconds=None)
        coord = _RecordingCoordinator()
        token = recoverable_timeout_coordinator.set(coord)
        exec_token = current_execution_id.set("e1")
        try:
            dispatcher = DeviceCallDispatcher(
                device=device, interpreter=None, operation_log=[],
                action_id="a1", thread_id="t1", execution_id="e1",
            )
            await dispatcher.dispatch(
                _make_request(), affected_labware=[], affected_labware_ids=[],
            )
        finally:
            current_execution_id.reset(exec_token)
            recoverable_timeout_coordinator.reset(token)
        assert coord.max_seconds_calls == [DEFAULT_COMMAND_TIMEOUT_SECONDS]

    @pytest.mark.asyncio
    async def test_annotated_command_uses_advertised_max(self) -> None:
        device = _TimedDevice(max_seconds=12.5)
        coord = _RecordingCoordinator()
        token = recoverable_timeout_coordinator.set(coord)
        exec_token = current_execution_id.set("e1")
        try:
            dispatcher = DeviceCallDispatcher(
                device=device, interpreter=None, operation_log=[],
                action_id="a1", thread_id="t1", execution_id="e1",
            )
            await dispatcher.dispatch(
                _make_request(), affected_labware=[], affected_labware_ids=[],
            )
        finally:
            current_execution_id.reset(exec_token)
            recoverable_timeout_coordinator.reset(token)
        assert coord.max_seconds_calls == [12.5]


class TestOperationLogBuilderInterpreterContract:
    """OperationLogBuilder dispatches the three-step interpreter contract."""

    @pytest.mark.asyncio
    async def test_per_channel_records_extend_log_and_raise(self) -> None:
        operation_log: List[OperationRecord] = []
        interpreter = MagicMock()
        per_channel = [MagicMock(spec=OperationRecord), MagicMock(spec=OperationRecord)]
        interpreter.interpret_per_channel_outcomes.return_value = per_channel

        builder = OperationLogBuilder(
            interpreter=interpreter, operation_log=operation_log,
            device_name="dev", action_id="a1", thread_id="t1",
        )
        request = _make_request()

        with pytest.raises(PartialChannelFailureError) as exc:
            builder.process(request, result=None, affected_labware=["p"], affected_labware_ids=["p-id"])

        assert exc.value.records == per_channel
        assert operation_log == per_channel
        # On the per-channel path, the single-record + driver-state branches
        # MUST NOT execute (their records would muddle the per-channel ones).
        interpreter.interpret.assert_not_called()
        interpreter.interpret_driver_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_interpreted_records_appended_then_driver_state_extended(self) -> None:
        operation_log: List[OperationRecord] = []
        interpreter = MagicMock()
        interpreter.interpret_per_channel_outcomes.return_value = []
        interpreted = [MagicMock(spec=OperationRecord)]
        interpreter.interpret.return_value = interpreted
        driver_state = [MagicMock(spec=OperationRecord)]
        interpreter.interpret_driver_state.return_value = driver_state

        builder = OperationLogBuilder(
            interpreter=interpreter, operation_log=operation_log,
            device_name="dev", action_id="a1", thread_id="t1",
        )
        request = _make_request()

        builder.process(request, result=None, affected_labware=["p"], affected_labware_ids=["p-id"])

        assert operation_log == [interpreted[0], driver_state[0]]

    @pytest.mark.asyncio
    async def test_every_interpreted_record_reaches_the_log_in_order(self) -> None:
        """One call can produce several records -- a head reaching across two
        racks is two facts about two labware -- and the log must carry all of
        them, in the order the channels acted."""
        operation_log: List[OperationRecord] = []
        interpreter = MagicMock()
        interpreter.interpret_per_channel_outcomes.return_value = []
        first, second = MagicMock(spec=OperationRecord), MagicMock(spec=OperationRecord)
        interpreter.interpret.return_value = [first, second]
        interpreter.interpret_driver_state.return_value = []

        builder = OperationLogBuilder(
            interpreter=interpreter, operation_log=operation_log,
            device_name="dev", action_id="a1", thread_id="t1",
        )
        request = _make_request()

        builder.process(request, result=None, affected_labware=["p"], affected_labware_ids=["p-id"])

        assert operation_log == [first, second]

    @pytest.mark.asyncio
    async def test_an_uninterpretable_command_appends_nothing(self) -> None:
        operation_log: List[OperationRecord] = []
        interpreter = MagicMock()
        interpreter.interpret_per_channel_outcomes.return_value = []
        interpreter.interpret.return_value = []
        interpreter.interpret_driver_state.return_value = []

        builder = OperationLogBuilder(
            interpreter=interpreter, operation_log=operation_log,
            device_name="dev", action_id="a1", thread_id="t1",
        )
        request = _make_request()

        builder.process(request, result=None, affected_labware=["p"], affected_labware_ids=["p-id"])

        assert operation_log == []


class _CountingCoordinator:
    """Implements ``IRecoverableTimeoutCoordinator`` and records whether
    ``run_with_timeout`` was reached. The non-positive-max_seconds path must
    bypass it entirely (unbounded call), so ``run_with_timeout_calls`` stays 0."""

    def __init__(self) -> None:
        self.run_with_timeout_calls = 0

    async def run_with_timeout(
        self,
        execution_id: str,
        device_id: str,
        command: str,
        max_seconds: float,
        coro_factory: Callable[[], Awaitable[Any]],
    ) -> Any:
        self.run_with_timeout_calls += 1
        return await coro_factory()

    def extend(self, incident_id: str, additional_seconds: float) -> None: ...

    def abort(self, incident_id: str, operator: str, reason: str) -> None: ...

    def mark_complete(self, incident_id: str, operator: str, reason: str) -> None: ...


class TestDispatcherNonPositiveMaxSecondsRunsUnbounded:
    """A non-positive ``command_max_seconds`` (driver metadata misconfigured)
    means "no timeout": the device method runs unbounded and the seeded
    coordinator's ``run_with_timeout`` is never reached."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("max_seconds", [0.0, -1.0])
    async def test_non_positive_max_seconds_bypasses_coordinator(
        self, monkeypatch: pytest.MonkeyPatch, max_seconds: float,
    ) -> None:
        monkeypatch.setattr(
            _FakeDevice, "command_max_seconds", lambda self, command: max_seconds,
        )
        device = _make_device(method_result={"ok": True})
        dispatcher = DeviceCallDispatcher(
            device=device, interpreter=None, operation_log=[], action_id="a1",
            thread_id="t1", execution_id="e1",
        )
        request = _make_request()
        coordinator = _CountingCoordinator()
        token = recoverable_timeout_coordinator.set(coordinator)
        exec_token = current_execution_id.set("e1")
        try:
            await dispatcher.dispatch(request, affected_labware=[], affected_labware_ids=[])
        finally:
            current_execution_id.reset(exec_token)
            recoverable_timeout_coordinator.reset(token)

        assert device.shake_calls == [((), {})]
        assert request.result == {"ok": True}
        assert request.error is None
        assert coordinator.run_with_timeout_calls == 0


class TestDispatcherInterpreterRaiseStillSetsCompletion:
    """Combined invariant: interpreter raising PartialChannelFailureError
    still sets request.completion (the per-channel records also extend
    the operation log before the raise)."""

    @pytest.mark.asyncio
    async def test_partial_channel_failure_sets_completion(self) -> None:
        per_channel = [MagicMock(spec=OperationRecord)]
        interpreter = MagicMock()
        interpreter.interpret_per_channel_outcomes.return_value = per_channel

        device = _make_device(method_result={"ok": True})
        operation_log: List[OperationRecord] = []
        dispatcher = DeviceCallDispatcher(
            device=device, interpreter=interpreter,
            operation_log=operation_log, action_id="a1", thread_id="t1",
            execution_id="e1",
        )
        request = _make_request()

        with pytest.raises(PartialChannelFailureError):
            await dispatcher.dispatch(request, affected_labware=["p"], affected_labware_ids=["p-id"])

        assert request.completion.is_set()
        assert operation_log == per_channel


class TestDispatcherRecoverableTimeoutGate:
    """The recoverable-timeout wrap engages only with a seeded coordinator AND a
    positive ``max_seconds``. A non-positive ``max_seconds`` (misconfigured driver
    metadata) runs unbounded instead of timing out immediately and wedging."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_max", [0.0, -1.0])
    async def test_non_positive_max_seconds_runs_unbounded(
        self, bad_max: float, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from orca.runtime.recoverable_timeout import recoverable_timeout_coordinator

        wrapped: list[str] = []

        class _RecordingCoordinator:
            async def run_with_timeout(
                self, execution_id: str, device_id: str, command: str,
                max_seconds: float, coro_factory: Callable[[], Awaitable[Any]],
            ) -> Any:
                wrapped.append(command)
                return await coro_factory()

            def extend(self, incident_id: str, additional_seconds: float) -> None: ...

            def abort(self, incident_id: str, operator: str, reason: str) -> None: ...

            def mark_complete(
                self, incident_id: str, operator: str, reason: str,
            ) -> None: ...

        monkeypatch.setattr(
            _FakeDevice, "command_max_seconds", lambda self, command: bad_max,
        )
        token = recoverable_timeout_coordinator.set(_RecordingCoordinator())
        exec_token = current_execution_id.set("e1")
        try:
            device = _make_device(method_result="ok")
            dispatcher = DeviceCallDispatcher(
                device=device, interpreter=None, operation_log=[], action_id="a1",
                thread_id="t1", execution_id="e1",
            )
            request = _make_request(command="shake")
            await dispatcher.dispatch(
                request, affected_labware=[], affected_labware_ids=[],
            )
            assert request.result == "ok"
            assert device.shake_calls  # device method reached
            assert wrapped == []  # coordinator NOT consulted on the unbounded path
        finally:
            current_execution_id.reset(exec_token)
            recoverable_timeout_coordinator.reset(token)


class TestOperationLog96HeadTypedLabware:
    """96-head verbs reach the interpreter with TYPED labware objects (the
    device API takes ctx.plate(...)/ctx.tip_rack(...)); the recorded details
    must carry the labware's instance name, not crash on a non-string arg."""

    def _builder(self, log: list[OperationRecord]) -> OperationLogBuilder:
        from orca.plugins.liquid_handler_interpreter import LiquidHandlerInterpreter
        return OperationLogBuilder(
            interpreter=LiquidHandlerInterpreter(), operation_log=log,
            device_name="lh", action_id="a1", thread_id="t1",
        )

    def test_aspirate96_records_the_instance_name(self) -> None:
        from orca.state.records import Aspirate96Details
        from orca.runtime.sim_labware import SimPlate
        log: list[OperationRecord] = []
        request = ActionRequest(
            device_name="lh", command="aspirate96",
            args=(SimPlate("plate_1-a1b2c3d4"), 25.0), kwargs={}, error=None,
        )
        self._builder(log).process(
            request, result=None,
            affected_labware=["plate_1-a1b2c3d4"], affected_labware_ids=["id1"],
        )
        [details] = [r.details for r in log if isinstance(r.details, Aspirate96Details)]
        assert details.labware == "plate_1-a1b2c3d4"
        assert details.volume == 25.0

    def test_tip96_verbs_record_the_instance_name(self) -> None:
        from orca.state.records import TipDrop96Details, TipPickUp96Details
        from orca.runtime.sim_labware import SimTipRack
        rack = SimTipRack("tips-a1b2c3d4", True)
        log: list[OperationRecord] = []
        builder = self._builder(log)
        builder.process(
            ActionRequest(device_name="lh", command="pick_up_tips96",
                          args=(rack,), kwargs={}, error=None),
            result=None, affected_labware=[rack.name], affected_labware_ids=["id1"],
        )
        builder.process(
            ActionRequest(device_name="lh", command="drop_tips96",
                          args=(), kwargs={"tip_rack": rack}, error=None),
            result=None, affected_labware=[rack.name], affected_labware_ids=["id1"],
        )
        [pick] = [r.details for r in log if isinstance(r.details, TipPickUp96Details)]
        [drop] = [r.details for r in log if isinstance(r.details, TipDrop96Details)]
        assert pick.tip_rack == rack.name
        assert drop.tip_rack == rack.name


class _FakeTipSpot:
    def __init__(self, parent_name: str, identifier: str) -> None:
        self.parent_name = parent_name
        self.identifier = identifier


class TestOperationLogArgKwargCoalescing:
    """The dispatcher forwards the action body's raw call, so every
    bridge-legal arg/kwarg split must produce the same truthful record."""

    def _process(
        self,
        command: str,
        args: tuple[SimPlate | SimTipRack | float, ...],
        kwargs: dict[str, SimPlate | SimTipRack | float | list[_FakeTipSpot]],
    ) -> list[OperationRecord]:
        from orca.plugins.liquid_handler_interpreter import LiquidHandlerInterpreter
        log: list[OperationRecord] = []
        builder = OperationLogBuilder(
            interpreter=LiquidHandlerInterpreter(), operation_log=log,
            device_name="lh", action_id="a1", thread_id="t1",
        )
        builder.process(
            ActionRequest(device_name="lh", command=command,
                          args=args, kwargs=kwargs, error=None),
            result=None, affected_labware=[], affected_labware_ids=[],
        )
        return log

    def test_positional_rack_drop96_records_the_return_not_waste(self) -> None:
        from orca.state.records import TipDrop96Details
        rack = SimTipRack("tips-a1b2c3d4", True)
        [record] = self._process("drop_tips96", (rack,), {})
        assert isinstance(record.details, TipDrop96Details)
        assert record.details.tip_rack == rack.name
        assert record.details.to_waste is False

    def test_keyword_rack_drop96_records_the_return_not_waste(self) -> None:
        from orca.state.records import TipDrop96Details
        rack = SimTipRack("tips-a1b2c3d4", True)
        [record] = self._process("drop_tips96", (), {"tip_rack": rack})
        assert isinstance(record.details, TipDrop96Details)
        assert record.details.tip_rack == rack.name
        assert record.details.to_waste is False

    def test_bare_drop96_records_waste(self) -> None:
        from orca.state.records import TipDrop96Details
        [record] = self._process("drop_tips96", (), {})
        assert isinstance(record.details, TipDrop96Details)
        assert record.details.tip_rack is None
        assert record.details.to_waste is True

    def test_keyword_volume_aspirate96_records_it(self) -> None:
        from orca.state.records import Aspirate96Details
        plate = SimPlate("plate_1-a1b2c3d4")
        [record] = self._process("aspirate96", (plate,), {"volume": 50.0})
        assert isinstance(record.details, Aspirate96Details)
        assert record.details.volume == 50.0

    def test_positional_flow_rate_dispense96_records_it(self) -> None:
        from orca.state.records import Dispense96Details
        plate = SimPlate("plate_1-a1b2c3d4")
        [record] = self._process("dispense96", (plate, 30.0, 80.0), {})
        assert isinstance(record.details, Dispense96Details)
        assert record.details.volume == 30.0
        assert record.details.flow_rate == 80.0

    def test_keyword_tip_spots_pick_up_records_the_rack(self) -> None:
        from orca.state.records import TipPickUpDetails
        spots = [_FakeTipSpot("tips-a1b2c3d4", "A1")]
        [record] = self._process("pick_up_tips", (), {"tip_spots": spots})
        assert isinstance(record.details, TipPickUpDetails)
        assert record.details.tip_rack == "tips-a1b2c3d4"

    def test_drop_tips_records_the_rack_return(self) -> None:
        from orca.state.records import TipDropDetails
        spots = [_FakeTipSpot("tips-a1b2c3d4", "A1")]
        [record] = self._process("drop_tips", (), {"tip_spots": spots})
        assert isinstance(record.details, TipDropDetails)
        assert record.details.tip_rack == "tips-a1b2c3d4"
        assert record.details.to_waste is False

    def test_duplicate_binding_fails_loud(self) -> None:
        rack = SimTipRack("tips-a1b2c3d4", True)
        with pytest.raises(TypeError, match="multiple values"):
            self._process("drop_tips96", (rack,), {"tip_rack": rack})

    def test_discard_tips_lands_as_one_record_the_ledger_can_read(self) -> None:
        """The bench regression: a bare record here was iterated field by field,
        the tuples failed validation, and every operation the action performed
        went missing while the run reported success."""
        from orca.state.records import (
            TipDiscardDetails, TrackingRecord, TrackingSource,
        )
        log = self._process("discard_tips", (), {"use_channels": [0, 1]})

        assert len(log) == 1
        assert isinstance(log[0], OperationRecord)
        assert isinstance(log[0].details, TipDiscardDetails)
        assert log[0].details.use_channels == [0, 1]
        TrackingRecord(
            execution_id="e1", action_id="a1", thread_id="t1", method_id=None,
            source=TrackingSource.OBSERVED, timestamp=0.0, operations=log,
        )

    def test_an_interpreter_returning_a_bare_record_is_refused_by_name(self) -> None:
        """Refused where it happens. Left to the append, one bad entry takes the
        whole action's history down with it, two layers away."""
        from orca.state.records import DeviceOperation, TipDiscardDetails

        class _BareRecordInterpreter:
            def interpret_per_channel_outcomes(self, **kwargs: Any) -> Any:
                return []

            def interpret_state_records(self, **kwargs: Any) -> Any:
                return []

            def interpret(self, **kwargs: Any) -> Any:
                return OperationRecord(
                    operation=DeviceOperation.DISCARD_TIPS, device_name="lh",
                    affected_labware=[], affected_labware_ids=[],
                    action_id="a1", thread_id="t1", timestamp=0.0,
                    details=TipDiscardDetails(use_channels=None),
                )

        builder = OperationLogBuilder(
            interpreter=_BareRecordInterpreter(), operation_log=[],
            device_name="lh", action_id="a1", thread_id="t1",
        )
        with pytest.raises(TypeError, match="not a list of OperationRecord"):
            builder.process(
                ActionRequest(device_name="lh", command="shake",
                              args=(), kwargs={}, error=None),
                result=None, affected_labware=[], affected_labware_ids=[],
            )

    def test_verb_binding_matches_the_bridge_signatures(self) -> None:
        import inspect
        from orca.devices.devices import LiquidHandler
        from orca.plugins.liquid_handler_interpreter import _VERB_PARAMS
        for verb, names in _VERB_PARAMS.items():
            bridge = tuple(
                inspect.signature(getattr(LiquidHandler, verb)).parameters
            )[1:]
            assert bridge == names, (
                f"{verb}: interpreter binds {names}, bridge takes {bridge}"
            )


class _ReconcilableDevice:
    """Device shape with the hardware-reconcile capability: seal() fails once,
    then succeeds; reconcile calls and their order are recorded."""

    def __init__(self, reconcile_report: ReconcileHardwareStateResponse) -> None:
        self.name = "lh1"
        self.lock = TrackedLock("dev device lock")
        self.under_external_control = False
        self.calls: list[str] = []
        self._fail_remaining = 1
        self._report = reconcile_report

    async def seal(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append("seal")
        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            raise RuntimeError("first seal fails")

    async def reconcile_hardware_state(self) -> ReconcileHardwareStateResponse:
        self.calls.append("reconcile")
        return self._report

    def command_max_seconds(self, command: str) -> float | None:
        return None


def _retry_once_handler() -> Callable[[Exception, str], Awaitable[RecoveryDecision]]:
    async def handler(exc: Exception, command: str) -> RecoveryDecision:
        return RecoveryDecision.RETRY_OP

    return handler


class TestRetryOpReconcilesHardwareFirst:
    """A RETRY_OP may follow an operator's hands-on recovery at the instrument,
    so the retried call must not run against driver state that recovery
    invalidated: reconcile first, and refuse the retry when reconcile reports a
    divergence only the operator can resolve."""

    @pytest.mark.asyncio
    async def test_retry_op_reconciles_before_reissuing_the_call(self) -> None:
        device = _ReconcilableDevice(ReconcileHardwareStateResponse(checked=True))
        dispatcher = DeviceCallDispatcher(
            device=device, interpreter=None, operation_log=[], action_id="a1",
            thread_id="t1", execution_id="e1",
        )
        request = _make_request(command="seal")
        token = device_op_recovery_handler.set(_retry_once_handler())
        try:
            await dispatcher.dispatch(request, affected_labware=[], affected_labware_ids=[])
        finally:
            device_op_recovery_handler.reset(token)

        assert device.calls == ["seal", "reconcile", "seal"]
        assert request.error is None

    @pytest.mark.asyncio
    async def test_a_divergence_report_pauses_instead_of_retrying(self) -> None:
        device = _ReconcilableDevice(
            ReconcileHardwareStateResponse(
                checked=True, requires_intervention=True,
                message="mount left holds a tip the driver does not track",
            )
        )
        dispatcher = DeviceCallDispatcher(
            device=device, interpreter=None, operation_log=[], action_id="a1",
            thread_id="t1", execution_id="e1",
        )
        request = _make_request(command="seal")
        token = device_op_recovery_handler.set(_retry_once_handler())
        try:
            with pytest.raises(DeviceStateDivergenceError, match="does not track"):
                await dispatcher.dispatch(
                    request, affected_labware=[], affected_labware_ids=[]
                )
        finally:
            device_op_recovery_handler.reset(token)

        assert device.calls == ["seal", "reconcile"]
        assert request.completion.is_set()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "reconcile_error",
        [
            CommandExecutionError(
                "Device lh1 does not support command reconcile_hardware_state",
                "UnsupportedCommandError",
            ),
            DeviceOfflineError("Device lh1 not found or offline"),
            RuntimeError("wire dropped mid-reconcile"),
        ],
        ids=["driver_predates_the_command", "device_went_offline", "wire_failure"],
    )
    async def test_a_failed_reconcile_still_runs_the_retry_the_operator_asked_for(
        self, reconcile_error: Exception,
    ) -> None:
        """Only a divergence report may stop a retry. Everything else is the
        reconcile failing, and failing the retry on it would hand the operator
        the reconcile's error under the name of the command they were
        recovering -- the on-prem device bridge whose drivers predate this
        command answers UnsupportedCommandError, which arrives as
        CommandExecutionError, not as the capability refusal the gateway raises
        before the wire."""
        class _RefusingDevice(_ReconcilableDevice):
            async def reconcile_hardware_state(self) -> ReconcileHardwareStateResponse:
                self.calls.append("reconcile")
                raise reconcile_error

        device = _RefusingDevice(ReconcileHardwareStateResponse(checked=True))
        dispatcher = DeviceCallDispatcher(
            device=device, interpreter=None, operation_log=[], action_id="a1",
            thread_id="t1", execution_id="e1",
        )
        request = _make_request(command="seal")
        token = device_op_recovery_handler.set(_retry_once_handler())
        try:
            await dispatcher.dispatch(request, affected_labware=[], affected_labware_ids=[])
        finally:
            device_op_recovery_handler.reset(token)

        assert device.calls == ["seal", "reconcile", "seal"]
        assert request.error is None

    @pytest.mark.asyncio
    async def test_taking_external_control_during_the_reconcile_is_not_overridden(self) -> None:
        """The operator's own decisions still win: a device taken for hands-on
        work mid-reconcile must not be driven by the retry that follows."""
        class _TakenDevice(_ReconcilableDevice):
            async def reconcile_hardware_state(self) -> ReconcileHardwareStateResponse:
                self.calls.append("reconcile")
                raise DeviceUnderExternalControlError(device_name=self.name)

        device = _TakenDevice(ReconcileHardwareStateResponse(checked=True))
        dispatcher = DeviceCallDispatcher(
            device=device, interpreter=None, operation_log=[], action_id="a1",
            thread_id="t1", execution_id="e1",
        )
        request = _make_request(command="seal")
        token = device_op_recovery_handler.set(_retry_once_handler())
        try:
            with pytest.raises(DeviceUnderExternalControlError):
                await dispatcher.dispatch(
                    request, affected_labware=[], affected_labware_ids=[]
                )
        finally:
            device_op_recovery_handler.reset(token)

        assert device.calls == ["seal", "reconcile"]

    @pytest.mark.asyncio
    async def test_devices_without_the_capability_retry_unreconciled(self) -> None:
        boom = RuntimeError("first shake fails")
        device = _make_device(method_raises=boom)
        dispatcher = DeviceCallDispatcher(
            device=device, interpreter=None, operation_log=[], action_id="a1",
            thread_id="t1", execution_id="e1",
        )
        request = _make_request(command="shake")

        calls = 0

        async def handler(exc: Exception, command: str) -> RecoveryDecision:
            nonlocal calls
            calls += 1
            if calls == 1:
                return RecoveryDecision.RETRY_OP
            return RecoveryDecision.ABORT_THREAD

        token = device_op_recovery_handler.set(handler)
        try:
            with pytest.raises(Exception):
                await dispatcher.dispatch(
                    request, affected_labware=[], affected_labware_ids=[]
                )
        finally:
            device_op_recovery_handler.reset(token)

        assert len(device.shake_calls) == 2


class _FakeTipRackInstance(LabwareInstance):
    """A real LabwareInstance subclass so it satisfies the ``instances``
    parameter's type, overriding only the pre-flight check's ledger read."""

    def __init__(
        self,
        rack_name: str,
        missing: list[str] | None = None,
        remaining: int = 0,
        provenance: Provenance = Provenance.KNOWN,
        unobserved: bool = False,
    ) -> None:
        super().__init__(template_name=rack_name, labware_type=rack_name, name=rack_name)
        self.missing = missing or []
        self.remaining = remaining
        self.provenance = provenance
        # Two separate questions on the real instance: how well the record
        # knows the rack, and whether a stretch went by with nobody watching.
        self.unobserved = unobserved
        self.calls = 0

    async def missing_tip_positions(self, positions: Sequence[str]) -> list[str]:
        self.calls += 1
        return self.missing

    async def tip_count_present(self) -> int:
        return self.remaining

    async def contents_provenance(self) -> Provenance:
        return self.provenance

    async def went_unobserved(self) -> bool:
        return self.unobserved


async def _rack_missing_a1(cause: ObservationGapCause) -> TipRackInstance:
    """A real rack the record says has no tip at A1, with one gap standing."""
    history = OpsHistory()
    backing = MagicMock()
    backing.name = f"tips_{cause.value}"
    backing.model = "hamilton_96_tiprack"
    rack = TipRackInstance(
        backing, template_name=backing.name, labware_type="hamilton_96_tiprack",
    )
    bind_ledger(rack, history)
    await _seed_rack(history, rack.name, ["A1", "A2"])
    await _append_pickup(history, rack.name, ["A1"])
    await rack.note_observation_gap(cause)
    return rack


class _ClaimingInterpreter:
    """Interpreter double that claims fixed positions for pick_up_tips only,
    mirroring LiquidHandlerInterpreter.claimed_tip_positions's contract
    without parsing real tip_spot args. The other three methods are never
    exercised by these tests; they exist only to satisfy the protocol."""

    def __init__(self, claimed: dict[str, list[str]]) -> None:
        self._claimed = claimed

    def claimed_tip_positions(
        self, command: str, args: tuple[Any, ...], kwargs: dict[str, Any],
    ) -> dict[str, list[str]]:
        return self._claimed if command == "pick_up_tips" else {}

    def interpret(
        self,
        command: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        result: LabwareStateResponse | None,
        device_name: str,
        affected_labware: list[str],
        affected_labware_ids: list[str],
        action_id: str,
        thread_id: str,
    ) -> list[OperationRecord]:
        return []

    def interpret_driver_state(
        self,
        command: str,
        result: LabwareStateResponse | None,
        device_name: str,
        affected_labware: list[str],
        affected_labware_ids: list[str],
        action_id: str,
        thread_id: str,
    ) -> list[OperationRecord]:
        return []

    def interpret_per_channel_outcomes(
        self,
        command: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        result: LabwareStateResponse | None,
        device_name: str,
        affected_labware: list[str],
        affected_labware_ids: list[str],
        action_id: str,
        thread_id: str,
    ) -> list[OperationRecord]:
        return []


class _PickDevice:
    """Device shape with a pick_up_tips method that records every call."""
    __slots__ = ("name", "lock", "under_external_control", "pick_up_tips", "pick_calls")

    def __init__(self) -> None:
        self.name = "lh1"
        self.lock = TrackedLock("dev device lock")
        self.under_external_control = False
        self.pick_calls: list[tuple[Any, ...]] = []

        async def pick_up_tips(*args: Any, **kwargs: Any) -> None:
            self.pick_calls.append((args, kwargs))

        self.pick_up_tips = pick_up_tips

    def command_max_seconds(self, command: str) -> float | None:
        return None


class TestTipPreflightCheck:
    """Defect 4: nothing checked the ledger before committing to a tip pick,
    so a driver's own tip-detection sensor was the only thing that could
    catch a hand-named or stale position -- and not every device has one.
    These pin the check running before the device call, using the same
    RETRY_OP recovery seam a real driver failure already goes through."""

    @pytest.mark.asyncio
    async def test_missing_positions_block_the_call_and_name_them(self) -> None:
        rack = _FakeTipRackInstance("tips_1", missing=["A1"], remaining=3)
        interpreter = _ClaimingInterpreter({"tips_1": ["A1"]})
        device = _PickDevice()
        dispatcher = DeviceCallDispatcher(
            device=device, interpreter=interpreter, operation_log=[],
            action_id="a1", thread_id="t1", execution_id="e1",
        )
        request = _make_request(command="pick_up_tips")

        with pytest.raises(TipsNotPresentError) as exc_info:
            await dispatcher.dispatch(
                request, affected_labware=["tips_1"], affected_labware_ids=["id1"],
                instances=[rack],
            )

        assert exc_info.value.rack_name == "tips_1"
        assert exc_info.value.rack_id == rack.id
        assert exc_info.value.missing_positions == ["A1"]
        assert exc_info.value.remaining_count == 3
        assert str(rack.id) in str(exc_info.value)
        assert device.pick_calls == []
        assert request.completion.is_set()

    @pytest.mark.asyncio
    async def test_a_stale_reading_does_not_block_the_call(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """After a restart every rack reads stale: the fold describes what was
        true before the gap, and the gap is exactly when an operator reloads a
        rack by hand. Blocking on that would stop the first pick after every
        restart on a reading nobody has checked."""
        rack = _FakeTipRackInstance(
            "tips_1", missing=["A1"], remaining=0, provenance=Provenance.STALE,
            unobserved=True,
        )
        interpreter = _ClaimingInterpreter({"tips_1": ["A1"]})
        device = _PickDevice()
        dispatcher = DeviceCallDispatcher(
            device=device, interpreter=interpreter, operation_log=[],
            action_id="a1", thread_id="t1", execution_id="e1",
        )
        request = _make_request(command="pick_up_tips")

        with caplog.at_level("WARNING", logger="orca"):
            await dispatcher.dispatch(
                request, affected_labware=["tips_1"], affected_labware_ids=["id1"],
                instances=[rack],
            )

        assert len(device.pick_calls) == 1
        assert request.error is None
        assert "tips_1" in caplog.text
        assert "A1" in caplog.text

    @pytest.mark.asyncio
    async def test_a_reading_that_is_merely_behind_still_blocks_the_call(
        self,
    ) -> None:
        """A rack reads stale for two different reasons and only one of them is
        a reason to be lenient.

        An unwatched stretch could have had a hand refill the rack, so the pick
        goes through. Operations an unfinished action has not written down can
        only have taken tips OFF, so a position the record already calls empty
        is still empty and the pick must not go anywhere near it. Two threads
        sharing one rack is all it takes to reach this: one of them is always
        mid-action.
        """
        rack = _FakeTipRackInstance(
            "tips_1", missing=["A1"], remaining=0, provenance=Provenance.STALE,
            unobserved=False,
        )
        interpreter = _ClaimingInterpreter({"tips_1": ["A1"]})
        device = _PickDevice()
        dispatcher = DeviceCallDispatcher(
            device=device, interpreter=interpreter, operation_log=[],
            action_id="a1", thread_id="t1", execution_id="e1",
        )

        with pytest.raises(TipsNotPresentError):
            await dispatcher.dispatch(
                _make_request(command="pick_up_tips"),
                affected_labware=["tips_1"], affected_labware_ids=["id1"],
                instances=[rack],
            )

        assert device.pick_calls == []

    @pytest.mark.asyncio
    async def test_the_gate_asks_a_real_rack_which_kind_of_stale_it_is(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The same rack, the same missing position, two gaps, two answers.

        Drives a real ``TipRackInstance`` rather than a double, because the
        distinction lives in the instance and a double answering the question
        for it would pin nothing.
        """
        aborted = await _rack_missing_a1(ObservationGapCause.OPERATIONS_DROPPED)
        restarted = await _rack_missing_a1(ObservationGapCause.RUNTIME_RESTART)
        device = _PickDevice()
        dispatcher = DeviceCallDispatcher(
            device=device,
            interpreter=_ClaimingInterpreter({aborted.name: ["A1"]}),
            operation_log=[], action_id="a1", thread_id="t1", execution_id="e1",
        )

        with pytest.raises(TipsNotPresentError):
            await dispatcher.dispatch(
                _make_request(command="pick_up_tips"),
                affected_labware=[aborted.name], affected_labware_ids=[aborted.id],
                instances=[aborted],
            )
        assert device.pick_calls == []

        lenient = DeviceCallDispatcher(
            device=device,
            interpreter=_ClaimingInterpreter({restarted.name: ["A1"]}),
            operation_log=[], action_id="a2", thread_id="t1", execution_id="e1",
        )
        with caplog.at_level("WARNING", logger="orca"):
            await lenient.dispatch(
                _make_request(command="pick_up_tips"),
                affected_labware=[restarted.name],
                affected_labware_ids=[restarted.id],
                instances=[restarted],
            )

        assert len(device.pick_calls) == 1, (
            "a stretch nobody watched is when a hand reloads a rack"
        )

    @pytest.mark.asyncio
    async def test_present_positions_reach_the_driver(self) -> None:
        rack = _FakeTipRackInstance("tips_1", missing=[])
        interpreter = _ClaimingInterpreter({"tips_1": ["A1"]})
        device = _PickDevice()
        dispatcher = DeviceCallDispatcher(
            device=device, interpreter=interpreter, operation_log=[],
            action_id="a1", thread_id="t1", execution_id="e1",
        )
        request = _make_request(command="pick_up_tips")

        await dispatcher.dispatch(
            request, affected_labware=["tips_1"], affected_labware_ids=["id1"],
            instances=[rack],
        )

        assert len(device.pick_calls) == 1
        assert request.error is None

    @pytest.mark.asyncio
    async def test_no_interpreter_skips_the_check(self) -> None:
        rack = _FakeTipRackInstance("tips_1", missing=["A1"])
        device = _PickDevice()
        dispatcher = DeviceCallDispatcher(
            device=device, interpreter=None, operation_log=[],
            action_id="a1", thread_id="t1", execution_id="e1",
        )
        request = _make_request(command="pick_up_tips")

        await dispatcher.dispatch(
            request, affected_labware=["tips_1"], affected_labware_ids=["id1"],
            instances=[rack],
        )

        assert len(device.pick_calls) == 1
        assert rack.calls == 0

    @pytest.mark.asyncio
    async def test_a_claimed_rack_absent_from_instances_is_not_checked(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The interpreter claims a rack the action has no instance for --
        nothing to check against, so the call proceeds. A real mismatch
        here is a wiring bug elsewhere, not something this check can see,
        so it logs loud instead of failing silent."""
        interpreter = _ClaimingInterpreter({"tips_1": ["A1"]})
        device = _PickDevice()
        dispatcher = DeviceCallDispatcher(
            device=device, interpreter=interpreter, operation_log=[],
            action_id="a1", thread_id="t1", execution_id="e1",
        )
        request = _make_request(command="pick_up_tips")

        with caplog.at_level("WARNING", logger="orca"):
            await dispatcher.dispatch(
                request, affected_labware=[], affected_labware_ids=[], instances=[],
            )

        assert len(device.pick_calls) == 1
        assert "tips_1" in caplog.text

    @pytest.mark.asyncio
    async def test_retry_op_re_checks_the_ledger_not_just_the_driver(self) -> None:
        """The operator loads tips and corrects the ledger between attempts;
        RETRY_OP must see that update, not repeat the same doomed check
        against a rack frozen at its first-attempt reading."""
        rack = _FakeTipRackInstance("tips_1", missing=["A1"])
        interpreter = _ClaimingInterpreter({"tips_1": ["A1"]})
        device = _PickDevice()
        dispatcher = DeviceCallDispatcher(
            device=device, interpreter=interpreter, operation_log=[],
            action_id="a1", thread_id="t1", execution_id="e1",
        )
        request = _make_request(command="pick_up_tips")

        calls = 0

        async def handler(exc: Exception, command: str) -> RecoveryDecision:
            nonlocal calls
            calls += 1
            assert calls == 1, "should not need a second recovery decision"
            rack.missing = []  # operator loaded tips and ran `set-tips`
            return RecoveryDecision.RETRY_OP

        token = device_op_recovery_handler.set(handler)
        try:
            await dispatcher.dispatch(
                request, affected_labware=["tips_1"], affected_labware_ids=["id1"],
                instances=[rack],
            )
        finally:
            device_op_recovery_handler.reset(token)

        assert calls == 1
        assert len(device.pick_calls) == 1
        assert request.error is None

    @pytest.mark.asyncio
    async def test_real_interpreters_rack_name_matches_the_instance_key(self) -> None:
        """Every other test here uses ``_ClaimingInterpreter`` with a
        hand-picked rack-name key, so nothing pinned that the REAL
        LiquidHandlerInterpreter's parsed rack name is the same string the
        dispatcher keys instances by. Holds by construction today (both
        read the rack's name off the same tip_spots), but was never
        actually exercised end-to-end until now."""
        rack = _FakeTipRackInstance("tips_96", missing=["A1"])
        interpreter = LiquidHandlerInterpreter()
        device = _PickDevice()
        dispatcher = DeviceCallDispatcher(
            device=device, interpreter=interpreter, operation_log=[],
            action_id="a1", thread_id="t1", execution_id="e1",
        )
        request = ActionRequest(
            device_name="lh1", command="pick_up_tips",
            args=([_FakeTipSpot("tips_96", "A1")],), kwargs={},
        )

        with pytest.raises(TipsNotPresentError) as exc_info:
            await dispatcher.dispatch(
                request, affected_labware=["tips_96"], affected_labware_ids=["id1"],
                instances=[rack],
            )

        assert exc_info.value.rack_name == "tips_96"
        assert device.pick_calls == []

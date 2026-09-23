"""Per-request dispatch body for ActionBodyLocationAction.execute().

OperationLogBuilder owns the three-step interpreter contract:
``interpret_per_channel_outcomes`` -> ``interpret`` -> ``interpret_driver_state``.
DeviceCallDispatcher owns the per-request body of the action's queue loop:
error short-circuit, method resolution, external-control gate, locked
device call, interpreter dispatch.

The user-task lifecycle (spawn, drain on outer finally, ``wait_for``
timeout) stays in ``ActionBodyLocationAction.execute``. Dispatch is
synchronous from the caller's point of view: every request that
enters ``dispatch`` exits with ``request.completion`` set, on success
and on failure -- so user code parked on
``request.completion.wait()`` always wakes, even when an exception
propagates out of ``dispatch``.
"""

import logging
from typing import Any, Awaitable, Callable, Iterable, List, Mapping, Protocol, Sequence, runtime_checkable

from cheshire_drivers.liquid_handler_models import (
    LabwareStateResponse,
    ReconcileHardwareStateResponse,
)

from orca.resource_models.tracked_lock import TrackedLock
from orca.resource_models.device_error import (
    CommandTimeoutAbortedError,
    DeviceStateDivergenceError,
    DeviceUnderExternalControlError,
    TipsNotPresentError,
)
from orca.resource_models.labware import LabwareInstance
from orca.state.records import OperationRecord
from orca.resource_models.tracking_interpreter import IOperationInterpreter
from orca.runtime.recoverable_timeout import (
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    recoverable_timeout_coordinator,
)
from orca.runtime.run_modes import current_execution_id
from orca.workflow_models.actions.operation_recovery import (
    OperationDecisionSignal,
    device_op_recovery_handler,
)
from orca.workflow_models.device_handle import ActionRequest
from orca.workflow_models.error_policy_overrides import OverrideWithPauseError
from orca.workflow_models.status_enums import RecoveryDecision

logger = logging.getLogger("orca")


@runtime_checkable
class IHardwareReconcilable(Protocol):
    """A device that can re-read hardware ground truth and repair its driver's
    cached state (today: liquid handlers). Checked structurally so the
    dispatcher stays independent of concrete device classes."""

    async def reconcile_hardware_state(self) -> ReconcileHardwareStateResponse: ...


@runtime_checkable
class IDispatchableDevice(Protocol):
    """Structural device contract DeviceCallDispatcher actually uses.

    Defined here -- not in ``resource_models.devices`` -- because this is
    the dispatcher's view of a device, not the full device surface.
    Concrete ``Device`` subclasses satisfy this structurally; the dispatcher
    does not import or depend on the full class.
    """

    @property
    def name(self) -> str: ...

    @property
    def lock(self) -> TrackedLock: ...

    @property
    def under_external_control(self) -> bool: ...

    def command_max_seconds(self, command: str) -> float | None: ...


class PartialChannelFailureError(RuntimeError):
    """Raised when a driver call returned partial-channel failure records.

    Drivers signal partial failure by returning a structured response rather
    than raising. The builder emits one record per affected well and raises
    this so the workflow's failure policy fires.
    """

    def __init__(self, records: List[OperationRecord]) -> None:
        super().__init__(
            f"liquid handler call had {len(records)} per-channel records "
            f"(at least one channel errored)."
        )
        self.records = records


class OperationLogBuilder:
    """Runs an interpreter against a device-call result and extends the log.

    Three-step dispatch matching the interpreter contract:
    1. ``interpret_per_channel_outcomes`` -- if it returns records, extend
       the log and raise ``PartialChannelFailureError`` so the workflow
       fails over.
    2. ``interpret`` -- every OBSERVED record the call produced, appended in order.
    3. ``interpret_driver_state`` -- zero or more DRIVER_OBSERVED records
       extracted from the result, always extended.
    """

    def __init__(
        self,
        interpreter: IOperationInterpreter,
        operation_log: List[OperationRecord],
        device_name: str,
        action_id: str,
        thread_id: str,
    ) -> None:
        self._interpreter = interpreter
        self._operation_log = operation_log
        self._device_name = device_name
        self._action_id = action_id
        self._thread_id = thread_id

    def _record(self, records: object, source: str) -> None:
        """Append interpreted operations, refusing anything that is not one.

        An interpreter branch that returns a bare record instead of a list gets
        iterated field-by-field here, and the tuples it yields fail validation
        later, where they take the whole action's history down with them.
        """
        if isinstance(records, OperationRecord) or not isinstance(records, Iterable):
            raise TypeError(
                f"{source} produced {type(records).__name__}, not a list of "
                f"OperationRecord; its operations would be lost"
            )
        for entry in records:
            if not isinstance(entry, OperationRecord):
                raise TypeError(
                    f"{source} produced a {type(entry).__name__} where an "
                    f"OperationRecord belongs: {entry!r}"
                )
            self._operation_log.append(entry)

    def process(
        self,
        request: ActionRequest,
        result: LabwareStateResponse | None,
        affected_labware: List[str],
        affected_labware_ids: List[str],
    ) -> None:
        per_channel = self._interpreter.interpret_per_channel_outcomes(
            command=request.command,
            args=request.args,
            kwargs=request.kwargs,
            result=result,
            device_name=self._device_name,
            affected_labware=affected_labware,
            affected_labware_ids=affected_labware_ids,
            action_id=self._action_id,
            thread_id=self._thread_id,
        )
        if per_channel:
            self._record(per_channel, f"per-channel interpretation of {request.command}")
            raise PartialChannelFailureError(per_channel)

        self._record(self._interpreter.interpret(
            command=request.command,
            args=request.args,
            kwargs=request.kwargs,
            result=result,
            device_name=self._device_name,
            affected_labware=affected_labware,
            affected_labware_ids=affected_labware_ids,
            action_id=self._action_id,
            thread_id=self._thread_id,
        ), f"interpretation of {request.command}")

        state_records = self._interpreter.interpret_driver_state(
            command=request.command,
            result=result,
            device_name=self._device_name,
            affected_labware=affected_labware,
            affected_labware_ids=affected_labware_ids,
            action_id=self._action_id,
            thread_id=self._thread_id,
        )
        self._record(state_records, f"state records for {request.command}")


class DeviceCallDispatcher:
    """Runs one ``ActionRequest`` against a device + optional interpreter.

    Contract: every request that enters ``dispatch`` exits with
    ``request.completion`` set. On the success path ``request.result`` is
    populated; on the failure path ``request.error`` is populated and the
    exception re-raises out of ``dispatch``. Either way the user task
    waiting on ``request.completion.wait()`` wakes at the next event-loop
    tick.

    The external-control gate is checked twice (before and after the
    device lock acquire) because the gateway flag-write is not
    synchronized with the lock -- a gateway take that lands while we
    are queued for the lock has to be honored.
    """

    def __init__(
        self,
        device: IDispatchableDevice,
        interpreter: IOperationInterpreter | None,
        operation_log: List[OperationRecord],
        action_id: str,
        thread_id: str,
        execution_id: str,
    ) -> None:
        self._device = device
        self._action_id = action_id
        self._execution_id = execution_id
        self._interpreter = interpreter
        self._log_builder = (
            OperationLogBuilder(interpreter, operation_log, device.name, action_id, thread_id)
            if interpreter is not None else None
        )

    async def dispatch(
        self,
        request: ActionRequest,
        affected_labware: List[str],
        affected_labware_ids: List[str],
        instances: Sequence[LabwareInstance] = (),
    ) -> None:
        try:
            if request.error is not None:
                raise request.error
            method = getattr(self._device, request.command, None)
            if method is None:
                available = [
                    m for m in dir(self._device)
                    if not m.startswith("_")
                    and callable(getattr(self._device, m, None))
                ]
                raise AttributeError(
                    f"Device '{self._device.name}' has no method "
                    f"'{request.command}'. Available: {available}"
                )
            result = await self._call_with_op_recovery(request, method, instances)
            if self._log_builder is not None:
                # Interpreters consume only structured driver state; other
                # returns (mock strings, None) narrow to None here, once.
                state = result if isinstance(result, LabwareStateResponse) else None
                self._log_builder.process(
                    request, state, affected_labware, affected_labware_ids,
                )
            request.result = result
        except Exception as exc:
            request.error = exc
            raise
        finally:
            request.completion.set()

    async def _call_with_op_recovery(
        self,
        request: ActionRequest,
        method: Callable[..., Awaitable[Any]],
        instances: Sequence[LabwareInstance],
    ) -> Any:
        """Run the device call; on a driver failure consult the per-thread
        operation-recovery seam. RETRY_OP re-invokes ONLY this call while the
        action body stays suspended at its await; an action-level decision (RETRY
        / ABORT_*) propagates as an OperationDecisionSignal for the existing
        whole-action path. With no seam armed, or for an external-coordination /
        already-aborted error, the exception propagates unchanged.
        """
        handler = device_op_recovery_handler.get()
        reconcile_pending = False
        while True:
            self._raise_if_under_external_control()
            try:
                async with self._device.lock.held_for(request.command):
                    self._raise_if_under_external_control()
                    if reconcile_pending:
                        reconcile_pending = False
                        await self._reconcile_before_retry()
                    await self._check_tips_present(request, instances)
                    return await self._call_with_recoverable_timeout(
                        request.command, method, request.args, request.kwargs,
                    )
            except (OverrideWithPauseError, CommandTimeoutAbortedError):
                raise
            except Exception as exc:
                if handler is None:
                    raise
                # Consult the recovery seam OUTSIDE the device lock: an operator
                # pause can take hours; holding it would block the multi-site device.
                decision = await handler(exc, request.command)
                if decision == RecoveryDecision.RETRY_OP:
                    reconcile_pending = True
                    continue
                raise OperationDecisionSignal(decision, exc) from exc

    async def _check_tips_present(
        self, request: ActionRequest, instances: Sequence[LabwareInstance],
    ) -> None:
        """Refuse a tip pick whose requested positions the ledger reads
        empty, before the call reaches the driver.

        Only on a reading nobody has questioned. A STALE rack was last
        described before a restart, a reconnect or an error pause, and
        nobody has looked since; the same gap is when an operator reloads a
        rack by hand. Refusing on that would stop the first pick after every
        restart on a reading that is not evidence, and the operator could
        only clear it by restating the whole rack. It logs and lets the pick
        go instead; the rack is already on the unsettled worklist.

        Runs every attempt, including a RETRY_OP re-issue: an operator who
        loaded tips and corrected the ledger between attempts needs the
        retry to see that, not repeat the same doomed check. No interpreter
        (or one with no tip concept, or a rack this action never had
        assigned) claims no positions, so this is a no-op for every other
        device call.
        """
        if self._interpreter is None:
            return
        claimed = self._interpreter.claimed_tip_positions(
            request.command, request.args, request.kwargs,
        )
        if not claimed:
            return
        by_name = {instance.name: instance for instance in instances}
        for rack_name, positions in claimed.items():
            rack = by_name.get(rack_name)
            if rack is None:
                logger.warning(
                    "_check_tips_present: interpreter claimed positions on "
                    "rack '%s' but no matching instance was passed to "
                    "dispatch() -- the pre-flight check did not run for it.",
                    rack_name,
                )
                continue
            missing = await rack.missing_tip_positions(positions)
            if not missing:
                continue
            if await rack.went_unobserved():
                logger.warning(
                    "_check_tips_present: rack '%s' reads empty at %s, but "
                    "nobody has looked at it since the last observation gap; "
                    "letting the pick through. Look at the rack, then `orca "
                    "labware confirm-tips %s` if the record was right, or "
                    "`orca labware set-tips %s --position ...` if it was "
                    "reloaded. Either one makes this check meaningful again.",
                    rack.name, ", ".join(missing), rack.id, rack.id,
                )
                continue
            remaining = await rack.tip_count_present()
            raise TipsNotPresentError(rack.name, rack.id, missing, remaining)

    async def _reconcile_before_retry(self) -> None:
        """Re-read hardware ground truth before re-issuing the failed call.

        The failure being retried may have been resolved AT the instrument
        (e-stop, touchscreen recovery), which kills the device-side session and
        can leave the driver tracking tips that are gone; retrying against that
        state computes motion from a world that no longer exists.

        Only a divergence report stops a retry. A reconcile that FAILS is
        logged and the retry proceeds as it did before this hook existed:
        failing the retry on it would hand the operator the reconcile's error
        under the name of the command they were recovering. Operator-decision
        signals still propagate, so external control and an aborted timeout are
        not overridden here.

        Only RETRY_OP is guarded; a whole-action RETRY re-drives unreconciled
        and a dead session then fails its first call with the typed run-death
        error.
        """
        if not isinstance(self._device, IHardwareReconcilable):
            return
        try:
            report = await self._call_with_recoverable_timeout(
                "reconcile_hardware_state",
                self._device.reconcile_hardware_state,
                (),
                {},
            )
        except (OverrideWithPauseError, CommandTimeoutAbortedError):
            raise
        except Exception as reconcile_error:
            logger.warning(
                "Pre-retry hardware reconcile on '%s' failed (%s); retrying unreconciled",
                self._device.name,
                reconcile_error,
            )
            return
        if not isinstance(report, ReconcileHardwareStateResponse):
            return
        if report.session_recovered or any(
            m.outcome == "cleared_lost_tips" for m in report.mounts
        ):
            logger.info(
                "Pre-retry hardware reconcile repaired '%s': %s",
                self._device.name,
                report.message,
            )
        if report.requires_intervention:
            raise DeviceStateDivergenceError(
                report.message or "hardware and driver state diverge",
                device_name=self._device.name,
            )

    async def _call_with_recoverable_timeout(
        self,
        command: str,
        method: Callable[..., Awaitable[Any]],
        args: Sequence[Any],
        kwargs: Mapping[str, Any],
    ) -> Any:
        """Run the device call under the engine's recoverable timeout if armed.

        The coordinator and the execution id are both read from the per-thread
        context seeded at thread start (the single source for both). With no
        coordinator or no execution id (unit tests, direct dispatch with no
        runtime) the call runs unbounded -- the prior behavior. An un-annotated
        command (no advertised ``max_seconds``) is bounded by
        ``DEFAULT_COMMAND_TIMEOUT_SECONDS`` so it can never hang the execution
        unboundedly; a non-positive ``max_seconds`` (driver metadata
        misconfigured) is treated as "no timeout" rather than an immediate
        timeout that would wedge the execution.
        """
        coordinator = recoverable_timeout_coordinator.get()
        execution_id = current_execution_id.get()
        if coordinator is None or execution_id is None:
            return await method(*args, **kwargs)
        max_seconds = self._device.command_max_seconds(command)
        if max_seconds is None:
            max_seconds = DEFAULT_COMMAND_TIMEOUT_SECONDS
        elif max_seconds <= 0:
            return await method(*args, **kwargs)
        return await coordinator.run_with_timeout(
            execution_id=execution_id,
            device_id=self._device.name,
            command=command,
            max_seconds=max_seconds,
            coro_factory=lambda: method(*args, **kwargs),
        )

    def _raise_if_under_external_control(self) -> None:
        if self._device.under_external_control:
            raise DeviceUnderExternalControlError(device_name=self._device.name)

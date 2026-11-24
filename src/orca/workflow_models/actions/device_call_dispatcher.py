"""Per-request dispatch body for ActionBodyLocationAction.execute().

CommandRecorder owns the three-step interpreter contract:
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

import json
import logging
import time
from typing import Any, Awaitable, Callable, Iterable, List, Mapping, Protocol, Sequence, runtime_checkable

from cheshire_drivers.command_table import COMMANDS
from pydantic import BaseModel, JsonValue
from cheshire_drivers.liquid_handler_models import (
    LabwareStateResponse,
    ReconcileHardwareStateResponse,
)

from orca.gateway.controller.command_events import params_as_sent
from orca.gateway.controller.command_pipeline import IDeviceCommandRunner, PartialChannelFailureError
from orca.gateway.controller.exceptions import CommandNotSavedError
from orca.resource_models.tracked_lock import TrackedLock
from orca.resource_models.device_error import (
    CommandTimeoutAbortedError,
    DeviceStateDivergenceError,
    DeviceUnderExternalControlError,
    ReagentRanOutError,
)
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.tip_preflight import refuse_a_pick_from_empty_spots, tips_claimed_by
from orca.state.command_operations import labware_named_in
from orca.state.records import OperationRecord
from orca.resource_models.tracking_interpreter import IOperationInterpreter
from orca.runtime.recoverable_timeout import time_limit, within_the_time_limit

from orca.modes.run_modes import WorkflowRunMode, current_run_mode
from orca.modes.sim_pacing import declared_duration
from orca.resource_models.resources import IModeAware
from orca.workflow_models.actions.operation_recovery import (
    DeviceOpRecoveryHandler,
    OperationDecisionSignal,
    UnsavedResult,
    device_op_recovery_handler,
)
from orca.workflow_models.device_handle import ActionRequest
from orca.resource_models.error_policy_overrides import OverrideWithPauseError
from orca.workflow_models.pause_checkpoint import IPauseCheckpoint
from orca.workflow_models.status_enums import PauseSite, RecoveryDecision

logger = logging.getLogger("orca")


@runtime_checkable
class IHardwareReconcilable(Protocol):
    """A device that can re-read hardware ground truth and repair its driver's
    cached state (today: liquid handlers). Checked structurally so the
    dispatcher stays independent of concrete device classes."""

    async def reconcile_hardware_state(self) -> ReconcileHardwareStateResponse: ...


class NotAWorkflowCommandError(AttributeError):
    """A workflow named one of Orca's own device methods that is not a device command."""


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

    def command_typical_seconds(self, command: str) -> float | None: ...


def _jsonable(value: Any) -> JsonValue:
    """A model as its JSON; anything JSON cannot hold as its repr."""
    return value.model_dump(mode="json") if isinstance(value, BaseModel) else repr(value)


def _as_json(value: Any) -> str:
    return json.dumps(value, default=_jsonable)


def refuse_an_engine_method(device: IDispatchableDevice, name: str) -> None:
    """Refuse a method Orca's device classes define for the engine, such as a deck wipe, and a lifecycle command.

    A device command is allowed, and so is any method a user's own device class adds. A lifecycle command is
    refused as the operator's `execute` refuses it: only its own Operation brings the deck in line after it.
    """
    if name in COMMANDS.lifecycle_names():
        raise NotAWorkflowCommandError(
            f"{name!r} is a lifecycle command, so a workflow may not call it on {device.name!r}. "
            f"Send it with its own operation, which brings Orca's record of the device in line after it."
        )
    defined_by = next((cls for cls in type(device).__mro__ if name in vars(cls)), None)
    if defined_by is None or not defined_by.__module__.startswith("orca."):
        return
    if name in COMMANDS.external_names():
        return
    raise NotAWorkflowCommandError(
        f"{name!r} is not a device command, so a workflow may not call it on {device.name!r}. "
        f"It is part of how Orca keeps its own record of the device."
    )


class CommandRecorder:
    """Saves what a completed device command did, before the command counts as done, whichever route sent it.

    ``DeviceCallDispatcher`` cannot be built without one, the operator runner saves through one too, and a
    failed save fails the command.
    """

    def __init__(
        self,
        interpreter: IOperationInterpreter,
        save: Callable[[list[OperationRecord]], Awaitable[None]],
        device_name: str,
        action_id: str,
        thread_id: str,
    ) -> None:
        self.interpreter = interpreter
        self._save = save
        self._device_name = device_name
        self._action_id = action_id
        self._thread_id = thread_id

    @staticmethod
    def _checked(
        records: Iterable[OperationRecord] | OperationRecord, source: str,
    ) -> list[OperationRecord]:
        """An interpreter branch returning a bare record would otherwise be iterated field by field."""
        if isinstance(records, OperationRecord) or not isinstance(records, Iterable):
            raise TypeError(
                f"{source} produced {type(records).__name__}, not a list of "
                f"OperationRecord; its operations would be lost"
            )
        checked: list[OperationRecord] = []
        for entry in records:
            if not isinstance(entry, OperationRecord):
                raise TypeError(
                    f"{source} produced a {type(entry).__name__} where an "
                    f"OperationRecord belongs: {entry!r}"
                )
            checked.append(entry)
        return checked

    def labware_named_by(self, request: ActionRequest) -> list[str]:
        """The labware the command names as its own: empty for one that names none, such as a shake or a discard."""
        try:
            operations = self.interpreter.interpret(
                command=request.command, args=request.args, kwargs=request.kwargs, result=None,
                device_name=self._device_name, affected_labware=[], affected_labware_ids=[],
                action_id=self._action_id, thread_id=self._thread_id,
            )
        except Exception:
            # A call the reader cannot read is refused by the device call itself. Here it names nothing.
            return []
        return labware_named_in(operations)

    async def save_completed(
        self,
        request: ActionRequest,
        result: LabwareStateResponse | None,
        affected_labware: List[str],
        affected_labware_ids: List[str],
    ) -> None:
        per_channel = self.interpreter.interpret_per_channel_outcomes(
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
            await self.save_operations(per_channel, f"per-channel interpretation of {request.command}")
            raise PartialChannelFailureError(per_channel)

        operations = self._checked(self.interpreter.interpret(
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
        operations += self._checked(self.interpreter.interpret_driver_state(
            command=request.command,
            result=result,
            device_name=self._device_name,
            affected_labware=affected_labware,
            affected_labware_ids=affected_labware_ids,
            action_id=self._action_id,
            thread_id=self._thread_id,
        ), f"state records for {request.command}")
        await self.save_operations(operations, f"interpretation of {request.command}")

    async def save_operations(self, operations: Iterable[OperationRecord] | OperationRecord, source: str) -> None:
        """The one save point: what a command did, written to the record before the command counts as done."""
        checked = self._checked(operations, source)
        if checked:
            await self._save(checked)


class DeviceCallDispatcher:
    """Runs one ``ActionRequest`` against a device + optional interpreter.

    Contract: every request that enters ``dispatch`` exits with
    ``request.completion`` set. On the success path ``request.result`` is
    populated; on the failure path ``request.error`` is populated and the
    exception re-raises out of ``dispatch``. Either way the user task
    waiting on ``request.completion.wait()`` wakes once it is set, which on
    success waits out any pause asked for during the command.

    The external-control gate is checked twice (before and after the
    device lock acquire) because the gateway flag-write is not
    synchronized with the lock -- a gateway take that lands while we
    are queued for the lock has to be honored.
    """

    def __init__(
        self,
        device: IDispatchableDevice,
        recorder: CommandRecorder,
        pipeline: IDeviceCommandRunner,
        action_id: str,
        execution_id: str,
        pause_checkpoint: IPauseCheckpoint | None = None,
    ) -> None:
        self._device = device
        self._pipeline = pipeline
        self._action_id = action_id
        self._execution_id = execution_id
        self._recorder = recorder
        self._pause_checkpoint = pause_checkpoint

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
            refuse_an_engine_method(self._device, request.command)
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
            request.result = await self._call_with_op_recovery(
                request, method, instances, affected_labware, affected_labware_ids,
            )
            await self._hold_between_commands()
        except Exception as exc:
            request.error = exc
            raise
        finally:
            request.completion.set()

    async def _hold_between_commands(self) -> None:
        """Honour a pending pause here, so an action stops after the command that was running."""
        if self._pause_checkpoint is not None:
            await self._pause_checkpoint.hold_if_pause_requested(
                message="held between instrument commands; resume to run the next one",
            )

    async def _call_with_op_recovery(
        self,
        request: ActionRequest,
        method: Callable[..., Awaitable[Any]],
        instances: Sequence[LabwareInstance],
        affected_labware: List[str],
        affected_labware_ids: List[str],
    ) -> Any:
        """Run the device call and save it; on a failure consult the per-thread operation-recovery seam.

        A failed call pauses at DEVICE_OP, where RETRY re-invokes only this call. A call that ran and whose save
        failed pauses at COMMAND_SAVE, where RETRY saves the same answer again and sends nothing. Either way the
        action body stays suspended at its await, and an action-level decision propagates as an
        OperationDecisionSignal. With no seam armed, or for an external-coordination / already-aborted error, the
        exception propagates unchanged.
        """
        handler = device_op_recovery_handler.get()
        named = self._recorder.labware_named_by(request)
        params = self._params_as_sent(request)
        reconcile_pending = False
        unsaved: list[Any] = []

        async def save(answer: Any) -> None:
            try:
                await self._save(request, answer, affected_labware, affected_labware_ids)
            except PartialChannelFailureError:
                raise
            except Exception:
                unsaved.append(answer)
                raise

        while True:
            unsaved.clear()
            self._raise_if_under_external_control()
            try:
                async with self._device.lock.held_for(request.command):
                    self._raise_if_under_external_control()
                    if reconcile_pending:
                        reconcile_pending = False
                        await self._reconcile_before_retry()
                    # A command cut off at the device is the pipeline's no-answer stage.
                    return await self._pipeline.run_device_command(
                        self._device.name, request.command,
                        lambda: self._call_with_recoverable_timeout(
                            request.command, method, request.args, request.kwargs,
                        ),
                        save,
                        checks=(
                            lambda: self._check_tips_present(request, instances),
                            lambda: self._check_reagent_present(request, instances),
                        ),
                        execution_id=self._execution_id, named=named, params=params,
                    )
            except CommandNotSavedError as not_saved:
                # A command that ran is never re-run. A no-answer report that was not filed has no answer to save.
                if handler is None or not unsaved:
                    raise
                try:
                    return await self._save_again_until_saved(
                        request, unsaved[0], not_saved, handler, affected_labware, affected_labware_ids,
                    )
                except PartialChannelFailureError as partial:
                    failed: Exception = partial
            except (OverrideWithPauseError, CommandTimeoutAbortedError):
                raise
            except Exception as exc:
                if handler is None:
                    raise
                failed = exc
            # Consult the recovery seam OUTSIDE the device lock: an operator
            # pause can take hours; holding it would block the multi-site device.
            decision = await handler(failed, request.command, PauseSite.DEVICE_OP)
            if decision == RecoveryDecision.RETRY:
                reconcile_pending = True
                continue
            raise OperationDecisionSignal(decision, failed) from failed

    async def _save_again_until_saved(
        self,
        request: ActionRequest,
        answer: Any,
        not_saved: CommandNotSavedError,
        handler: DeviceOpRecoveryHandler,
        affected_labware: List[str],
        affected_labware_ids: List[str],
    ) -> Any:
        """Pause at COMMAND_SAVE until the answer the device gave is saved. RETRY saves it again; nothing is sent."""
        while True:
            decision = await handler(not_saved, request.command, PauseSite.COMMAND_SAVE)
            if decision != RecoveryDecision.RETRY:
                unsaved = UnsavedResult(
                    request.command, _as_json({"args": list(request.args), "kwargs": request.kwargs}), _as_json(answer),
                )
                raise OperationDecisionSignal(decision, not_saved, unsaved) from not_saved
            try:
                await self._save(request, answer, affected_labware, affected_labware_ids)
            except PartialChannelFailureError:
                raise
            except Exception as again:
                not_saved = CommandNotSavedError.of(self._device.name, request.command, again)
                continue
            return answer

    async def _save(
        self,
        request: ActionRequest,
        result: Any,
        affected_labware: List[str],
        affected_labware_ids: List[str],
    ) -> None:
        # Interpreters consume only structured driver state; other
        # returns (mock strings, None) narrow to None here, once.
        state = result if isinstance(result, LabwareStateResponse) else None
        await self._recorder.save_completed(
            request, state, affected_labware, affected_labware_ids,
        )

    def _params_as_sent(self, request: ActionRequest) -> dict[str, JsonValue] | None:
        """The request's params as the wire carries them, so a command refused before it is sent names them too.

        None when the interpreter has no request for the call, or the args do not read as one: the tip check then
        fails the call inside the pipeline, as any refusal does.
        """
        try:
            typed = self._recorder.interpreter.request_of(request.command, request.args, request.kwargs)
        except Exception:
            return None
        return None if typed is None else params_as_sent(typed)

    async def _check_tips_present(
        self, request: ActionRequest, instances: Sequence[LabwareInstance],
    ) -> None:
        """Refuse a tip pick the record reads empty, the check every tip pick-up passes.

        Runs every attempt, including a RETRY re-issue: an operator who loaded tips and corrected the record
        between attempts needs the retry to see that. A rack this action never had assigned claims nothing.
        """
        typed = self._recorder.interpreter.request_of(request.command, request.args, request.kwargs)
        by_name = {instance.name: instance for instance in instances}
        await refuse_a_pick_from_empty_spots(tips_claimed_by(typed, by_name.get), by_name.get)

    async def _run_mode_of_this_device(self) -> WorkflowRunMode | None:
        """A device under a sim override is simulated even inside a LIVE run."""
        if isinstance(self._device, IModeAware):
            return await self._device.effective_mode()
        return current_run_mode.get(None)

    async def _check_reagent_present(
        self, request: ActionRequest, instances: Sequence[LabwareInstance],
    ) -> None:
        """Stop an aspirate the record says a trough cannot supply. Runs every attempt, like the tip check."""
        by_name = {instance.name: instance for instance in instances}
        claimed = self._recorder.interpreter.claimed_reagent_volumes(
            request.command, request.args, request.kwargs,
        )
        for name, volume in claimed.items():
            labware = by_name.get(name)
            if labware is None:
                continue
            short = await labware.reagent_short_by(volume, await self._run_mode_of_this_device())
            if short > 0:
                raise ReagentRanOutError(labware.name, labware.id, short)

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

        Only a step RETRY of a device call is guarded; RETRY_ACTION re-runs an
        action whose own code failed, so it re-drives unreconciled and a dead
        session then fails its first call with the typed run-death error. A reconcile that ran is saved like any command: it can clear
        tips the driver was tracking.
        """
        device = self._device
        if not isinstance(device, IHardwareReconcilable):
            return
        reconcile = ActionRequest(
            device_name=self._device.name, command="reconcile_hardware_state", args=(), kwargs={},
        )
        try:
            report = await self._pipeline.run_device_command(
                self._device.name, reconcile.command,
                lambda: self._call_with_recoverable_timeout(
                    "reconcile_hardware_state", device.reconcile_hardware_state, (), {},
                ),
                lambda answer: self._save(reconcile, answer, [], []),
                checks=(), execution_id=self._execution_id, named=(), params=None,
            )
        except (OverrideWithPauseError, CommandTimeoutAbortedError, CommandNotSavedError, PartialChannelFailureError):
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
        """Run the device call under the run's recoverable timeout, at the command's time limit for this request."""
        limit = time_limit(
            self._device.command_max_seconds(command), self._device.command_typical_seconds(command),
            declared_duration(method, args, kwargs),
        )
        return await within_the_time_limit(self._device.name, command, limit, lambda: method(*args, **kwargs))

    def _raise_if_under_external_control(self) -> None:
        if self._device.under_external_control:
            raise DeviceUnderExternalControlError(device_name=self._device.name)

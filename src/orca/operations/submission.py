from orca.operations.submission_models import (
    GET_SUBMISSION_ROUTE,
    LIST_SUBMISSIONS_ROUTE,
    SUBMIT_EXECUTION_ROUTE,
    SUBMIT_METHOD_EXECUTION_ROUTE,
)
"""Submission Operations.

`SubmitExecutionOperation` owns the three-path submission dispatch.

`ListSubmissionsOperation` and `GetSubmissionOperation` serve the
`ISubmissionFacade` read methods.

The orchestration lives in `run()`: variable
coercion, run-mode + batch-mode parsing, LabwareGroup tuple
construction, one-shot-vs-multi-group routing, error mapping.

The Pydantic Request IS the wire shape -- binders
deserialize directly into the Request type, no per-surface
wire model in between.
"""

from orca.runtime.registries.device_registry import NOT_CONNECTED
from orca.system.errors import DevicesNotReadyError
from orca.operations.binding import RestBinding
import dataclasses
import logging
from functools import singledispatch
from typing import ClassVar, Mapping, Protocol, runtime_checkable
from pydantic import BaseModel
from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError, message_of
from orca.operations._spawn_incompatible import spawn_incompatible
from orca.operations._interface_mismatch import interface_mismatch
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.runtime_interface import ISystemRuntime
from orca.runtime.errors import (
    EventHooksNotInTheWorkflowError,
    NoLabwareCanOwnTheMethodError,
    OtherRunModeActiveError,
    DeploymentProfileNotFoundError,
    LabwareTemplateNotFoundError,
    MethodNotFoundError,
    RunModeMismatchError,
    RunModeRequiredError,
    SimSpeedupNotApplicableError,
    StartLocationsOccupiedError,
    SubmissionBlockedByOrphanedBacklogError,
    SubmissionToPausedExecutionError,
    TopologyCollisionError,
    UnknownLocationError,
)
from orca.workflow_models.errors import ReuseThreadCannotBeEntryError, SpawnIncompatibleError
from orca.operations.state_models import UnsettledSubjectDTO
from orca.runtime.status_models import SubmissionSnapshot
from orca.workflow_models.submission_modes import BatchMode
from orca.operations.submission_models import (
    GetSubmissionRequest,
    GetSubmissionResponse,
    ListSubmissionsRequest,
    ListSubmissionsResponse,
    MethodExecutionRequest,
    MethodExecutionResponse,
    SubmitExecutionRequest,
    SubmitExecutionResponse,
    _OccupiedSlotExtra,
)
from orca.operations.wire_models import AcquisitionDTO, LabwareGroupDTO, SubmissionDTO
from orca.runtime.execution_record import ExecutionRecord
from orca.workflow_models.labware_group import (
    AcquisitionValidationError,
    Acquisition,
    BarcodeAcquisition,
    LabwareGroup,
    LabwareGroupMember,
    LocationAcquisition,
    PoolAcquisition,
)
from orca.state.unsettled import UnsettledSubject
from orca.variables.errors import OptionValue, SubmissionInputsRefusedError, VariableValidationError


logger = logging.getLogger("orca")


class _SubmissionFacadeProtocol(Protocol):
    async def submit_group(
        self, *,
        workflow_name: str,
        groups: tuple[LabwareGroup, ...] = (),
        variables: dict[str, OptionValue] | None = None,
        batch_mode: BatchMode = BatchMode.STANDALONE,
        operator_id: str | None = None,
        deployment_profile: str | None = None,
        mode: WorkflowRunMode | None = None,
        sim_speedup: float | None = None,
        confirm: bool = False,
    ) -> SubmissionSnapshot: ...

    def list_submissions(
        self, *, execution_id: str | None = None,
    ) -> list[SubmissionSnapshot]: ...

    def get_submission(self, submission_id: str) -> SubmissionSnapshot: ...


@runtime_checkable
class _RuntimeWithSubmission(Protocol):
    """Narrow capability surface the operation actually uses.

    Same pattern as `system.GetSystemInfoOperation._RuntimeWithRegistry`:
    the Operation declares only what it touches.
    """

    @property
    def submissions(self) -> _SubmissionFacadeProtocol: ...

    async def unsettled_state(self) -> list[UnsettledSubject]: ...

    def get_execution(self, execution_id: str) -> ExecutionRecord: ...


class _SubmissionReadsProtocol(Protocol):
    def list_submissions(
        self, *, execution_id: str | None = None,
    ) -> list[SubmissionSnapshot]: ...

    def get_submission(self, submission_id: str) -> SubmissionSnapshot: ...


@runtime_checkable
class _RuntimeWithSubmissionReads(Protocol):
    """Narrow capability surface for ``ListSubmissions`` / ``GetSubmission``.

    A runtime satisfies it, and so do the check runs, whose submissions outlive their runtimes.
    """

    @property
    def submissions(self) -> _SubmissionReadsProtocol: ...


def _acquisition_from_dto(dto: AcquisitionDTO) -> Acquisition:
    """Map AcquisitionDTO → runtime Acquisition variant."""
    kind = dto.kind
    if kind == "pool":
        return PoolAcquisition()
    if kind == "barcode":
        if not dto.barcode:
            raise ValueError("barcode acquisition requires a 'barcode' field")
        return BarcodeAcquisition(barcode=dto.barcode)
    if kind == "location":
        if not dto.source_location:
            raise ValueError(
                "location acquisition requires a 'source_location' field",
            )
        if dto.verify_barcode is not None:
            raise ValueError(
                "verify_barcode is not supported: nothing reads a barcode when the labware is picked up, "
                "so it cannot be checked. To take a labware by its barcode, use kind 'barcode'.",
            )
        return LocationAcquisition(source_location=dto.source_location)
    raise ValueError(f"unknown acquisition kind: {kind!r}")


def _build_runtime_groups(
    groups: list[LabwareGroupDTO],
) -> tuple[LabwareGroup, ...]:
    """Convert wire groups → runtime LabwareGroup tuple."""
    return tuple(
        LabwareGroup(
            id=g.id,
            members=tuple(
                LabwareGroupMember(
                    thread_template_name=m.thread_template_name,
                    acquisition=_acquisition_from_dto(m.acquisition),
                )
                for m in g.members
            ),
            name=g.name,
        )
        for g in groups
    )



@singledispatch
def submit_refusal(exc: Exception) -> OperationError:
    """The error every submit path answers an engine refusal with. `SUBMIT_REFUSALS` lists what it maps."""
    raise TypeError(f"no submit refusal for {type(exc).__name__}") from exc


submit_refusal.register(TopologyCollisionError, interface_mismatch)


@submit_refusal.register
def _sim_speedup_not_applicable(
    exc: SimSpeedupNotApplicableError,
) -> OperationError:
    """A sim speed-up where none can apply: a LIVE run, or a joining submission.
    A shape refusal, 422, like the LIVE-override gate."""
    return OperationError.of(
        ErrorCode.SIM_SPEEDUP_NOT_APPLICABLE,
        str(exc),
        reason=exc.reason,
        execution_id=exc.execution_id,
    )


@submit_refusal.register
def _run_mode_mismatch(exc: RunModeMismatchError) -> OperationError:
    """JOIN_EXISTING submission's run_mode differs from the live execution's.

    Status 409 because the engine state (the existing execution's mode)
    is what blocks the submission, not the submission's shape. Carries
    the blocking execution + mode pair so callers can render an
    actionable error.
    """
    return OperationError.of(
        ErrorCode.RUN_MODE_MISMATCH,
        str(exc),
        blocking_execution_id=exc.blocking_execution_id,
        blocking_workflow_name=exc.blocking_workflow_name,
        existing_run_mode=exc.existing_run_mode.value,
        submitted_run_mode=exc.submitted_run_mode.value,
    )


@submit_refusal.register
def _submission_to_paused_execution(
    exc: SubmissionToPausedExecutionError,
) -> OperationError:
    """JOIN_EXISTING submission targeting a paused execution.

    Engine-state refusal -> 409 (the execution's pause latch is what
    blocks the join, not the submission's shape). Carries the blocking
    execution + workflow so callers can resume it or resubmit STANDALONE,
    mirroring the run-mode-mismatch precedent.
    """
    return OperationError.of(
        ErrorCode.SUBMISSION_TO_PAUSED_EXECUTION,
        str(exc),
        blocking_execution_id=exc.blocking_execution_id,
        blocking_workflow_name=exc.blocking_workflow_name,
    )


@submit_refusal.register
def _submission_blocked_by_orphaned_backlog(
    exc: SubmissionBlockedByOrphanedBacklogError,
) -> OperationError:
    """JOIN_EXISTING submission targeting an execution with a quarantined slot.

    Engine-state refusal -> 409. A batchable join would route straight into
    the quarantined backlog, which the accept-partial resume discards.
    Carries the blocking execution + workflow so callers can resume it
    first or resubmit STANDALONE.
    """
    return OperationError.of(
        ErrorCode.SUBMISSION_BLOCKED_BY_ORPHANED_BACKLOG,
        str(exc),
        blocking_execution_id=exc.blocking_execution_id,
        blocking_workflow_name=exc.blocking_workflow_name,
    )


@submit_refusal.register
def other_run_mode_active(exc: OtherRunModeActiveError) -> OperationError:
    """A system runs in one run mode at a time: 409, naming the running execution and both modes."""
    return OperationError.of(
        ErrorCode.OTHER_RUN_MODE_ACTIVE,
        str(exc),
        blocking_execution_id=exc.blocking_execution_id,
        blocking_workflow_name=exc.blocking_workflow_name,
        existing_run_mode=exc.existing_run_mode.value,
        submitted_run_mode=exc.submitted_run_mode.value,
    )


_START_THE_CLIENT = (
    ". Start the orca-client that runs them, on its computer: `python -m orca_client --config <its config file>`."
    " The orca-client README shows the config file."
)


@submit_refusal.register
def devices_not_ready(exc: DevicesNotReadyError) -> OperationError:
    """A run refused at its start: 409, naming each device that did not come up and why."""
    waits_on_a_client = any(why.startswith(NOT_CONNECTED) for why in exc.devices.values())
    return OperationError.of(
        ErrorCode.DEVICES_NOT_READY,
        str(exc) + (_START_THE_CLIENT if waits_on_a_client else ""),
        devices=dict(exc.devices),
    )


@submit_refusal.register
def _start_locations_occupied(
    exc: StartLocationsOccupiedError,
) -> OperationError:
    """The pre-submit start-location check found one or more occupied slots.

    Status 409 because operator action (clear the location) unblocks
    the submission. Extras list each occupied slot for an actionable UI.
    """
    return OperationError.of(
        ErrorCode.START_LOCATION_OCCUPIED,
        str(exc),
        occupied=[
            _OccupiedSlotExtra(
                position_id=slot.position_id,
                existing_labware_id=slot.existing_labware_id,
                existing_labware_name=slot.existing_labware_name,
                existing_template_name=slot.existing_template_name,
                source=slot.source,
            ).model_dump()
            for slot in exc.occupied
        ],
    )


@submit_refusal.register
def _reuse_thread_cannot_be_entry(
    exc: ReuseThreadCannotBeEntryError,
) -> OperationError:
    """A ``start_reuse_existing=True`` thread was registered as an entry.

    Build-time check that fires at submit when the system was authored
    against the wrong primitive. Status 409 because the submission is
    refused on engine-state grounds (workflow shape is incompatible
    with the submit path).
    """
    return OperationError.of(
        ErrorCode.REUSE_THREAD_CANNOT_BE_ENTRY,
        str(exc),
        thread_name=exc.thread_name,
    )



@submit_refusal.register
def _run_mode_required(exc: RunModeRequiredError) -> OperationError:
    return OperationError.of(ErrorCode.RUN_MODE_REQUIRED, str(exc))


@submit_refusal.register
def _spawn_incompatible(exc: SpawnIncompatibleError) -> OperationError:
    return spawn_incompatible(exc)


@submit_refusal.register
def _profile_not_found(exc: DeploymentProfileNotFoundError) -> OperationError:
    return OperationError.of(ErrorCode.PROFILE_NOT_FOUND, message_of(exc))


@submit_refusal.register
def _variables_refused(exc: VariableValidationError) -> OperationError:
    return OperationError.validation_error(str(exc))


@submit_refusal.register
def _inputs_refused(exc: SubmissionInputsRefusedError) -> OperationError:
    return OperationError.validation_error(str(exc))


@submit_refusal.register
def _acquisition_refused(exc: AcquisitionValidationError) -> OperationError:
    return OperationError.validation_error(str(exc))


@submit_refusal.register
def _event_hooks_refused(exc: EventHooksNotInTheWorkflowError) -> OperationError:
    return OperationError.validation_error(str(exc))


@submit_refusal.register
def _no_labware_owns_the_method(exc: NoLabwareCanOwnTheMethodError) -> OperationError:
    return OperationError.validation_error(str(exc))


@submit_refusal.register
def _method_not_found(exc: MethodNotFoundError) -> OperationError:
    return OperationError.of(ErrorCode.METHOD_NOT_FOUND, message_of(exc))


@submit_refusal.register
def _labware_template_not_found(exc: LabwareTemplateNotFoundError) -> OperationError:
    return OperationError.of(ErrorCode.LABWARE_TEMPLATE_NOT_FOUND, message_of(exc))


@submit_refusal.register
def _location_not_found(exc: UnknownLocationError) -> OperationError:
    return OperationError.of(ErrorCode.LOCATION_NOT_FOUND, message_of(exc), location=exc.location)


SUBMIT_REFUSALS: tuple[type[Exception], ...] = tuple(t for t in submit_refusal.registry if t is not object)

class SubmitExecutionOperation:
    """Submit a workflow through `submissions.submit_group`. No groups is one default group.

    An engine refusal maps through `submit_refusal`, `KeyError` to
    `workflow_not_found`, `ValueError` to `validation_error` and
    `RuntimeError` to `conflict`.
    """

    Request: ClassVar[type[BaseModel]] = SUBMIT_EXECUTION_ROUTE.request
    Response: ClassVar[type[BaseModel]] = SUBMIT_EXECUTION_ROUTE.response
    binding: ClassVar[RestBinding] = SUBMIT_EXECUTION_ROUTE.binding

    def __init__(self, runtime: _RuntimeWithSubmission):
        self._runtime = runtime

    async def _unsettled_now(self) -> list[UnsettledSubjectDTO]:
        """What a person still owes the system, read once per submission.

        Never raises into the submission: the run was accepted, and failing it
        here would refuse work over a report about work. A read that cannot be
        made is logged and the list comes back empty.
        """
        try:
            return [
                UnsettledSubjectDTO.from_subject(subject)
                for subject in await self._runtime.unsettled_state()
            ]
        except Exception:
            logger.warning(
                "could not read what is unsettled for this submission; the "
                "run was accepted and the list is reported empty",
                exc_info=True,
            )
            return []

    async def run(self, req: SubmitExecutionRequest) -> SubmitExecutionResponse:
        try:
            runtime_groups = _build_runtime_groups(req.groups or [])
        except ValueError as exc:
            raise OperationError.validation_error(str(exc)) from exc

        try:
            snap = await self._runtime.submissions.submit_group(
                workflow_name=req.workflow_name,
                groups=runtime_groups,
                variables=req.variables,
                batch_mode=BatchMode[req.batch_mode],
                operator_id=req.operator_id,
                deployment_profile=req.deployment_profile,
                mode=WorkflowRunMode[req.run_mode],
                sim_speedup=req.sim_speedup,
                confirm=True,
            )
        except SUBMIT_REFUSALS as exc:
            raise submit_refusal(exc) from exc
        except KeyError as exc:
            missing = exc.args[0] if exc.args else req.workflow_name
            raise OperationError.of(
                ErrorCode.WORKFLOW_NOT_FOUND,
                f"workflow or labware template not found: {missing!s}",
                workflow_name=req.workflow_name,
            ) from exc
        except ValueError as exc:
            raise OperationError.validation_error(str(exc)) from exc
        except RuntimeError as exc:
            raise OperationError.of(ErrorCode.CONFLICT, str(exc)) from exc

        return SubmitExecutionResponse.from_snapshot(
            snap, await self._unsettled_now(),
            self._runtime.get_execution(snap.execution_id).sim_speedup,
        )

# -- ListSubmissionsOperation -----------------------------------------------


def _submission_to_dto(snap: SubmissionSnapshot) -> SubmissionDTO:
    return SubmissionDTO.model_validate(dataclasses.asdict(snap))


class ListSubmissionsOperation:
    Request: ClassVar[type[BaseModel]] = LIST_SUBMISSIONS_ROUTE.request
    Response: ClassVar[type[BaseModel]] = LIST_SUBMISSIONS_ROUTE.response
    binding: ClassVar[RestBinding] = LIST_SUBMISSIONS_ROUTE.binding

    def __init__(self, runtime: _RuntimeWithSubmissionReads):
        self._runtime = runtime

    async def run(
        self, req: ListSubmissionsRequest,
    ) -> ListSubmissionsResponse:
        try:
            snaps = self._runtime.submissions.list_submissions(
                execution_id=req.execution_id,
            )
        except KeyError as exc:
            raise OperationError.of(
                ErrorCode.EXECUTION_NOT_FOUND,
                (
                    f"execution {req.execution_id!r} not found"
                    if req.execution_id is not None
                    else "execution not found"
                ),
                execution_id=req.execution_id,
            ) from exc
        return ListSubmissionsResponse(
            submissions=[_submission_to_dto(s) for s in snaps],
        )


# -- GetSubmissionOperation -------------------------------------------------


class GetSubmissionOperation:
    Request: ClassVar[type[BaseModel]] = GET_SUBMISSION_ROUTE.request
    Response: ClassVar[type[BaseModel]] = GET_SUBMISSION_ROUTE.response
    binding: ClassVar[RestBinding] = GET_SUBMISSION_ROUTE.binding

    def __init__(self, runtime: _RuntimeWithSubmissionReads):
        self._runtime = runtime

    async def run(
        self, req: GetSubmissionRequest,
    ) -> GetSubmissionResponse:
        try:
            snap = self._runtime.submissions.get_submission(req.submission_id)
        except KeyError as exc:
            raise OperationError.of(
                ErrorCode.SUBMISSION_NOT_FOUND,
                f"submission {req.submission_id!r} not found",
                submission_id=req.submission_id,
            ) from exc
        return GetSubmissionResponse(submission=_submission_to_dto(snap))


class SubmitMethodExecutionOperation:
    """Run one method standalone, as a synthesized one-method workflow."""

    Request: ClassVar[type[BaseModel]] = SUBMIT_METHOD_EXECUTION_ROUTE.request
    Response: ClassVar[type[BaseModel]] = SUBMIT_METHOD_EXECUTION_ROUTE.response
    binding: ClassVar[RestBinding] = SUBMIT_METHOD_EXECUTION_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime) -> None:
        self._runtime = runtime

    async def run(self, req: MethodExecutionRequest) -> MethodExecutionResponse:
        try:
            record = await self._runtime.submissions.submit_method(
                workflow_name=req.workflow_name,
                method_name=req.method_name,
                labware_start=req.labware_start,
                labware_end=req.labware_end,
                variables=req.variables,
                deployment_profile=req.deployment_profile,
                mode=WorkflowRunMode[req.run_mode],
                event_hooks=req.event_hooks,
                sim_speedup=req.sim_speedup,
                confirm=True,
            )
        except SUBMIT_REFUSALS as exc:
            raise submit_refusal(exc) from exc
        except KeyError as exc:
            raise OperationError.of(ErrorCode.WORKFLOW_NOT_FOUND, message_of(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise OperationError.validation_error(str(exc)) from exc
        except RuntimeError as exc:
            raise OperationError.of(ErrorCode.CONFLICT, str(exc)) from exc
        return MethodExecutionResponse(
            execution_id=record.id,
            workflow_name=record.workflow_name,
            status=record.status.value,
        )


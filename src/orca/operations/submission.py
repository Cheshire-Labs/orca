"""Submission Operations.

`SubmitExecutionOperation` owns the full 3-path submission
dispatch that previously lived duplicated across the orca-core daemon REST,
a hosted REST `POST /api/executions`, and a hosted MCP `submissions_submit`.

`ListSubmissionsOperation` + `GetSubmissionOperation`
surface the `ISubmissionFacade` read methods so MCP + REST + CLI
callers query through the unified `/operations/*` surface instead of
legacy `GET /submissions[/...]` URLs.

The orchestration lives in `run()`: variable
coercion, run-mode + batch-mode parsing, LabwareGroup tuple
construction, legacy-vs-multi-group routing, error mapping.

The Pydantic Request IS the wire shape — binders
deserialize directly into the Request type, no per-surface
wire model in between.
"""

import dataclasses
import logging
from typing import ClassVar, Protocol, runtime_checkable
from pydantic import BaseModel
from orca.operations._protocol import OperationError, OperationErrorCode
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.runtime_interface import (
    ConcurrentLiveSimRefusedError,
    LiveSubmissionWithSimOverridesUnacknowledgedError,
    ReuseThreadCannotBeEntryError,
    RunModeMismatchError,
    SpawnIncompatibleError,
    StartLocationsOccupiedError,
    SubmissionBlockedByOrphanedBacklogError,
    SubmissionToPausedExecutionError,
)
from orca.operations.state_models import UnsettledSubjectDTO
from orca.runtime.status_models import SubmissionSnapshot
from orca.runtime.submission_modes import BatchMode
from orca.operations.submission_models import (
    GetSubmissionRequest,
    GetSubmissionResponse,
    ListSubmissionsRequest,
    ListSubmissionsResponse,
    SubmitExecutionRequest,
    SubmitExecutionResponse,
    _LiveSimOverrideDeviceExtra,
    _OccupiedSlotExtra,
)
from orca.daemon.schemas import AcquisitionDTO, LabwareGroupDTO, SubmissionDTO
from orca.runtime.execution_record import ExecutionRecord
from orca.runtime.labware_group import (
    Acquisition,
    BarcodeAcquisition,
    LabwareGroup,
    LabwareGroupMember,
    LocationAcquisition,
    PoolAcquisition,
)
from orca.state.unsettled import UnsettledSubject
from orca.variables.errors import OptionValue


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
        acknowledge_warnings: bool = False,
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

    async def submit_workflow(
        self,
        workflow_name: str,
        variables: dict[str, OptionValue] | None = None,
        *,
        mode: WorkflowRunMode | None = None,
    ) -> ExecutionRecord: ...

    @property
    def submissions(self) -> _SubmissionFacadeProtocol: ...

    async def unsettled_state(self) -> list[UnsettledSubject]: ...


@runtime_checkable
class _RuntimeWithSubmissionReads(Protocol):
    """Narrow capability surface for ``ListSubmissions`` / ``GetSubmission``.

    Both the daemon (``SystemRuntime``) and a hosted deployment (``ISystemRuntime``)
    callers satisfy this structurally; declaring the parameter type at
    this width sidesteps the pyright variance error that fires when a
    concrete ``SystemRuntime`` is passed to an ``ISystemRuntime``-
    typed parameter, the same friction ``_RuntimeWithSubmission`` handles
    for ``SubmitExecutionOperation``.
    """

    @property
    def submissions(self) -> _SubmissionFacadeProtocol: ...


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
        return LocationAcquisition(
            source_location=dto.source_location,
            verify_barcode=dto.verify_barcode,
        )
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


def _raise_live_sim_overrides_unacknowledged(
    exc: LiveSubmissionWithSimOverridesUnacknowledgedError,
) -> OperationError:
    """LIVE submission referenced devices with topology ``sim_override`` and
    the operator did not pass ``acknowledge_warnings=True``.

    Wire shape: 422 with
    ``extras.devices[].{name, sim_override, resolved_mode}`` so a UI
    can render the offenders. Status is 422 (not 409) because the
    submission shape itself is the issue: the operator must add
    ``acknowledge_warnings=True`` to proceed.
    """
    return OperationError.typed(
        OperationErrorCode.INVALID_INPUT,
        str(exc),
        wire_code="LIVE_SUBMISSION_WITH_SIM_OVERRIDES_UNACKNOWLEDGED",
        status_code=422,
        devices=[
            _LiveSimOverrideDeviceExtra(
                name=name,
                sim_override=override.value,
                resolved_mode=resolved.value,
            ).model_dump()
            for name, override, resolved in exc.devices
        ],
    )


def _raise_run_mode_mismatch(exc: RunModeMismatchError) -> OperationError:
    """JOIN_EXISTING submission's run_mode differs from the live execution's.

    Status 409 because the engine state (the existing execution's mode)
    is what blocks the submission, not the submission's shape. Carries
    the blocking execution + mode pair so callers can render an
    actionable error.
    """
    return OperationError.typed(
        OperationErrorCode.CONFLICT,
        str(exc),
        wire_code="RUN_MODE_MISMATCH",
        status_code=409,
        blocking_execution_id=exc.blocking_execution_id,
        blocking_workflow_name=exc.blocking_workflow_name,
        existing_run_mode=exc.existing_run_mode.value,
        submitted_run_mode=exc.submitted_run_mode.value,
    )


def _raise_submission_to_paused_execution(
    exc: SubmissionToPausedExecutionError,
) -> OperationError:
    """JOIN_EXISTING submission targeting a paused execution.

    Engine-state refusal -> 409 (the execution's pause latch is what
    blocks the join, not the submission's shape). Carries the blocking
    execution + workflow so callers can resume it or resubmit STANDALONE,
    mirroring the run-mode-mismatch precedent.
    """
    return OperationError.typed(
        OperationErrorCode.CONFLICT,
        str(exc),
        wire_code="submission_to_paused_execution",
        status_code=409,
        blocking_execution_id=exc.blocking_execution_id,
        blocking_workflow_name=exc.blocking_workflow_name,
    )


def _raise_submission_blocked_by_orphaned_backlog(
    exc: SubmissionBlockedByOrphanedBacklogError,
) -> OperationError:
    """JOIN_EXISTING submission targeting an execution with a quarantined slot.

    Engine-state refusal -> 409. A batchable join would route straight into
    the quarantined backlog, which the accept-partial resume discards.
    Carries the blocking execution + workflow so callers can resume it
    first or resubmit STANDALONE.
    """
    return OperationError.typed(
        OperationErrorCode.CONFLICT,
        str(exc),
        wire_code="submission_blocked_by_orphaned_backlog",
        status_code=409,
        blocking_execution_id=exc.blocking_execution_id,
        blocking_workflow_name=exc.blocking_workflow_name,
    )


def _raise_concurrent_live_sim_refused(
    exc: ConcurrentLiveSimRefusedError,
) -> OperationError:
    """A live execution and a sim execution cannot run at the same time.

    Engine-state refusal -> 409. Carries the blocking execution + the two
    run modes so the operator knows which world is running and which run to
    wait on.
    """
    return OperationError.typed(
        OperationErrorCode.CONFLICT,
        str(exc),
        wire_code="CONCURRENT_LIVE_SIM_REFUSED",
        status_code=409,
        blocking_execution_id=exc.blocking_execution_id,
        blocking_workflow_name=exc.blocking_workflow_name,
        existing_run_mode=exc.existing_run_mode.value,
        submitted_run_mode=exc.submitted_run_mode.value,
    )


def _raise_start_locations_occupied(
    exc: StartLocationsOccupiedError,
) -> OperationError:
    """The pre-submit start-location check found one or more occupied slots.

    Status 409 because operator action (clear the location) unblocks
    the submission. Extras list each occupied slot for an actionable UI.
    """
    return OperationError.typed(
        OperationErrorCode.CONFLICT,
        str(exc),
        wire_code="start_location_occupied",
        status_code=409,
        occupied=[
            _OccupiedSlotExtra(
                position_id=slot.position_id,
                existing_labware_name=slot.existing_labware_name,
                existing_template_name=slot.existing_template_name,
                source=slot.source,
            ).model_dump()
            for slot in exc.occupied
        ],
    )


def _raise_spawn_incompatible(exc: SpawnIncompatibleError) -> OperationError:
    """A spawn-action found labware of the wrong template at the start slot.

    Status 409: operator pre-loaded the wrong labware (or the workflow
    is mis-specified). Extras pin the location plus expected vs actual
    template names so the operator can correct it.
    """
    return OperationError.typed(
        OperationErrorCode.CONFLICT,
        str(exc),
        wire_code="spawn_incompatible",
        status_code=409,
        location=exc.location,
        expected_template=exc.expected_template,
        actual_template=exc.actual_template,
    )


def _raise_reuse_thread_cannot_be_entry(
    exc: ReuseThreadCannotBeEntryError,
) -> OperationError:
    """A ``start_reuse_existing=True`` thread was registered as an entry.

    Build-time check that fires at submit when the system was authored
    against the wrong primitive. Status 409 because the submission is
    refused on engine-state grounds (workflow shape is incompatible
    with the submit path).
    """
    return OperationError.typed(
        OperationErrorCode.CONFLICT,
        str(exc),
        wire_code="reuse_thread_cannot_be_entry",
        status_code=409,
        thread_name=exc.thread_name,
    )


def _is_legacy_shape(req: SubmitExecutionRequest) -> bool:
    """Detect the legacy single-workflow path.

    Legacy = no groups + STANDALONE + no operator/profile metadata + no
    ``acknowledge_warnings`` request. Anything else routes through
    ``submissions.submit_group`` (where ``acknowledge_warnings`` is on
    the ``ISystemRuntime`` interface). The interface-level
    ``submit_workflow`` does not surface the kwarg, so we re-route the
    LIVE-override-acknowledgement flow through the multi-group path.
    """
    return (
        req.groups is None
        and req.batch_mode == "STANDALONE"
        and req.operator_id is None
        and req.deployment_profile is None
        and req.acknowledge_warnings is False
    )


class SubmitExecutionOperation:
    """The worst-case spike Operation.

    Eight orchestration steps in `run()`:
    1. Parse `run_mode` (optional `WorkflowRunMode` override).
    2. Validate `batch_mode` (Literal already constrains; map to enum).
    3. Decide legacy vs multi-group dispatch.
    4. Legacy: call `runtime.submit_workflow`, then fetch the auto-
       created submission via `runtime.submissions.list_submissions`
       so the wire response shape matches multi-group exactly.
    5. Multi-group: build `LabwareGroup` tuples from the DTOs.
    6. Multi-group: call `runtime.submissions.submit_group`.
    7. Map `KeyError → not_found`, `ValueError → invalid_input`,
       `RuntimeError → conflict`.
    8. Convert `SubmissionSnapshot` → `SubmitExecutionResponse`.

    Variable coercion (the Operation only accepts OptionValue) lives in
    the Pydantic schema, not in `run()` — Pydantic rejects non-
    primitive values at deserialization with a 422 from the binder.
    """

    Request: ClassVar[type[BaseModel]] = SubmitExecutionRequest
    Response: ClassVar[type[BaseModel]] = SubmitExecutionResponse

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

    async def run(
        self, req: SubmitExecutionRequest,
    ) -> SubmitExecutionResponse:
        run_mode = (
            WorkflowRunMode[req.run_mode] if req.run_mode is not None else None
        )

        if _is_legacy_shape(req):
            return await self._run_legacy(req, run_mode)

        return await self._run_multi_group(req, run_mode)

    async def _run_legacy(
        self,
        req: SubmitExecutionRequest,
        run_mode: WorkflowRunMode | None,
    ) -> SubmitExecutionResponse:
        try:
            record = await self._runtime.submit_workflow(
                req.workflow_name,
                req.variables or {},
                mode=run_mode,
            )
        except LiveSubmissionWithSimOverridesUnacknowledgedError as exc:
            raise _raise_live_sim_overrides_unacknowledged(exc) from exc
        except StartLocationsOccupiedError as exc:
            raise _raise_start_locations_occupied(exc) from exc
        except SpawnIncompatibleError as exc:
            raise _raise_spawn_incompatible(exc) from exc
        except ReuseThreadCannotBeEntryError as exc:
            raise _raise_reuse_thread_cannot_be_entry(exc) from exc
        except RunModeMismatchError as exc:
            raise _raise_run_mode_mismatch(exc) from exc
        except ConcurrentLiveSimRefusedError as exc:
            raise _raise_concurrent_live_sim_refused(exc) from exc
        except SubmissionToPausedExecutionError as exc:
            raise _raise_submission_to_paused_execution(exc) from exc
        except SubmissionBlockedByOrphanedBacklogError as exc:
            raise _raise_submission_blocked_by_orphaned_backlog(exc) from exc
        except KeyError as exc:
            raise OperationError.not_found(
                f"workflow not found: {req.workflow_name}",
                workflow_name=req.workflow_name,
            ) from exc
        except RuntimeError as exc:
            raise OperationError.conflict(str(exc)) from exc

        snap = self._fetch_submission_for_execution(record.id)
        return SubmitExecutionResponse.from_snapshot(
            snap, await self._unsettled_now(),
        )

    async def _run_multi_group(
        self,
        req: SubmitExecutionRequest,
        run_mode: WorkflowRunMode | None,
    ) -> SubmitExecutionResponse:
        try:
            runtime_groups = _build_runtime_groups(req.groups or [])
        except ValueError as exc:
            raise OperationError.invalid_input(str(exc)) from exc

        try:
            snap = await self._runtime.submissions.submit_group(
                workflow_name=req.workflow_name,
                groups=runtime_groups,
                variables=req.variables,
                batch_mode=BatchMode[req.batch_mode],
                operator_id=req.operator_id,
                deployment_profile=req.deployment_profile,
                mode=run_mode,
                acknowledge_warnings=req.acknowledge_warnings,
                confirm=True,
            )
        except LiveSubmissionWithSimOverridesUnacknowledgedError as exc:
            raise _raise_live_sim_overrides_unacknowledged(exc) from exc
        except StartLocationsOccupiedError as exc:
            raise _raise_start_locations_occupied(exc) from exc
        except SpawnIncompatibleError as exc:
            raise _raise_spawn_incompatible(exc) from exc
        except ReuseThreadCannotBeEntryError as exc:
            raise _raise_reuse_thread_cannot_be_entry(exc) from exc
        except RunModeMismatchError as exc:
            raise _raise_run_mode_mismatch(exc) from exc
        except ConcurrentLiveSimRefusedError as exc:
            raise _raise_concurrent_live_sim_refused(exc) from exc
        except SubmissionToPausedExecutionError as exc:
            raise _raise_submission_to_paused_execution(exc) from exc
        except SubmissionBlockedByOrphanedBacklogError as exc:
            raise _raise_submission_blocked_by_orphaned_backlog(exc) from exc
        except KeyError as exc:
            missing = exc.args[0] if exc.args else req.workflow_name
            raise OperationError.not_found(
                f"workflow or labware template not found: {missing!s}",
                workflow_name=req.workflow_name,
            ) from exc
        except ValueError as exc:
            raise OperationError.invalid_input(str(exc)) from exc
        except RuntimeError as exc:
            raise OperationError.conflict(str(exc)) from exc

        return SubmitExecutionResponse.from_snapshot(
            snap, await self._unsettled_now(),
        )

    def _fetch_submission_for_execution(
        self, execution_id: str,
    ) -> SubmissionSnapshot:
        """Look up the submission `submit_workflow` auto-creates.

        The legacy path returns an `ExecutionRecord`, not a
        `SubmissionSnapshot`. Surfacing the snapshot keeps the wire
        response uniform with the multi-group path so MCP / REST
        callers see one shape regardless of dispatch.
        """
        try:
            snaps = self._runtime.submissions.list_submissions(
                execution_id=execution_id,
            )
        except KeyError as exc:
            raise OperationError.conflict(
                f"execution {execution_id} has no submission row",
                execution_id=execution_id,
            ) from exc
        if not snaps:
            raise OperationError.conflict(
                f"execution {execution_id} has no submission row",
                execution_id=execution_id,
            )
        return snaps[0]


# -- ListSubmissionsOperation -----------------------------------------------


def _submission_to_dto(snap: SubmissionSnapshot) -> SubmissionDTO:
    return SubmissionDTO.model_validate(dataclasses.asdict(snap))


class ListSubmissionsOperation:
    Request: ClassVar[type[BaseModel]] = ListSubmissionsRequest
    Response: ClassVar[type[BaseModel]] = ListSubmissionsResponse

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
            raise OperationError.not_found(
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
    Request: ClassVar[type[BaseModel]] = GetSubmissionRequest
    Response: ClassVar[type[BaseModel]] = GetSubmissionResponse

    def __init__(self, runtime: _RuntimeWithSubmissionReads):
        self._runtime = runtime

    async def run(
        self, req: GetSubmissionRequest,
    ) -> GetSubmissionResponse:
        try:
            snap = self._runtime.submissions.get_submission(req.submission_id)
        except KeyError as exc:
            raise OperationError.not_found(
                f"submission {req.submission_id!r} not found",
                submission_id=req.submission_id,
            ) from exc
        return GetSubmissionResponse(submission=_submission_to_dto(snap))

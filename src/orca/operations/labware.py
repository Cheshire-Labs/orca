"""Labware Operations.

Covers the 7 methods on `ILabwareFacade`:
- GetLabwareById / GetLabwareByBarcode
- ListLabware
- GetLabwareHistory
- EditLabwareLocation / EditLabwareBarcode / ResetLabwareLocation
- RegisterLabware

Single-target Operations; no Scope dispatch needed (labware identity
is a primary key, not a position in a runtime tree).
"""

from typing import ClassVar
from cheshire_drivers.move_parameters import MoveParameterPatch
from pydantic import BaseModel
from orca.operations._protocol import OperationError, OperationErrorCode, message_of
from orca.resource_models.device_error import SlotOccupiedError
from orca.state.records import TrackingSource
from orca.state.ops_store import OpsHistorySearchQuery
from orca.runtime.move_parameters import MoveParameterEditRefused
from orca.runtime.runtime_interface import (
    ClearSubmissionResult,
    ISystemRuntime,
    ActiveExecutionRefusedError,
    LabwareNotFoundError,
    LocationReservedError,
    MoverHoldsNothingError,
)
from orca.operations.labware_models import (
    CarryOverrideResponse,
    ClearAllLabwareRequest,
    ClearAllLabwareResponse,
    ClearLabwareCarryOverrideRequest,
    ClearSubmissionLabwareRequest,
    ClearSubmissionLabwareResponse,
    ConfirmTipStateRequest,
    ConfirmWellVolumesRequest,
    ContentsLayerModel,
    DischargeLabwareRequest,
    DischargeLabwareResponse,
    EditLabwareBarcodeRequest,
    EditLabwareLocationRequest,
    GetLabwareByBarcodeRequest,
    GetLabwareByIdRequest,
    GetLabwareHistoryRequest,
    GetLabwareHistoryResponse,
    GetLabwareJourneyRequest,
    GetLabwareJourneyResponse,
    GetLabwareResponse,
    GetTipStateRequest,
    GetTipStateResponse,
    GetWellVolumesRequest,
    GetWellVolumesResponse,
    JourneyAction,
    JourneyMove,
    LabwareSnapshotModel,
    ListLabwareRequest,
    ListLabwareResponse,
    LocationEventModel,
    MarkTipsUsedRequest,
    MarkTipsUsedResponse,
    RegisterLabwareRequest,
    RegisterLabwareResponse,
    ReleaseMoverHoldRequest,
    ReleaseMoverHoldResponse,
    ResetLabwareLocationRequest,
    ResolveContentsRequest,
    ResolveContentsResponse,
    SetLabwareCarryOverrideRequest,
    SetTipStateRequest,
    SetWellVolumesRequest,
    _MutationResponse,
)


def _location_reserved(exc: LocationReservedError) -> OperationError:
    """Placing into a position another thread has already claimed.

    409 alongside ``slot_occupied`` because both are "something else is going
    to be there"; this one is a claim rather than a plate.

    The two kinds of claim need different things from the operator, so both
    travel in extras rather than only in the message. ``inbound_labware`` names
    the labware the claim is for and ``awaiting_operator`` says whether it
    exists yet. False means it is on its way, ``inbound_from`` says where from,
    and the position frees when it lands and the operator takes it off. True
    means a person is being asked to place it, so making that placement is what
    frees the position. Read the flag before the name.
    """
    return OperationError.typed(
        OperationErrorCode.CONFLICT,
        str(exc),
        wire_code="location_reserved",
        status_code=409,
        position_id=exc.position_id,
        reservation_id=exc.reservation_id,
        holder_thread_id=exc.holder_thread_id,
        holder_thread_name=exc.holder_thread_name,
        inbound_labware=exc.inbound_labware,
        inbound_from=exc.inbound_from,
        awaiting_operator=exc.awaiting_operator,
    )


def _slot_occupied(exc: SlotOccupiedError) -> OperationError:
    """Placing into a slot that already holds something else.

    409 because the operator unblocks it by clearing the named slot. The
    occupant travels in extras so a surface can say what is in the way
    rather than only that the placement was refused.
    """
    return OperationError.typed(
        OperationErrorCode.CONFLICT,
        str(exc),
        wire_code="slot_occupied",
        status_code=409,
        position_id=exc.position_id,
        existing_labware_name=exc.existing_labware_name,
        existing_template_name=exc.existing_template_name,
    )


# -- Wire models -------------------------------------------------------------


class GetLabwareByIdOperation:
    Request: ClassVar[type[BaseModel]] = GetLabwareByIdRequest
    Response: ClassVar[type[BaseModel]] = GetLabwareResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: GetLabwareByIdRequest) -> GetLabwareResponse:
        try:
            snap = await self._runtime.labware.get_by_id(req.labware_id)
        except KeyError as exc:
            raise OperationError.not_found(
                f"labware id {req.labware_id!r} not found",
                labware_id=req.labware_id,
            ) from exc
        return GetLabwareResponse(labware=LabwareSnapshotModel.from_dc(snap))


class GetLabwareByBarcodeOperation:
    Request: ClassVar[type[BaseModel]] = GetLabwareByBarcodeRequest
    Response: ClassVar[type[BaseModel]] = GetLabwareResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: GetLabwareByBarcodeRequest) -> GetLabwareResponse:
        try:
            snap = await self._runtime.labware.get_by_barcode(req.barcode)
        except KeyError as exc:
            raise OperationError.not_found(
                f"labware with barcode {req.barcode!r} not found",
                barcode=req.barcode,
            ) from exc
        return GetLabwareResponse(labware=LabwareSnapshotModel.from_dc(snap))


# -- ListLabware -------------------------------------------------------------


class ListLabwareOperation:
    Request: ClassVar[type[BaseModel]] = ListLabwareRequest
    Response: ClassVar[type[BaseModel]] = ListLabwareResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: ListLabwareRequest) -> ListLabwareResponse:
        del req
        snaps = await self._runtime.labware.list_all()
        return ListLabwareResponse(
            labware=[LabwareSnapshotModel.from_dc(s) for s in snaps],
        )


# -- GetLabwareHistory -------------------------------------------------------


class GetLabwareHistoryOperation:
    Request: ClassVar[type[BaseModel]] = GetLabwareHistoryRequest
    Response: ClassVar[type[BaseModel]] = GetLabwareHistoryResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: GetLabwareHistoryRequest) -> GetLabwareHistoryResponse:
        try:
            history = await self._runtime.labware.get_history(req.labware_id)
        except KeyError as exc:
            raise OperationError.not_found(
                f"labware id {req.labware_id!r} not found",
                labware_id=req.labware_id,
            ) from exc
        return GetLabwareHistoryResponse(
            labware_id=req.labware_id,
            history=[LocationEventModel.from_dc(e) for e in history],
        )


# -- EditLabwareLocation ----------------------------------------------------


class EditLabwareLocationOperation:
    Request: ClassVar[type[BaseModel]] = EditLabwareLocationRequest
    Response: ClassVar[type[BaseModel]] = _MutationResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: EditLabwareLocationRequest) -> _MutationResponse:
        try:
            await self._runtime.labware.edit_location(
                req.labware_id, req.location,
                reason=req.reason, confirm=True,
            )
        except LocationReservedError as exc:
            raise _location_reserved(exc) from exc
        except SlotOccupiedError as exc:
            raise _slot_occupied(exc) from exc
        except KeyError as exc:
            raise OperationError.not_found(
                f"labware or location not found",
                labware_id=req.labware_id, location=req.location,
            ) from exc
        except ValueError as exc:
            raise OperationError.invalid_input(str(exc)) from exc
        except RuntimeError as exc:
            raise OperationError.conflict(str(exc)) from exc
        return _MutationResponse(labware_id=req.labware_id)


# -- EditLabwareBarcode -----------------------------------------------------


class EditLabwareBarcodeOperation:
    Request: ClassVar[type[BaseModel]] = EditLabwareBarcodeRequest
    Response: ClassVar[type[BaseModel]] = _MutationResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: EditLabwareBarcodeRequest) -> _MutationResponse:
        try:
            await self._runtime.labware.edit_barcode(
                req.labware_id, req.new_barcode,
                confirm=True,
            )
        except KeyError as exc:
            raise OperationError.not_found(
                f"labware id {req.labware_id!r} not found",
                labware_id=req.labware_id,
            ) from exc
        except ValueError as exc:
            raise OperationError.invalid_input(str(exc)) from exc
        return _MutationResponse(labware_id=req.labware_id)


# -- Carry override (layer four) --------------------------------------------


class SetLabwareCarryOverrideOperation:
    Request: ClassVar[type[BaseModel]] = SetLabwareCarryOverrideRequest
    Response: ClassVar[type[BaseModel]] = CarryOverrideResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(
        self, req: SetLabwareCarryOverrideRequest,
    ) -> CarryOverrideResponse:
        named = req.set.model_dump(exclude_none=True)
        if not named and not req.clear:
            raise OperationError.invalid_input(
                "name at least one field to set, or one to clear",
            )
        try:
            patch = await self._runtime.labware.set_carry_override(
                req.labware_id, req.set, req.clear, confirm=True,
            )
        except KeyError as exc:
            raise OperationError.not_found(
                f"labware id {req.labware_id!r} not found",
                labware_id=req.labware_id,
            ) from exc
        except MoveParameterEditRefused as exc:
            # The facade owns the wording; what the wire adds is the field
            # names, so a caller fixes the request in one go.
            raise OperationError.invalid_input(
                str(exc), fields=list(exc.fields),
            ) from exc
        return CarryOverrideResponse(
            labware_id=req.labware_id, carry_override=patch,
        )


class ClearLabwareCarryOverrideOperation:
    Request: ClassVar[type[BaseModel]] = ClearLabwareCarryOverrideRequest
    Response: ClassVar[type[BaseModel]] = CarryOverrideResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(
        self, req: ClearLabwareCarryOverrideRequest,
    ) -> CarryOverrideResponse:
        try:
            await self._runtime.labware.clear_carry_override(
                req.labware_id, confirm=True,
            )
        except KeyError as exc:
            raise OperationError.not_found(
                f"labware id {req.labware_id!r} not found",
                labware_id=req.labware_id,
            ) from exc
        return CarryOverrideResponse(
            labware_id=req.labware_id, carry_override=MoveParameterPatch(),
        )


# -- ResetLabwareLocation ---------------------------------------------------


class ResetLabwareLocationOperation:
    Request: ClassVar[type[BaseModel]] = ResetLabwareLocationRequest
    Response: ClassVar[type[BaseModel]] = _MutationResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: ResetLabwareLocationRequest) -> _MutationResponse:
        try:
            await self._runtime.labware.reset_location(
                req.labware_id, req.location,
                reason=req.reason, confirm=True,
            )
        except LocationReservedError as exc:
            raise _location_reserved(exc) from exc
        except SlotOccupiedError as exc:
            raise _slot_occupied(exc) from exc
        except KeyError as exc:
            raise OperationError.not_found(
                f"labware or location not found",
                labware_id=req.labware_id, location=req.location,
            ) from exc
        except ValueError as exc:
            raise OperationError.invalid_input(str(exc)) from exc
        return _MutationResponse(labware_id=req.labware_id)


# -- RegisterLabware --------------------------------------------------------


class RegisterLabwareOperation:
    Request: ClassVar[type[BaseModel]] = RegisterLabwareRequest
    Response: ClassVar[type[BaseModel]] = RegisterLabwareResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: RegisterLabwareRequest) -> RegisterLabwareResponse:
        try:
            snap = await self._runtime.labware.register(
                req.template_name, labware_type=req.labware_type,
                barcode=req.barcode, location=req.location,
                confirm=True,
            )
        except LocationReservedError as exc:
            raise _location_reserved(exc) from exc
        except SlotOccupiedError as exc:
            raise _slot_occupied(exc) from exc
        except KeyError as exc:
            missing = (
                f"template {req.template_name!r}" if req.template_name is not None
                else f"catalog labware type {req.labware_type!r}"
            )
            raise OperationError.not_found(
                f"{missing} not found",
                template_name=req.template_name, labware_type=req.labware_type,
            ) from exc
        except ValueError as exc:
            raise OperationError.invalid_input(str(exc)) from exc
        return RegisterLabwareResponse(
            labware=LabwareSnapshotModel.from_dc(snap),
        )


# -- Get/SetWellVolumes (operator volume CRUD) ------------------------------


class GetWellVolumesOperation:
    Request: ClassVar[type[BaseModel]] = GetWellVolumesRequest
    Response: ClassVar[type[BaseModel]] = GetWellVolumesResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: GetWellVolumesRequest) -> GetWellVolumesResponse:
        try:
            vols = await self._runtime.labware.get_well_volumes(req.labware_id)
        except KeyError as exc:
            raise OperationError.not_found(
                f"labware id {req.labware_id!r} not found",
                labware_id=req.labware_id,
            ) from exc
        return GetWellVolumesResponse(
            labware_id=req.labware_id,
            well_volumes=vols.volumes,
            provenance=vols.provenance,
        )


class SetWellVolumesOperation:
    Request: ClassVar[type[BaseModel]] = SetWellVolumesRequest
    Response: ClassVar[type[BaseModel]] = _MutationResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: SetWellVolumesRequest) -> _MutationResponse:
        try:
            await self._runtime.labware.set_well_volumes(
                req.labware_id, dict(req.well_volumes),
                reason=req.reason, confirm=True,
            )
        except KeyError as exc:
            raise OperationError.not_found(
                f"labware id {req.labware_id!r} not found",
                labware_id=req.labware_id,
            ) from exc
        except ValueError as exc:
            raise OperationError.invalid_input(str(exc)) from exc
        return _MutationResponse(labware_id=req.labware_id)


# -- ResolveContents (the canonical "what does it hold" read) ---------------


class ResolveContentsOperation:
    Request: ClassVar[type[BaseModel]] = ResolveContentsRequest
    Response: ClassVar[type[BaseModel]] = ResolveContentsResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: ResolveContentsRequest) -> ResolveContentsResponse:
        try:
            resolved = await self._runtime.labware.resolve_contents(req.labware_id)
        except KeyError as exc:
            raise OperationError.not_found(
                f"labware id {req.labware_id!r} not found",
                labware_id=req.labware_id,
            ) from exc
        return ResolveContentsResponse(
            labware_id=resolved.labware_id,
            labware_name=resolved.labware_name,
            provenance=resolved.provenance,
            source=resolved.source,
            tip_positions_present=resolved.tip_positions_present,
            tip_count=resolved.tip_count,
            volumes=resolved.volumes,
            layers=[
                ContentsLayerModel(
                    layer=layer.layer, tip_count=layer.tip_count,
                    agrees=layer.agrees, note=layer.note,
                )
                for layer in resolved.layers
            ],
        )


# -- MarkTipsUsed (advance past positions that are actually empty) ----------


class MarkTipsUsedOperation:
    Request: ClassVar[type[BaseModel]] = MarkTipsUsedRequest
    Response: ClassVar[type[BaseModel]] = MarkTipsUsedResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: MarkTipsUsedRequest) -> MarkTipsUsedResponse:
        try:
            remaining = await self._runtime.labware.mark_tips_used(
                req.labware_id, req.positions, reason=req.reason, confirm=True,
            )
        except KeyError as exc:
            raise OperationError.not_found(
                f"labware id {req.labware_id!r} not found",
                labware_id=req.labware_id,
            ) from exc
        except (ValueError, RuntimeError) as exc:
            raise OperationError.invalid_input(str(exc)) from exc
        return MarkTipsUsedResponse(
            labware_id=req.labware_id, tip_positions_present=remaining,
        )


# -- Get/Set/ConfirmTipState (operator tip CRUD) ----------------------------


class GetTipStateOperation:
    Request: ClassVar[type[BaseModel]] = GetTipStateRequest
    Response: ClassVar[type[BaseModel]] = GetTipStateResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: GetTipStateRequest) -> GetTipStateResponse:
        try:
            state = await self._runtime.labware.get_tip_state(req.labware_id)
        except KeyError as exc:
            raise OperationError.not_found(
                f"labware id {req.labware_id!r} not found",
                labware_id=req.labware_id,
            ) from exc
        except ValueError as exc:
            raise OperationError.invalid_input(str(exc)) from exc
        return GetTipStateResponse(
            labware_id=req.labware_id,
            tip_positions_present=state.positions_present,
            provenance=state.provenance,
        )


class SetTipStateOperation:
    Request: ClassVar[type[BaseModel]] = SetTipStateRequest
    Response: ClassVar[type[BaseModel]] = _MutationResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: SetTipStateRequest) -> _MutationResponse:
        try:
            await self._runtime.labware.set_tip_state(
                req.labware_id, list(req.tip_positions_present),
                reason=req.reason, confirm=True,
            )
        except KeyError as exc:
            raise OperationError.not_found(
                f"labware id {req.labware_id!r} not found",
                labware_id=req.labware_id,
            ) from exc
        except ValueError as exc:
            raise OperationError.invalid_input(str(exc)) from exc
        return _MutationResponse(labware_id=req.labware_id)


class ConfirmWellVolumesOperation:
    Request: ClassVar[type[BaseModel]] = ConfirmWellVolumesRequest
    Response: ClassVar[type[BaseModel]] = _MutationResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: ConfirmWellVolumesRequest) -> _MutationResponse:
        try:
            await self._runtime.labware.confirm_well_volumes(
                req.labware_id, reason=req.reason, confirm=True,
            )
        except KeyError as exc:
            raise OperationError.not_found(
                f"labware id {req.labware_id!r} not found",
                labware_id=req.labware_id,
            ) from exc
        except ValueError as exc:
            raise OperationError.invalid_input(str(exc)) from exc
        return _MutationResponse(labware_id=req.labware_id)


class ConfirmTipStateOperation:
    Request: ClassVar[type[BaseModel]] = ConfirmTipStateRequest
    Response: ClassVar[type[BaseModel]] = _MutationResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: ConfirmTipStateRequest) -> _MutationResponse:
        try:
            await self._runtime.labware.confirm_tip_state(
                req.labware_id, reason=req.reason, confirm=True,
            )
        except KeyError as exc:
            raise OperationError.not_found(
                f"labware id {req.labware_id!r} not found",
                labware_id=req.labware_id,
            ) from exc
        except ValueError as exc:
            raise OperationError.invalid_input(str(exc)) from exc
        return _MutationResponse(labware_id=req.labware_id)


# -- GetLabwareJourneyOperation ---------------------------------------------
#
# Returns a chronologically-merged moves + actions journey for one
# labware -- the unified server-side join that replaces the legacy
# two-call client-side join (get_history + ops_history.search).


_VALID_JOURNEY_KINDS: frozenset[str] = frozenset({"move", "action"})


class GetLabwareJourneyOperation:
    Request: ClassVar[type[BaseModel]] = GetLabwareJourneyRequest
    Response: ClassVar[type[BaseModel]] = GetLabwareJourneyResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(
        self, req: GetLabwareJourneyRequest,
    ) -> GetLabwareJourneyResponse:
        include_moves = req.kinds is None or "move" in req.kinds
        include_actions = req.kinds is None or "action" in req.kinds

        try:
            snap = await self._runtime.labware.get_by_id(req.labware_id)
        except KeyError as exc:
            raise OperationError.not_found(
                f"labware {req.labware_id!r} not found",
                labware_id=req.labware_id,
            ) from exc

        # ops_history records carry labware *name* on
        # ``OperationRecord.affected_labware`` -- orca-core's
        # location-action observers populate the list from
        # ``LabwareInstance.name``, not ``.id``. The request param is the
        # labware id (UUID), so the search filter and the per-op
        # affected-labware match must both speak NAME, not id.
        labware_name = snap.name

        moves = (
            await self._runtime.labware.get_history(req.labware_id)
            if include_moves else []
        )

        action_hits = []
        if include_actions:
            query = OpsHistorySearchQuery(labware_name=labware_name)
            try:
                hits = await self._runtime.ops_history.search(query)
            except Exception as exc:
                raise OperationError(
                    code=OperationErrorCode.INTERNAL_ERROR,
                    message=f"ops_history.search failed: {exc!r}",
                ) from exc
            for eid, record in hits:
                for op in record.operations:
                    if labware_name not in op.affected_labware:
                        continue
                    if (
                        not req.include_driver_snapshots
                        and op.source is TrackingSource.DRIVER_OBSERVED
                    ):
                        continue
                    action_hits.append((eid, record, op))

        # Merge by ``(timestamp, kind_priority)`` so a move at t=N
        # appears before an action at t=N (priority 0 < 1) -- physical
        # reality says arrival precedes operation.
        timestamped: list[tuple[float, int, JourneyMove | JourneyAction]] = []
        for move in moves:
            timestamped.append((
                move.timestamp, 0,
                JourneyMove(
                    sequence=move.sequence,
                    position_id=move.position_id,
                    timestamp=move.timestamp,
                ),
            ))
        for eid, record, op in action_hits:
            timestamped.append((op.timestamp, 1, JourneyAction(
                source=op.source.value,
                timestamp=op.timestamp,
                device_name=op.device_name,
                operation=op.operation.value,
                details=op.details,
                execution_id=eid,
                thread_id=op.thread_id,
                action_id=op.action_id,
                method_id=record.method_id,
            )))
        timestamped.sort(key=lambda item: (item[0], item[1]))
        entries = [item[2] for item in timestamped]
        return GetLabwareJourneyResponse(
            labware_id=req.labware_id, entries=entries,
        )


# -- Operator clear Operations -------------------------------------------
#
# Silent-stall recovery added three operator-facing clear surfaces to
# ``ILabwareFacade``. The legacy ``/api/labware/runtime/*`` namespace is gone;
# they are served as ``operations_clear_submission_labware`` /
# ``operations_discharge_labware`` / ``operations_clear_all_labware``.
#
# All three Operations refuse when an active execution would conflict (raised
# as ``ActiveExecutionRefusedError`` -> 409 ``conflict`` with extras naming
# the offender). Operator passes ``force=True`` to override.


class ClearSubmissionLabwareOperation:
    """Clear non-reuse-bound labware tied to a submission."""

    Request: ClassVar[type[BaseModel]] = ClearSubmissionLabwareRequest
    Response: ClassVar[type[BaseModel]] = ClearSubmissionLabwareResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(
        self, req: ClearSubmissionLabwareRequest,
    ) -> ClearSubmissionLabwareResponse:
        try:
            result: ClearSubmissionResult = (
                await self._runtime.labware.clear_submission_labware(
                    req.submission_id, force=req.force,
                )
            )
        except ActiveExecutionRefusedError as exc:
            raise OperationError.typed(
                OperationErrorCode.CONFLICT,
                str(exc),
                wire_code="active_execution_refused",
                status_code=409,
                scope=exc.scope,
                submission_id=exc.submission_id,
            ) from exc
        except KeyError as exc:
            raise OperationError.not_found(
                f"submission {req.submission_id!r} not found",
                submission_id=req.submission_id,
            ) from exc
        return ClearSubmissionLabwareResponse(
            cleared=list(result.cleared),
            preserved_reuse_bound=list(result.preserved_reuse_bound),
        )


class ReleaseMoverHoldOperation:
    """Free one mover the record says is holding a labware."""

    Request: ClassVar[type[BaseModel]] = ReleaseMoverHoldRequest
    Response: ClassVar[type[BaseModel]] = ReleaseMoverHoldResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(
        self, req: ReleaseMoverHoldRequest,
    ) -> ReleaseMoverHoldResponse:
        try:
            result = await self._runtime.labware.release_mover_hold(
                req.mover_name, req.to_location,
                force=req.force, reason=req.reason, confirm=True,
            )
        except MoverHoldsNothingError as exc:
            raise OperationError.typed(
                OperationErrorCode.CONFLICT,
                str(exc),
                wire_code="mover_holds_nothing",
                status_code=409,
                mover_name=exc.mover_name,
            ) from exc
        except ActiveExecutionRefusedError as exc:
            raise OperationError.typed(
                OperationErrorCode.CONFLICT,
                str(exc),
                wire_code="active_execution_refused",
                status_code=409,
                scope=exc.scope,
                labware_id=exc.labware_id,
            ) from exc
        except LocationReservedError as exc:
            raise _location_reserved(exc) from exc
        except SlotOccupiedError as exc:
            raise _slot_occupied(exc) from exc
        except ValueError as exc:
            raise OperationError.invalid_input(str(exc)) from exc
        except KeyError as exc:
            raise OperationError.not_found(message_of(exc)) from exc
        return ReleaseMoverHoldResponse(
            mover_name=result.mover_name,
            labware_id=result.labware_id,
            labware_name=result.labware_name,
            released_to=result.released_to,
            discharged=result.discharged,
        )


class DischargeLabwareOperation:
    """Remove a single labware instance from runtime state."""

    Request: ClassVar[type[BaseModel]] = DischargeLabwareRequest
    Response: ClassVar[type[BaseModel]] = DischargeLabwareResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(
        self, req: DischargeLabwareRequest,
    ) -> DischargeLabwareResponse:
        try:
            await self._runtime.labware.discharge_labware(
                req.labware_id, force=req.force,
            )
        except ActiveExecutionRefusedError as exc:
            raise OperationError.typed(
                OperationErrorCode.CONFLICT,
                str(exc),
                wire_code="active_execution_refused",
                status_code=409,
                scope=exc.scope,
                labware_id=exc.labware_id,
            ) from exc
        except LabwareNotFoundError as exc:
            raise OperationError.not_found(
                message_of(exc), labware_id=exc.labware_id,
            ) from exc
        except KeyError as exc:
            raise OperationError.not_found(
                f"labware {req.labware_id!r} not found",
                labware_id=req.labware_id,
            ) from exc
        return DischargeLabwareResponse(labware_id=req.labware_id)


class ClearAllLabwareOperation:
    """Clear every Location + remove every labware in the runtime."""

    Request: ClassVar[type[BaseModel]] = ClearAllLabwareRequest
    Response: ClassVar[type[BaseModel]] = ClearAllLabwareResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(
        self, req: ClearAllLabwareRequest,
    ) -> ClearAllLabwareResponse:
        try:
            cleared = await self._runtime.labware.clear_all_labware(
                force=req.force,
            )
        except ActiveExecutionRefusedError as exc:
            raise OperationError.typed(
                OperationErrorCode.CONFLICT,
                str(exc),
                wire_code="active_execution_refused",
                status_code=409,
                scope=exc.scope,
            ) from exc
        return ClearAllLabwareResponse(cleared=list(cleared))

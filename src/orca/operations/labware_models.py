from orca.operations.binding import RestBinding, Route
from orca.operations.route_tags import RouteTag
"""Wire models for the labware operations.

Apart from `labware.py` because the Operation classes there take
`ISystemRuntime`, and the CLI reads these models over HTTP without
ever wanting the engine.
"""

from typing import Annotated, Literal
from typing_extensions import Self
from cheshire_drivers.move_parameters import MoveParameterField, MoveParameterPatch
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_serializer,
    model_validator,
)
from orca.state.placement import PlacementState
from orca.state.records import OperationDetails
from orca.state.provenance import Provenance, Source
from orca.runtime.status_models import LabwareSnapshot


class LabwareSnapshotModel(BaseModel):
    """Mirrors `LabwareSnapshot` dataclass on the wire."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    template_name: str
    labware_type: str = Field(description=(
        "The catalog key, the one `GET /api/labware-catalog/{labware_type}` takes. A sim "
        "template's placeholder, such as `_sim_plate`, and `unknown` are not in the catalog."
    ))
    barcode: str | None = None
    current_location: str | None = None
    placement: PlacementState | None = None
    """EXPECTED means `current_location` is where it is HEADED, not where it is."""
    carry_override: MoveParameterPatch = MoveParameterPatch()
    contents_provenance: Provenance = Provenance.UNKNOWN
    """How well the record knows what this labware holds. Serialises lowercase:
    `stale` is the one worth acting on, `unknown` means nothing has ever said
    rather than that it is empty."""

    @classmethod
    def from_dc(cls, snap: LabwareSnapshot) -> Self:
        return cls(
            id=snap.id, name=snap.name,
            template_name=snap.template_name,
            labware_type=snap.labware_type,
            barcode=snap.barcode,
            current_location=snap.current_location,
            placement=snap.placement,
            carry_override=snap.carry_override,
            contents_provenance=snap.contents_provenance,
        )

    @field_serializer("carry_override")
    def _only_what_was_said(self, patch: MoveParameterPatch) -> dict[str, JsonValue]:
        """Sparse: a null would read as an opinion nobody stated."""
        return patch.model_dump(exclude_none=True)


class GetLabwareByIdRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    labware_id: str


class GetLabwareByBarcodeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    barcode: str


class GetLabwareResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    labware: LabwareSnapshotModel


class ListLabwareRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ListLabwareResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    labware: list[LabwareSnapshotModel]


class EditLabwareLocationRequest(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", str_strip_whitespace=True,
    )
    labware_id: str
    location: str
    reason: str = Field(
        ...,
        min_length=1,
        description="Required audit reason. Empty / whitespace-only "
                    "is rejected with 422 at body-parse time.",
    )


class LabwareMutationResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["ok"] = "ok"
    labware_id: str


class EditLabwareBarcodeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    labware_id: str
    new_barcode: str
    reason: str | None = None


class SetLabwareCarryOverrideRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    labware_id: str
    set: MoveParameterPatch = MoveParameterPatch()
    clear: list[MoveParameterField] = []
    reason: str | None = None


class CarryOverrideResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    labware_id: str
    carry_override: MoveParameterPatch

    @field_serializer("carry_override")
    def _only_what_was_said(self, patch: MoveParameterPatch) -> dict[str, JsonValue]:
        """Sparse: a null would read as an opinion nobody stated."""
        return patch.model_dump(exclude_none=True)


class ClearLabwareCarryOverrideRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    labware_id: str
    reason: str | None = None


class ResetLabwareLocationRequest(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", str_strip_whitespace=True,
    )
    labware_id: str
    location: str
    reason: str = Field(
        ...,
        min_length=1,
        description="Required audit reason. Empty / whitespace-only "
                    "is rejected with 422 at body-parse time.",
    )


class RegisterLabwareRequest(BaseModel):
    """Say what labware an operator put down, and optionally where.

    Name it one of two ways. ``template_name`` is labware the deployment
    package declares. ``labware_type`` is a catalog definition it does not,
    which derives an ad-hoc template so labware the workflow author never
    anticipated can still be introduced without editing code.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")
    template_name: str | None = None
    labware_type: str | None = None
    barcode: str | None = None
    location: str | None = None
    """Where it physically is: any deck site, device site, storage position or
    mover gripper the topology knows. Omitted, the labware exists but sits
    nowhere."""
    reason: str | None = None

    @model_validator(mode="after")
    def _named_exactly_one_way(self) -> Self:
        # Blank counts as unset: a client that always sends every field says
        # "unset" with "" as readily as with null, and a blank name would
        # otherwise pass here and 404 later on a name nobody wrote.
        named = [
            field for field, value in (
                ("template_name", self.template_name),
                ("labware_type", self.labware_type),
            )
            if (value or "").strip() != ""
        ]
        if len(named) != 1:
            raise ValueError(
                "give exactly one of template_name (labware the deployment "
                f"package declares) or labware_type (a catalog definition it "
                f"does not); got {named or 'neither'}."
            )
        return self


class RegisterLabwareResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    labware: LabwareSnapshotModel
    status: Literal["registered"] = "registered"
    adopted_thread_id: str | None = None
    """The thread that was waiting for this labware and took it, or None when
    nothing was waiting and a new labware was made."""


class SetWellVolumesRequest(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", str_strip_whitespace=True,
    )
    labware_id: str
    well_volumes: dict[str, float] = Field(
        ...,
        min_length=1,
        description="Absolute per-well volumes in uL; at least one well. "
                    "Overwrites tracked volume for the named wells.",
    )
    reason: str = Field(
        ...,
        min_length=1,
        description="Required audit reason. Empty / whitespace-only "
                    "is rejected with 422 at body-parse time.",
    )


class ResolveContentsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    labware_id: str


class ContentsLayerModel(BaseModel):
    """One layer's reading, and whether it matches the resolved answer."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    layer: str
    tip_count: int | None = None
    agrees: bool
    note: str


class ResolveContentsResponse(BaseModel):
    """What a labware holds: the one read of its contents, for any labware.

    The record's own tip and volume layers and a liquid handler's deck state
    feed this answer. ``layers`` shows what each of them says, kept for
    diagnosis, so nothing has to go and compare them by hand.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")
    labware_id: str
    labware_name: str
    provenance: Provenance
    """`unknown` means nobody has ever said what this holds, not that it is empty."""
    source: Source
    """Which layer produced the current number."""
    tip_positions_present: list[str] | None = None
    tip_count: int | None = None
    volumes: dict[str, float] | None = None
    layers: list[ContentsLayerModel]


class MarkTipsUsedRequest(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", str_strip_whitespace=True,
    )
    labware_id: str
    positions: list[str] = Field(
        ...,
        min_length=1,
        description="Positions that no longer hold a tip; at least one. An "
                    "empty list would assert the layout unchanged and settle "
                    "the rack with nobody having looked at it.",
    )
    reason: str = Field(
        ...,
        min_length=1,
        description="Required audit reason. Empty / whitespace-only "
                    "is rejected with 422 at body-parse time.",
    )


class MarkTipsUsedResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    labware_id: str
    tip_positions_present: list[str]
    """What the rack still holds after the subtraction."""


class SetTipStateRequest(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", str_strip_whitespace=True,
    )
    labware_id: str
    tip_positions_present: list[str] = Field(
        ...,
        description="Positions that hold a tip; every other position on the "
                    "rack reads empty. Absolute overwrite of tracked state. "
                    "An empty list asserts an empty rack.",
    )
    reason: str = Field(
        ...,
        min_length=1,
        description="Required audit reason. Empty / whitespace-only "
                    "is rejected with 422 at body-parse time.",
    )


class ConfirmWellVolumesRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    labware_id: str
    reason: str | None = None


class ConfirmTipStateRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    labware_id: str
    reason: str | None = None


class JourneyMove(BaseModel):
    """One location-change entry in a labware's journey."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["move"] = "move"
    sequence: int
    position_id: str
    timestamp: float


class JourneyAction(BaseModel):
    """One ops_history entry in a labware's journey.

    ``details`` is a discriminated union keyed on its inner ``kind``
    field; Pydantic clients re-typecheck against the matching
    ``*Details`` variant automatically.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["action"] = "action"
    source: Literal["observed", "operator", "declared", "driver_observed"]
    """Who produced this entry: `observed` (the engine watched the call),
    `operator`, `declared`, or `driver_observed` (a snapshot the driver
    volunteered, which is a witness rather than a step). Without it nothing
    downstream could tell a step from a snapshot. The four are
    ``TrackingSource``; spelled out here so the wire contract validates
    itself rather than passing any string through."""
    timestamp: float
    device_name: str
    operation: str
    details: OperationDetails
    execution_id: str
    thread_id: str
    action_id: str
    # None mirrors TrackingRecord.method_id: bootstrap initial-state seeds
    # and free-floating actions belong to no enclosing method.
    method_id: str | None


JourneyEntry = Annotated[
    JourneyMove | JourneyAction, Field(discriminator="kind"),
]


class GetLabwareJourneyRequest(BaseModel):
    """Optional ``kinds`` filter: ``move``, ``action``, or both; omit to include both."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    labware_id: str
    kinds: list[Literal["move", "action"]] | None = None
    include_driver_snapshots: bool = False
    """Include the state snapshots a driver volunteers alongside each call.

    Off by default. Every liquid-handler call writes one snapshot per labware
    on the deck, each carrying every well, so on a real run they outnumbered
    the steps and buried them. They are a witness to what the driver believed,
    not a record of anything happening to this labware.
    """


class GetLabwareJourneyResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    labware_id: str
    entries: list[JourneyEntry]


class ClearSubmissionLabwareRequest(BaseModel):
    """Wire shape for ``POST /api/submissions/{submission_id}/clear-labware``.

    ``force=True`` overrides the safety check that refuses when the
    submission's execution is non-terminal.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    submission_id: str
    force: bool = False
    reason: str | None = None


class ClearSubmissionLabwareResponse(BaseModel):
    """Mirrors :class:`ClearSubmissionResult` on the wire.

    ``preserved_reuse_bound`` lists the labware ids that were skipped
    because their thread template declared ``end_leave_in_place`` (deck-
    resident reagents). Surfacing them lets the operator see what
    survived the clear.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    cleared: list[str]
    preserved_reuse_bound: list[str]


class ReleaseMoverHoldRequest(BaseModel):
    """Wire shape for ``POST /api/movers/{device_id}/release-hold``.

    Addressed by MOVER, because the mover is what the refusal names: a pick
    onto a mover the record says is already holding fails with the mover's own
    name, not the labware's id.

    ``to_location`` is where the operator has actually put the labware. Leave
    it out to discharge the labware instead, which is the answer when the jaws
    are empty and the record is wrong. ``force`` applies to the discharge path
    only, where a live thread still carrying the labware otherwise refuses.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    device_id: str
    to_location: str | None = None
    force: bool = False
    reason: str


class ReleaseMoverHoldResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    mover_name: str
    labware_id: str
    labware_name: str
    released_to: str | None
    discharged: bool


class DischargeLabwareRequest(BaseModel):
    """Wire shape for ``POST /api/labware-instances/{labware_id}/discharge``.

    Operator-initiated single-labware removal: "I picked it up
    physically." ``force=True`` overrides the safety check that refuses
    when any active execution holds a thread referencing the labware.
    Releasing an ``AWAITING_MANUAL_REMOVE`` park does NOT need it: that
    thread is already exempt, and forcing would drop the check for every
    other thread still using the plate.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    labware_id: str
    force: bool = False
    reason: str | None = None


class DischargeLabwareResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    labware_id: str
    status: Literal["discharged"] = "discharged"


class ClearAllLabwareRequest(BaseModel):
    """Wire shape for ``POST /api/labware-instances/clear-all``.

    Panic button. ``force=True`` overrides the safety check that
    refuses when any active execution exists.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    force: bool = False
    reason: str | None = None


class ClearAllLabwareResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    cleared: list[str]


GET_LABWARE_BY_ID_ROUTE = Route(
    GetLabwareByIdRequest,
    GetLabwareResponse,
    RestBinding(
        method="GET", path="/labware-instances/{labware_id}", tag=RouteTag.LABWARE,
    ),
)


GET_LABWARE_BY_BARCODE_ROUTE = Route(
    GetLabwareByBarcodeRequest,
    GetLabwareResponse,
    RestBinding(
        method="GET", path="/labware-barcodes/{barcode}", tag=RouteTag.LABWARE,
    ),
)


LIST_LABWARE_ROUTE = Route(
    ListLabwareRequest,
    ListLabwareResponse,
    RestBinding(
        method="GET", path="/labware-instances", tag=RouteTag.LABWARE,
    ),
)


EDIT_LABWARE_LOCATION_ROUTE = Route(
    EditLabwareLocationRequest,
    LabwareMutationResponse,
    RestBinding(
        method="POST", path="/labware-instances/{labware_id}/edit-location", tag=RouteTag.LABWARE,
        carries_blockers=True,
        drives_device=True,
    ),
)


EDIT_LABWARE_BARCODE_ROUTE = Route(
    EditLabwareBarcodeRequest,
    LabwareMutationResponse,
    RestBinding(
        method="PUT", path="/labware-instances/{labware_id}/barcode", tag=RouteTag.LABWARE,
    ),
)


SET_LABWARE_CARRY_OVERRIDE_ROUTE = Route(
    SetLabwareCarryOverrideRequest,
    CarryOverrideResponse,
    RestBinding(
        method="PUT", path="/labware-instances/{labware_id}/carry-override", tag=RouteTag.LABWARE,
    ),
)


CLEAR_LABWARE_CARRY_OVERRIDE_ROUTE = Route(
    ClearLabwareCarryOverrideRequest,
    CarryOverrideResponse,
    RestBinding(
        method="DELETE", path="/labware-instances/{labware_id}/carry-override", tag=RouteTag.LABWARE,
    ),
)


RESET_LABWARE_LOCATION_ROUTE = Route(
    ResetLabwareLocationRequest,
    LabwareMutationResponse,
    RestBinding(
        method="POST", path="/labware-instances/{labware_id}/reset-location", tag=RouteTag.LABWARE,
        drives_device=True,
    ),
)


REGISTER_LABWARE_ROUTE = Route(
    RegisterLabwareRequest,
    RegisterLabwareResponse,
    RestBinding(
        method="POST", path="/labware-instances", tag=RouteTag.LABWARE,
        carries_blockers=True,
        drives_device=True,
    ),
)


SET_WELL_VOLUMES_ROUTE = Route(
    SetWellVolumesRequest,
    LabwareMutationResponse,
    RestBinding(
        method="PUT", path="/labware-instances/{labware_id}/well-volumes", tag=RouteTag.LABWARE,
        carries_blockers=True,
        drives_device=True,
    ),
)


RESOLVE_CONTENTS_ROUTE = Route(
    ResolveContentsRequest,
    ResolveContentsResponse,
    RestBinding(
        method="GET", path="/labware-instances/{labware_id}/contents", tag=RouteTag.LABWARE,
    ),
)


MARK_TIPS_USED_ROUTE = Route(
    MarkTipsUsedRequest,
    MarkTipsUsedResponse,
    RestBinding(
        method="POST", path="/labware-instances/{labware_id}/tips/mark-used", tag=RouteTag.LABWARE,
        carries_blockers=True,
        drives_device=True,
    ),
)


SET_TIP_STATE_ROUTE = Route(
    SetTipStateRequest,
    LabwareMutationResponse,
    RestBinding(
        method="PUT", path="/labware-instances/{labware_id}/tips", tag=RouteTag.LABWARE,
        carries_blockers=True,
        drives_device=True,
    ),
)


CONFIRM_WELL_VOLUMES_ROUTE = Route(
    ConfirmWellVolumesRequest,
    LabwareMutationResponse,
    RestBinding(
        method="POST", path="/labware-instances/{labware_id}/well-volumes/confirm", tag=RouteTag.LABWARE,
    ),
)


CONFIRM_TIP_STATE_ROUTE = Route(
    ConfirmTipStateRequest,
    LabwareMutationResponse,
    RestBinding(
        method="POST", path="/labware-instances/{labware_id}/tips/confirm", tag=RouteTag.LABWARE,
        carries_blockers=True,
    ),
)


GET_LABWARE_JOURNEY_ROUTE = Route(
    GetLabwareJourneyRequest,
    GetLabwareJourneyResponse,
    RestBinding(
        method="GET", path="/labware-instances/{labware_id}/journey", tag=RouteTag.LABWARE,
    ),
)


CLEAR_SUBMISSION_LABWARE_ROUTE = Route(
    ClearSubmissionLabwareRequest,
    ClearSubmissionLabwareResponse,
    RestBinding(
        method="POST", path="/submissions/{submission_id}/clear-labware", tag=RouteTag.LABWARE,
        drives_device=True,
    ),
)


RELEASE_MOVER_HOLD_ROUTE = Route(
    ReleaseMoverHoldRequest,
    ReleaseMoverHoldResponse,
    RestBinding(
        method="POST", path="/movers/{device_id}/release-hold", tag=RouteTag.LABWARE,
        carries_blockers=True,
        drives_device=True,
    ),
)


DISCHARGE_LABWARE_ROUTE = Route(
    DischargeLabwareRequest,
    DischargeLabwareResponse,
    RestBinding(
        method="POST", path="/labware-instances/{labware_id}/discharge", tag=RouteTag.LABWARE,
        carries_blockers=True,
        drives_device=True,
    ),
)


CLEAR_ALL_LABWARE_ROUTE = Route(
    ClearAllLabwareRequest,
    ClearAllLabwareResponse,
    RestBinding(
        method="POST", path="/labware-instances/clear-all", tag=RouteTag.LABWARE,
        drives_device=True,
    ),
)

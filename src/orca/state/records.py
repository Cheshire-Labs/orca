import itertools
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


WellCertainty = Literal["confirmed_transferred", "definitely_not_transferred"]


class DeviceOperation(Enum):
    ASPIRATE = "aspirate"
    DISPENSE = "dispense"
    PICK_UP_TIPS = "pick_up_tips"
    DROP_TIPS = "drop_tips"
    DISCARD_TIPS = "discard_tips"
    SET_MOUNTED_TIPS = "set_mounted_tips"
    CONFIRM_MOUNTED_TIPS = "confirm_mounted_tips"
    MIX = "mix"
    ASPIRATE96 = "aspirate96"
    DISPENSE96 = "dispense96"
    PICK_UP_TIPS96 = "pick_up_tips96"
    DROP_TIPS96 = "drop_tips96"
    RETURN_TIPS96 = "return_tips96"
    SHAKE = "shake"
    SEAL = "seal"
    INCUBATE = "incubate"
    CENTRIFUGE = "centrifuge"
    READ = "read"
    DELID = "delid"
    RUN_PROTOCOL = "run_protocol"
    INITIAL_STATE = "initial_state"
    WELL_USAGE = "well_usage"
    SET_VOLUME = "set_volume"
    SET_TIP_STATE = "set_tip_state"
    ACTION_CONTINUED = "action_continued"
    OBSERVATION_GAP = "observation_gap"


class TrackingSource(Enum):
    """Where the record was synthesised, not the per-op source.

    Each ``OperationRecord`` carries its own ``source`` field; the value on
    the wrapping ``TrackingRecord`` describes how the BATCH was assembled,
    not every op's per-call provenance. A ``TrackingRecord(source=OBSERVED)``
    may contain individual ``OperationRecord`` entries with
    ``source=DRIVER_OBSERVED`` (the LiquidHandlerInterpreter mixes both
    inside one action's operation log when the driver provides state and
    the action declares no DeclaredTracking). Consumers querying by
    per-op source should walk ``record.operations`` and check each op's
    ``source`` field; consumers querying by synthesis mode use
    ``TrackingRecord.source``.
    """
    OBSERVED = "observed"
    DECLARED = "declared"
    DRIVER_OBSERVED = "driver_observed"
    OPERATOR = "operator"


_FROZEN = ConfigDict(frozen=True, extra="forbid")


class AspirateDetails(BaseModel):
    """Per-call aspirate details.

    On full success the interpreter emits one record covering all wells in the
    call (positions/volumes carry the full lists). On partial failure (driver
    returned LabwareStateResponse with non-empty per_channel_errors) the
    interpreter emits one record PER WELL with single-element positions/volumes
    and the per-well certainty/error_code fields populated.

    ``certainty`` defaults to ``"confirmed_transferred"`` so the success-path
    shape is unchanged. ``error_code`` is None unless this record describes a
    failed channel; in that case ``volumes`` carries [0] and ``error_code``
    carries the backend-supplied code.
    """
    model_config = _FROZEN
    kind: Literal["aspirate"] = "aspirate"
    labware: str
    positions: list[str]
    volumes: list[float]
    flow_rates: list[float] | None = None
    certainty: WellCertainty = "confirmed_transferred"
    error_code: str | None = None


class DispenseDetails(BaseModel):
    """Per-call dispense details. See ``AspirateDetails`` for the shape contract."""
    model_config = _FROZEN
    kind: Literal["dispense"] = "dispense"
    labware: str
    positions: list[str]
    volumes: list[float]
    flow_rates: list[float] | None = None
    certainty: WellCertainty = "confirmed_transferred"
    error_code: str | None = None


class TipPickUpDetails(BaseModel):
    model_config = _FROZEN
    kind: Literal["pick_up_tips"] = "pick_up_tips"
    tip_rack: str
    positions: list[str]
    use_channels: list[int] | None = None
    """Which channels took them, in the same order as ``positions``. Without it
    a record says a tip was taken but not what is now carrying it."""
    channels_were_counted: bool = False
    """The channels above were counted in target order across the whole command
    rather than named by whoever sent it, so they are the record's own best
    guess at which nozzle holds what."""


class TipDropDetails(BaseModel):
    model_config = _FROZEN
    kind: Literal["drop_tips"] = "drop_tips"
    tip_rack: str
    positions: list[str]
    to_waste: bool = False
    use_channels: list[int] | None = None


class MountedTipsAssertedDetails(BaseModel):
    model_config = _FROZEN
    kind: Literal["mounted_tips_asserted"] = "mounted_tips_asserted"
    by_channel: dict[int, tuple[str, str]]
    """channel -> (tip rack, position). Empty means the operator says the head
    is carrying nothing, which is a statement, not silence."""


class MountedTipsConfirmedDetails(BaseModel):
    """The operator agrees with what the record already says the head carries.

    Separate from an assertion because it names no tips. Confirming settles who
    has looked; it does not restate which channel each tip is on, so a channel
    number the record only guessed stays marked as a guess.
    """

    model_config = _FROZEN
    kind: Literal["mounted_tips_confirmed"] = "mounted_tips_confirmed"
    device_name: str


class TipDiscardDetails(BaseModel):
    model_config = _FROZEN
    kind: Literal["discard_tips"] = "discard_tips"
    use_channels: list[int] | None = None
    """A discard names no rack: the tips go to waste and stop existing."""


class ShakeDetails(BaseModel):
    model_config = _FROZEN
    kind: Literal["shake"] = "shake"
    speed_rpm: float
    duration_s: float


class SealDetails(BaseModel):
    model_config = _FROZEN
    kind: Literal["seal"] = "seal"
    temperature_c: float
    duration_s: float


class IncubateDetails(BaseModel):
    model_config = _FROZEN
    kind: Literal["incubate"] = "incubate"
    temperature_c: float
    duration_s: float


class CentrifugeDetails(BaseModel):
    model_config = _FROZEN
    kind: Literal["centrifuge"] = "centrifuge"
    g_force: float
    duration_s: float


class RunProtocolDetails(BaseModel):
    model_config = _FROZEN
    kind: Literal["run_protocol"] = "run_protocol"
    protocol_filepath: str
    params: dict[str, str | int | float | bool]


class Aspirate96Details(BaseModel):
    model_config = _FROZEN
    kind: Literal["aspirate96"] = "aspirate96"
    labware: str
    volume: float
    flow_rate: float | None = None
    liquid_height: float | None = None


class Dispense96Details(BaseModel):
    model_config = _FROZEN
    kind: Literal["dispense96"] = "dispense96"
    labware: str
    volume: float
    flow_rate: float | None = None
    liquid_height: float | None = None


class TipPickUp96Details(BaseModel):
    model_config = _FROZEN
    kind: Literal["pick_up_tips96"] = "pick_up_tips96"
    tip_rack: str


class TipDrop96Details(BaseModel):
    model_config = _FROZEN
    kind: Literal["drop_tips96"] = "drop_tips96"
    tip_rack: str | None = None
    to_waste: bool = True


class GenericOperationDetails(BaseModel):
    model_config = _FROZEN
    kind: Literal["generic"] = "generic"
    command: str
    args_repr: str


class MixDetails(BaseModel):
    model_config = _FROZEN
    kind: Literal["mix"] = "mix"
    labware: str
    positions: list[str]
    volume_ul: float
    cycles: int


class WellUsageDetails(BaseModel):
    model_config = _FROZEN
    kind: Literal["well_usage"] = "well_usage"
    labware: str
    positions: list[str]


class SetVolumeDetails(BaseModel):
    """Operator-set absolute per-well volumes (source=OPERATOR).

    Folded as an absolute overwrite of the named wells at its position in
    history; later aspirate/dispense deltas still apply on top. Carries the
    WHAT only -- the WHY rides the @dangerous audit trail, never the record.
    """
    model_config = _FROZEN
    kind: Literal["set_volume"] = "set_volume"
    labware: str
    well_volumes: dict[str, float]


class SetTipStateDetails(BaseModel):
    """Operator-asserted absolute tip layout (source=OPERATOR).

    Folded as an absolute overwrite: the named positions hold tips, every
    other position on the rack does not, and later pick-ups/drops apply on
    top. Carries the WHAT only -- the WHY rides the @dangerous audit trail.
    """
    model_config = _FROZEN
    kind: Literal["set_tip_state"] = "set_tip_state"
    labware: str
    tip_positions_present: list[str]


class ActionContinuedDetails(BaseModel):
    """An action errored and an operator chose to carry on past it (source=OPERATOR).

    Folds into nothing. Continuing asserts that the run may proceed, never that
    the action's work happened, so this record must not move a volume or a tip.
    It sits in the labware's history so a reader can see the gap, what failed,
    and that a person authorized carrying on.
    """
    model_config = _FROZEN
    kind: Literal["action_continued"] = "action_continued"
    command: str
    error_type: str
    error_message: str


class ObservationGapCause(Enum):
    """Why nobody was watching this labware for a while."""

    RUNTIME_RESTART = "runtime_restart"
    DEVICE_RECONNECT = "device_reconnect"
    ERROR_PAUSE = "error_pause"
    OPERATOR_CONTRADICTED = "operator_contradicted"
    """An operator command only made sense if the record was wrong. Unlike
    the stretches of time this is one moment that proved the fold no longer
    describes the labware, and by an amount the command itself cannot say."""

    OPERATIONS_DROPPED = "operations_dropped"
    """An action was abandoned holding operations it had really performed, and
    they went with it. Also one moment rather than a stretch: the pick-up and
    the aspirate happened, nobody wrote them down, and nothing will now."""


GAPS_THAT_LOST_WORK = frozenset({ObservationGapCause.OPERATIONS_DROPPED})
"""The cause where work really happened and nothing will ever record it.

Agreeing with the fold cannot settle this one. The labware looks exactly as the
record describes it to anyone glancing at the shelf, and the record is still
short by what the abort discarded, so a confirm freezes a wrong number and
marks it checked. Only counting and stating settles it.

A contradicted record is deliberately NOT here. Something was proved wrong at
one position at one moment, and the pick that proved it is folded, so an
operator who looks at the labware can find the record right and agree with it.
That is the same contract every other stale row has.

Named once because three surfaces ask the same question of it: the verb the
worklist offers, the confirm the facade refuses, and the sentence an operator
reads.
"""


class ObservationGapDetails(BaseModel):
    """A stretch of time during which a hand could have changed this labware
    with nothing recording it.

    Written when the runtime restarts, a device bridge reconnects, a thread
    pauses on an error, or an operator command contradicts the fold. It moves
    no volume and no tip: the fold is unchanged, but an operator attestation
    made before the gap no longer counts as current.
    """
    model_config = _FROZEN
    kind: Literal["observation_gap"] = "observation_gap"
    labware: str
    cause: ObservationGapCause


class HeadObservationGapDetails(BaseModel):
    """The same gap, about a liquid handler's channels rather than a labware."""
    model_config = _FROZEN
    kind: Literal["head_observation_gap"] = "head_observation_gap"
    device_name: str
    cause: ObservationGapCause


class InitialStateDetails(BaseModel):
    model_config = _FROZEN
    kind: Literal["initial_state"] = "initial_state"
    labware: str
    well_volumes: dict[str, float] | None = None
    tip_positions_present: list[str] | None = None
    # A single-pool container (trough): the whole 96-head draws from the one
    # pool, so a 96-head op folds head_size x volume, not one volume per well.
    single_pool: bool = False


OperationDetails = Annotated[
    Union[
        AspirateDetails,
        DispenseDetails,
        TipPickUpDetails,
        TipDropDetails,
        TipDiscardDetails,
        MountedTipsAssertedDetails,
        MountedTipsConfirmedDetails,
        HeadObservationGapDetails,
        Aspirate96Details,
        Dispense96Details,
        TipPickUp96Details,
        TipDrop96Details,
        ShakeDetails,
        SealDetails,
        IncubateDetails,
        CentrifugeDetails,
        RunProtocolDetails,
        GenericOperationDetails,
        MixDetails,
        WellUsageDetails,
        InitialStateDetails,
        SetVolumeDetails,
        SetTipStateDetails,
        ActionContinuedDetails,
        ObservationGapDetails,
    ],
    Field(discriminator="kind"),
]


_op_sequence_counter = itertools.count(1)


def _next_op_sequence() -> int:
    """Process-wide monotonic sequence stamped at record construction.

    Breaks ``timestamp`` ties when records from different ops buckets are
    merged chronologically (the system bucket's operator SET_VOLUME vs an
    execution bucket's aspirate/dispense): on coarse clocks two records can
    share a timestamp, and a stable sort then orders them by bind order
    rather than creation order. The counter resets per process, which is
    safe because cross-process timestamp ties cannot occur (a restart takes
    far longer than clock granularity); the value is persisted with the
    record so reload preserves the original within-run order.
    """
    return next(_op_sequence_counter)


class OperationRecord(BaseModel):
    """A single device operation recorded at the queue bridge.

    ``timestamp`` is seconds-since-epoch (``time.time()``) at the moment the
    op was captured. Required; no default -- callers must stamp explicitly.
    ``sequence`` is a monotonic tiebreak for equal timestamps (see
    ``_next_op_sequence``); auto-assigned at construction.

    ``source`` distinguishes operation-derived records (default OBSERVED) from
    driver-state-derived records (DRIVER_OBSERVED). When a driver advertises
    ``provides_state=True`` and the user opts into ``trust_driver_state``, the
    LiquidHandler bridge emits paired DRIVER_OBSERVED records reflecting the
    driver's reported labware state. Trackers fold both kinds; ledger projections
    can prefer DRIVER_OBSERVED as authoritative when present.

    ``affected_labware`` holds human-readable display names; ``affected_labware_ids``
    holds the parallel canonical UUIDs that ``labware journey`` / ``labware history``
    accept. Populated wherever the recording site holds a LabwareInstance (the
    device-call dispatch boundary, the declared-tracking observer); empty when only
    a driver-reported name is available.
    """

    model_config = _FROZEN

    operation: DeviceOperation
    device_name: str
    affected_labware: list[str]
    affected_labware_ids: list[str] = Field(default_factory=list)
    action_id: str
    thread_id: str
    details: OperationDetails
    timestamp: float
    sequence: int = Field(default_factory=_next_op_sequence)
    group_id: str | None = None
    source: TrackingSource = TrackingSource.OBSERVED


class TrackingRecord(BaseModel):
    """Per-action tracking summary.

    Carries only the ordered typed ``operations`` list plus per-action
    metadata. Denormalized summary fields (volume_changes, tips_used,
    wells_used, tip_status_changes) were removed; consumers derive what
    they need from ``operations`` via ledger_projections.

    ``execution_id`` is required: every record self-describes which
    execution produced it. System-bucket records (initial-state seeds
    fired before any user execution exists) carry the ``SYSTEM_ID``
    sentinel rather than a free-floating empty string. Indexed downstream
    (a DB column in a hosted deployment) so cross-execution search is a primary-key
    lookup, not an array scan.

    ``method_id`` is None when the record belongs to no enclosing method:
    bootstrap initial-state seeds (no method ever ran), free-floating
    actions outside any method composition. Co-thread methods still get
    a real method_id; only the genuinely-no-method case is None.

    ``timestamp`` is seconds-since-epoch at record construction; required.
    """

    model_config = _FROZEN

    execution_id: str
    action_id: str
    thread_id: str
    method_id: str | None
    source: TrackingSource
    timestamp: float
    operations: list[OperationRecord] = Field(default_factory=list)


class DeclaredVolumeTransfer(BaseModel):
    model_config = _FROZEN

    source: str
    target: str
    volume_ul: float
    source_wells: list[str] | None = None
    target_wells: list[str] | None = None


class LabwareInitialState(BaseModel):
    """Runtime-overridable initial-state declaration for a labware instance.

    Interpreted at birth by LabwareTemplate.declared_contents:
    - max_fill=True: wells fill to max_volume, tip racks fill every position.
    - uniform_volume: every well starts at this volume (ignored for tip racks).
    - wells: per-well volume dict (ignored for tip racks).
    - tip_positions: only these positions hold a tip (tip racks only).
    - replenished=True: reagent source; the seeded volume never depletes in sim
      (aspirates never run it dry), so a shared reagent survives any batch size.
      Sim-only; real hardware runs the actual finite reagent.

    Fields are mutually descriptive, not mutually exclusive; max_fill wins when set.
    """

    model_config = _FROZEN

    wells: dict[str, float] | None = None
    uniform_volume: float | None = None
    max_fill: bool = False
    tip_positions: list[str] | None = None
    replenished: bool = False


class DeclaredTracking(BaseModel):
    """User annotations for closed-protocol actions."""

    model_config = _FROZEN

    wells_used: dict[str, list[str]] | None = None
    volume_transferred: list[DeclaredVolumeTransfer] | None = None
    tips_used: dict[str, list[str]] | None = None
    operations: list[tuple[DeviceOperation, OperationDetails]] | None = None
    initial_state: dict[str, LabwareInitialState] | None = None

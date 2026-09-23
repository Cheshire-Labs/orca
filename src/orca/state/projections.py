"""Pure projections over a labware's ops history.

Layer 2 in the tracking stack:

    Layer 3: can_continue(demand)
    Layer 2: pure derivation functions over ops  <-- this module
    Layer 1: OpsHistory on ISystem (single source of truth)

Functions are stateless and take an ops list plus the labware name.
OpsHistory.ops_for(name) produces the input. No caching; re-compute on
read. If profiling ever demands it, add a memoizer here without
changing the surface.
"""

from orca.state.provenance import Provenance, Source
from orca.state.records import (
    AspirateDetails,
    Aspirate96Details,
    DeviceOperation,
    DispenseDetails,
    Dispense96Details,
    InitialStateDetails,
    ObservationGapCause,
    ObservationGapDetails,
    OperationRecord,
    SetTipStateDetails,
    SetVolumeDetails,
    TipDropDetails,
    TipDrop96Details,
    TipPickUpDetails,
    TipPickUp96Details,
    TrackingSource,
)


def contents_source(
    ops: list[OperationRecord], labware_name: str,
) -> Source:
    """Which layer the current answer came from, newest wins.

    The counterpart to ``contents_provenance``: provenance says how much the
    record knows, this says where the number came from.
    """
    source = Source.NONE
    for op in ops:
        details = op.details
        if isinstance(details, InitialStateDetails) and _is_declaration(op) and details.labware == labware_name:
            source = Source.DECLARATION
        elif isinstance(details, (SetVolumeDetails, SetTipStateDetails)) and details.labware == labware_name:
            source = Source.OPERATOR
        elif labware_name in op.affected_labware and op.operation in (
            *_ASPIRATES, *_DISPENSES, *_TIP_PICKUPS, *_TIP_DROPS,
        ) and op.source is not TrackingSource.DRIVER_OBSERVED:
            source = Source.OPERATION
    return source


def _is_declaration(op: OperationRecord) -> bool:
    """True for an initial-state record the system itself wrote.

    A driver's own report arrives wearing the same details model. It is a
    witness, not a baseline: its numbers came from what we projected onto that
    driver in the first place, so folding it would launder our own guess back in
    as evidence. Compare it (see ``latest_driver_report``); never fold it.
    """
    return (
        isinstance(op.details, InitialStateDetails)
        and op.source is not TrackingSource.DRIVER_OBSERVED
    )


_ASPIRATES = (DeviceOperation.ASPIRATE, DeviceOperation.ASPIRATE96)
_DISPENSES = (DeviceOperation.DISPENSE, DeviceOperation.DISPENSE96)
_TIP_PICKUPS = (DeviceOperation.PICK_UP_TIPS, DeviceOperation.PICK_UP_TIPS96)
_TIP_DROPS = (DeviceOperation.DROP_TIPS, DeviceOperation.DROP_TIPS96, DeviceOperation.RETURN_TIPS96, DeviceOperation.DISCARD_TIPS)


# A 96-head op engages the whole head, so a single-pool container (trough) loses
# head_size x volume while a plate spreads one volume per known well.
_HEAD96_CHANNEL_COUNT = 96


def _fold_96(totals: dict[str, float], signed_volume: float, single_pool: bool) -> None:
    """Apply a 96-head aspirate (negative) or dispense (positive) to a labware's
    folded volumes. A trough's single pool takes the full head (head_size x volume);
    a plate grid takes one volume per known well. ``single_pool`` is carried by the
    labware's seed, not inferred from the well count -- a plate with one known well
    is still a plate. An unseeded labware (no known wells) is untracked either way.

    Assumes a FULL 96-head. Partial-head engagement (an offset that seats only some
    channels, or a cherry-pick that leaves only some tips for the 96-head) is not
    modeled: the 96-detail carries no engaged-channel count, so a single pool would
    be over-drained here. Tracked as separate work."""
    multiplier = _HEAD96_CHANNEL_COUNT if single_pool else 1
    for pos in totals:
        totals[pos] = totals[pos] + signed_volume * multiplier


def well_volumes(ops: list[OperationRecord], labware_name: str) -> dict[str, float]:
    """Per-well current volume, folded from INITIAL_STATE + aspirate + dispense.

    No clamp -- negative values surface overdraw for the caller.

    An operator SET_VOLUME overwrites its named wells absolutely at its
    position in history; later aspirate/dispense deltas still apply on top.

    Wells never seeded and never touched do not appear in the result.
    Wells touched by an aspirate/dispense without a prior seed fold from
    a base of 0.0 (the aspirate surfaces as a negative value).

    BLACK-BOX DRIFT: DeclaredTrackingObserver can emit AspirateDetails with
    positions=[] when source wells are unknown (source_wells=None in the
    DeclaredVolumeTransfer). The source labware loses no per-well volume
    in that case; callers should expect drift for any labware whose
    aspirates are declared black-box. Documented, intentional.
    """
    totals: dict[str, float] = {}
    single_pool = False
    for op in ops:
        if labware_name not in op.affected_labware:
            continue
        details = op.details
        if _is_declaration(op):
            if isinstance(details, InitialStateDetails) and details.labware == labware_name and details.well_volumes is not None:
                totals.update(details.well_volumes)
                single_pool = single_pool or details.single_pool
        elif isinstance(details, SetVolumeDetails) and details.labware == labware_name:
            totals.update(details.well_volumes)
        elif isinstance(details, AspirateDetails) and details.labware == labware_name:
            for pos, vol in zip(details.positions, details.volumes):
                totals[pos] = totals.get(pos, 0.0) - vol
        elif isinstance(details, DispenseDetails) and details.labware == labware_name:
            for pos, vol in zip(details.positions, details.volumes):
                totals[pos] = totals.get(pos, 0.0) + vol
        elif isinstance(details, Aspirate96Details) and details.labware == labware_name:
            _fold_96(totals, -details.volume, single_pool)
        elif isinstance(details, Dispense96Details) and details.labware == labware_name:
            _fold_96(totals, details.volume, single_pool)
    return totals


def well_volume(ops: list[OperationRecord], labware_name: str, well_id: str) -> float | None:
    """Current volume for one well, or None if unseeded and untouched."""
    return well_volumes(ops, labware_name).get(well_id)


def has_volume_history(ops: list[OperationRecord], labware_name: str) -> bool:
    """True when the ops carry volume information beyond a zero-only baseline.

    The creation-time seed of an undeclared labware is an all-zero
    INITIAL_STATE; treating that as history would project the zero baseline
    and flip the driver's lenient tracker to strict-at-zero. Mirrors the op
    kinds ``well_volumes`` folds, so the two answer consistently.
    """
    for op in ops:
        if labware_name not in op.affected_labware:
            continue
        details = op.details
        if _is_declaration(op):
            if (
                isinstance(details, InitialStateDetails)
                and details.labware == labware_name
                and details.well_volumes is not None
                and any(v != 0.0 for v in details.well_volumes.values())
            ):
                return True
        elif isinstance(details, SetVolumeDetails) and details.labware == labware_name:
            return True
        elif isinstance(details, (AspirateDetails, DispenseDetails)) and details.labware == labware_name:
            return True
        elif isinstance(details, (Aspirate96Details, Dispense96Details)) and details.labware == labware_name:
            return True
    return False


def tips_present(ops: list[OperationRecord], tip_rack_name: str) -> set[str]:
    """Positions currently holding a tip on this rack.

    = INITIAL_STATE.tip_positions_present
      + {pos for DROP op with tip_rack=this, to_waste=False}  # returned
      - {pos for PICK_UP op on this rack}
    """
    present: set[str] = set()
    # The layout the rack most recently started from; a 96-drop-to-rack
    # restores it, since 96-head ops carry no per-position list.
    last_baseline: set[str] = set()
    for op in ops:
        if tip_rack_name not in op.affected_labware:
            continue
        if _is_declaration(op):
            details = op.details
            if isinstance(details, InitialStateDetails) and details.labware == tip_rack_name and details.tip_positions_present is not None:
                present.update(details.tip_positions_present)
                last_baseline = set(details.tip_positions_present)
        elif op.operation == DeviceOperation.SET_TIP_STATE:
            details = op.details
            if isinstance(details, SetTipStateDetails) and details.labware == tip_rack_name:
                present = set(details.tip_positions_present)
                last_baseline = set(details.tip_positions_present)
        elif op.operation in _TIP_PICKUPS:
            details = op.details
            if isinstance(details, TipPickUpDetails) and details.tip_rack == tip_rack_name:
                for pos in details.positions:
                    present.discard(pos)
            elif isinstance(details, TipPickUp96Details) and details.tip_rack == tip_rack_name:
                present.clear()
        elif op.operation in _TIP_DROPS:
            details = op.details
            if isinstance(details, TipDropDetails) and details.tip_rack == tip_rack_name and not details.to_waste:
                present.update(details.positions)
            # TipDrop96Details + to_waste=False returning-to-rack is represented
            # by the same rack name; positions are every channel.
            elif isinstance(details, TipDrop96Details) and details.tip_rack == tip_rack_name and not details.to_waste:
                present.update(last_baseline)
    return present


def has_tip_baseline(ops: list[OperationRecord], tip_rack_name: str) -> bool:
    """True when these ops carry a baseline describing this rack's tips: an
    INITIAL_STATE seed or an operator SET_TIP_STATE.

    Without one, ``tips_present`` has nothing to subtract from, so an empty
    result means the rack's starting layout was never seen, not that the rack
    ran out. A rack restored from a store reads that way: its seed lives in an
    execution bucket that nothing has bound.
    """
    for op in ops:
        details = op.details
        if (
            _is_declaration(op)
            and isinstance(details, InitialStateDetails)
            and details.labware == tip_rack_name
            and details.tip_positions_present is not None
        ):
            return True
        if (
            op.operation == DeviceOperation.SET_TIP_STATE
            and isinstance(details, SetTipStateDetails)
            and details.labware == tip_rack_name
        ):
            return True
    return False


def tip_positions_seen(ops: list[OperationRecord], tip_rack_name: str) -> set[str]:
    """Every position on this rack the history has said anything about.

    The seed universe for a restored rack: ``tips_present`` names what holds a
    tip, this names what the record covers at all, so a seeded projection can
    say "empty" for a picked position instead of staying silent about it.
    96-head ops carry no positions and contribute nothing here.
    """
    seen: set[str] = set()
    for op in ops:
        if tip_rack_name not in op.affected_labware:
            continue
        details = op.details
        if isinstance(details, InitialStateDetails) and _is_declaration(op) and details.labware == tip_rack_name and details.tip_positions_present is not None:
            seen.update(details.tip_positions_present)
        elif isinstance(details, SetTipStateDetails) and details.labware == tip_rack_name:
            seen.update(details.tip_positions_present)
        elif isinstance(details, TipPickUpDetails) and details.tip_rack == tip_rack_name:
            seen.update(details.positions)
        elif isinstance(details, TipDropDetails) and details.tip_rack == tip_rack_name:
            seen.update(details.positions)
    return seen


def tips_used(ops: list[OperationRecord], tip_rack_name: str) -> set[str]:
    """Positions picked from this rack and not returned here.

    Distinct from tips_present: a position may be neither present nor used
    if the rack was initialized empty at that slot.
    """
    picked: set[str] = set()
    returned: set[str] = set()
    for op in ops:
        if tip_rack_name not in op.affected_labware:
            continue
        if op.operation in _TIP_PICKUPS:
            details = op.details
            if isinstance(details, TipPickUpDetails) and details.tip_rack == tip_rack_name:
                picked.update(details.positions)
        elif op.operation in _TIP_DROPS:
            details = op.details
            if isinstance(details, TipDropDetails) and details.tip_rack == tip_rack_name and not details.to_waste:
                returned.update(details.positions)
    return picked - returned


def op_count(
    ops: list[OperationRecord],
    labware_name: str,
    op_types: tuple[DeviceOperation, ...] | None = None,
) -> int:
    """Count ops of interest touching labware_name.

    Default op_types = all aspirates and dispenses (single- and 96-channel):
    every direct liquid-handler contact counts. The SMC 'full after 4
    combines' gate resolves correctly from either side -- final_plate
    sees 4 dispenses, source_plate sees 4 aspirates, both reach 4.

    Pass op_types to narrow:
        op_count(ops, name, (DISPENSE, DISPENSE96))  # receive-only
        op_count(ops, name, (ASPIRATE, ASPIRATE96))  # source-drain
        op_count(ops, name, (SHAKE,))                # how many shakes
    """
    if op_types is None:
        op_types = (*_ASPIRATES, *_DISPENSES)
    return sum(
        1 for op in ops
        if op.operation in op_types and labware_name in op.affected_labware
    )


def sample_events_at(
    ops: list[OperationRecord], labware_name: str, well_id: str
) -> list[OperationRecord]:
    """Ordered ops that touch (labware_name, well_id).

    Returns every aspirate or dispense op that names well_id in its
    positions list, in ops order. Callers compose a sample identity by
    walking backwards across group_id or positional channel index.
    """
    result: list[OperationRecord] = []
    for op in ops:
        if labware_name not in op.affected_labware:
            continue
        details = op.details
        if isinstance(details, AspirateDetails) and details.labware == labware_name and well_id in details.positions:
            result.append(op)
        elif isinstance(details, DispenseDetails) and details.labware == labware_name and well_id in details.positions:
            result.append(op)
    return result


def has_contents_baseline(ops: list[OperationRecord], labware_name: str) -> bool:
    """True when the record has ever stated what this labware holds.

    The question birth asks before writing an opening entry, and the question a
    read asks before answering with a number. Without one, a fold has nothing to
    subtract from, so an empty result means "never told", not "ran out".
    """
    for op in ops:
        details = op.details
        if isinstance(details, InitialStateDetails) and _is_declaration(op) and details.labware == labware_name:
            return True
        if isinstance(details, (SetVolumeDetails, SetTipStateDetails)) and details.labware == labware_name:
            return True
    return False


def unsettled_gaps(
    ops: list[OperationRecord], labware_name: str,
) -> frozenset[ObservationGapCause]:
    """Every gap standing over this labware's contents, by cause.

    Only an operator's word closes a gap; the system's own fold cannot, because
    the fold is what the gap called into question. A later gap does not replace
    an earlier one either: an abort leaves the count short by the work it threw
    away, and a restart afterwards says nothing about that shortfall.
    """
    standing: set[ObservationGapCause] = set()
    for op in ops:
        details = op.details
        if isinstance(details, (SetVolumeDetails, SetTipStateDetails)) and details.labware == labware_name:
            standing.clear()
        elif isinstance(details, ObservationGapDetails) and details.labware == labware_name:
            standing.add(details.cause)
    return frozenset(standing)


def contents_provenance(
    ops: list[OperationRecord], labware_name: str,
) -> Provenance:
    """How much the record knows about this labware's contents.

    A restart, a reconnect or an error pause opens a stretch nobody was
    watching, and an aborted action throws away work it really did. Anything
    known before either describes then, not now, so the answer needs a look
    until someone gives it one.
    """
    if not has_contents_baseline(ops, labware_name):
        return Provenance.UNKNOWN
    if not unsettled_gaps(ops, labware_name):
        return Provenance.KNOWN
    return Provenance.STALE


# Listed rather than derived: a cause missing from this set is refused, and
# refusing costs a person a count while admitting one wrongly can drive a head
# at a position the record calls empty.
_GAPS_A_HAND_COULD_HAVE_FILLED = frozenset({
    ObservationGapCause.RUNTIME_RESTART,
    ObservationGapCause.DEVICE_RECONNECT,
    ObservationGapCause.ERROR_PAUSE,
    ObservationGapCause.OPERATOR_CONTRADICTED,
})


def went_unobserved(
    ops: list[OperationRecord], labware_name: str,
) -> bool:
    """True while a stretch nobody was watching stands over this labware.

    Narrower than ``contents_provenance`` answering STALE. Every gap makes a
    read stale, but only some of them leave room for a hand having put
    something back, and that chance is the only reason to let work through at a
    position the record calls empty. An aborted action is not that chance: it
    threw away work it really did, so the count is wrong by an amount and in a
    direction nothing can state, and neither the lenient nor the strict
    assumption is safe. Refusing and asking a person is what is left.

    One such gap is enough. A restart landing after an abort does not make the
    missing tips come back, so the rack stays refused until somebody counts it.
    """
    if not has_contents_baseline(ops, labware_name):
        return False
    standing = unsettled_gaps(ops, labware_name)
    return bool(standing) and standing <= _GAPS_A_HAND_COULD_HAVE_FILLED


def latest_driver_report(
    ops: list[OperationRecord], labware_name: str,
) -> InitialStateDetails | None:
    """The most recent state a driver reported for this labware, or None.

    Never folded (see ``_is_declaration``). Its use is comparison: a driver that
    disagrees with the fold is telling us one of the two is wrong, which is worth
    raising even though neither side can settle it alone.
    """
    latest: InitialStateDetails | None = None
    for op in ops:
        details = op.details
        if (
            op.source is TrackingSource.DRIVER_OBSERVED
            and isinstance(details, InitialStateDetails)
            and details.labware == labware_name
        ):
            latest = details
    return latest


def tip_layout(
    ops: list[OperationRecord], tip_rack_name: str,
) -> dict[str, bool] | None:
    """Every position the record has spoken about, True where a tip sits now.

    None without a baseline: a pick with nothing to subtract from would project
    an empty rack nobody ever saw. A picked position projects False rather than
    dropping off, so a rack empties as its history says.
    """
    if not has_tip_baseline(ops, tip_rack_name):
        return None
    present = tips_present(ops, tip_rack_name)
    return {pos: pos in present for pos in tip_positions_seen(ops, tip_rack_name)}


def sparse_volumes(
    ops: list[OperationRecord], labware_name: str,
) -> dict[str, float] | None:
    """Non-zero wells, plus any well an operator named. None without history.

    An operator's asserted zero rides the projection, so its tracker is strict
    about that well; the implicit zero of a well nobody has touched stays off
    it, so an undeclared labware keeps a lenient one.
    """
    if not has_volume_history(ops, labware_name):
        return None
    operator_wells: set[str] = set()
    for op in ops:
        details = op.details
        if isinstance(details, SetVolumeDetails) and details.labware == labware_name:
            operator_wells.update(details.well_volumes)
    folded = well_volumes(ops, labware_name)
    return {w: v for w, v in folded.items() if v != 0.0 or w in operator_wells}

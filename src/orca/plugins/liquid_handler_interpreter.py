import logging
import time
from typing import Mapping, Protocol, Sequence, TypeVar

from cheshire_drivers.labware_interfaces import IContainer, IPlate, ITipRack, ITrough
from cheshire_drivers.liquid_handler_models import ChannelError, LabwareStateResponse, TROUGH_WELL_ID

logger = logging.getLogger("orca.plugins.liquid_handler_interpreter")


# Mirrors the LiquidHandler bridge signatures (sync-guard test pins it) so
# every bridge-legal arg/kwarg split interprets identically.
_VERB_PARAMS: dict[str, tuple[str, ...]] = {
    "aspirate": (
        "containers", "volumes", "flow_rates", "offsets_z", "use_channels",
        "liquid_class", "technique",
    ),
    "dispense": (
        "containers", "volumes", "flow_rates", "offsets_z", "use_channels",
        "liquid_class", "technique",
    ),
    "pick_up_tips": ("tip_spots",),
    "drop_tips": ("tip_spots",),
    "discard_tips": ("use_channels",),
    "aspirate96": ("labware", "volume", "flow_rate", "liquid_height"),
    "dispense96": ("labware", "volume", "flow_rate", "liquid_height"),
    "pick_up_tips96": ("tip_rack",),
    "drop_tips96": ("tip_rack",),
}

_V = TypeVar("_V")


def _bind_verb_call(
    command: str,
    args: tuple[_V, ...],
    kwargs: dict[str, _V],
) -> dict[str, _V]:
    names = _VERB_PARAMS[command]
    if len(args) > len(names):
        raise TypeError(
            f"{command}() takes at most {len(names)} arguments, "
            f"got {len(args)} positional"
        )
    bound: dict[str, _V] = dict(zip(names, args))
    for key, value in kwargs.items():
        if key in bound:
            raise TypeError(
                f"{command}() got multiple values for argument {key!r}"
            )
        bound[key] = value
    return bound


def _bound_float(bound: Mapping[str, _V], key: str) -> float:
    val = bound.get(key)
    if not isinstance(val, (int, float)) or isinstance(val, bool):
        raise TypeError(f"{key!r} expected number, got {type(val).__name__}")
    return float(val)


def _id_lookup(names: list[str], ids: list[str]) -> dict[str, str]:
    """Each affected labware name to its instance id, positionally.

    A name with no id maps to nothing and its record carries an EMPTY id list,
    which folds by name. Putting the NAME in the id field is worse than leaving
    it out: the fold filters on a non-empty id list, so the record would match
    nothing and the labware would read as if it had never been touched.
    """
    return dict(zip(names, ids))


def _bound_sequence_with_parent(
    bound: Mapping[str, _V], key: str,
) -> Sequence["_HasParentNameAndId"]:
    val = bound.get(key)
    if not isinstance(val, Sequence):
        raise TypeError(f"{key!r} expected Sequence, got {type(val).__name__}")
    return val


def _bound_sequence_float(bound: Mapping[str, _V], key: str) -> Sequence[float]:
    val = bound.get(key)
    if not isinstance(val, Sequence):
        raise TypeError(f"{key!r} expected Sequence[float], got {type(val).__name__}")
    return val


def _containers_arg(raw: IContainer | Sequence[IContainer], n_channels: int) -> Sequence[IContainer]:
    """The aspirate/dispense first arg: a single-pool container (trough) expands to
    one-per-channel; a sequence passes through. A single itemized well is rejected
    (pass wells in a list) so a forgotten list does not silently fan one well out."""
    if isinstance(raw, IContainer):
        if raw.position is not None:
            raise TypeError(
                "a single itemized well must be passed in a list; only a single-pool "
                "container broadcasts across channels"
            )
        return [raw] * n_channels
    return raw


def _tracking_position(container: IContainer) -> str:
    """The pseudo-well a container's volume tracks under: its well id, or the
    single-pool key for a standalone container (trough)."""
    return container.position if container.position is not None else TROUGH_WELL_ID


def _bound_optional_float(bound: Mapping[str, _V], key: str) -> float | None:
    val = bound.get(key)
    if val is None:
        return None
    if not isinstance(val, (int, float)) or isinstance(val, bool):
        raise TypeError(f"{key!r} expected number, got {type(val).__name__}")
    return float(val)


# The 96-head API passes ctx.plate(...)/ctx.tip_rack(...) objects, never
# name strings; interpreters narrow with this tuple and read .name.
_TYPED_LABWARE = (IPlate, ITipRack, ITrough)


def _typed_labware_error(got_type_name: str) -> TypeError:
    return TypeError(
        f"expected typed labware (IPlate/ITipRack/ITrough), got {got_type_name}"
    )

from orca.resource_models.tip_runs import consecutive_runs, consecutive_runs_by_parent
from orca.state.mounted import channels_for_slices
from orca.state.records import (
    AspirateDetails,
    Aspirate96Details,
    DeviceOperation,
    DispenseDetails,
    Dispense96Details,
    GenericOperationDetails,
    InitialStateDetails,
    OperationRecord,
    TipDrop96Details,
    TipDiscardDetails,
    TipDropDetails,
    TipPickUp96Details,
    TipPickUpDetails,
    TrackingSource,
)


class _HasParentNameAndId(Protocol):
    @property
    def parent_name(self) -> str: ...
    @property
    def identifier(self) -> str: ...



class LiquidHandlerInterpreter:
    def interpret(
        self,
        command: str,
        args: tuple[_V, ...],
        kwargs: dict[str, _V],
        result: LabwareStateResponse | None,
        device_name: str,
        affected_labware: list[str],
        affected_labware_ids: list[str],
        action_id: str,
        thread_id: str,
    ) -> list[OperationRecord]:
        """Every record this call produced, in the order the channels acted.

        A list because a head reaching across two racks is two facts about two
        labware. Folding them into one record was how a rack got debited for
        another rack's tips.
        """
        if command in _VERB_PARAMS:
            bound = _bind_verb_call(command, args, kwargs)
            if command == "aspirate":
                return self._interpret_aspirate(bound, device_name, affected_labware, affected_labware_ids, action_id, thread_id)
            if command == "dispense":
                return self._interpret_dispense(bound, device_name, affected_labware, affected_labware_ids, action_id, thread_id)
            if command == "pick_up_tips":
                return self._interpret_pick_up_tips(bound, device_name, affected_labware, affected_labware_ids, action_id, thread_id)
            if command == "drop_tips":
                return self._interpret_drop_tips(bound, device_name, affected_labware, affected_labware_ids, action_id, thread_id)
            if command == "discard_tips":
                return self._interpret_discard_tips(bound, device_name, affected_labware, affected_labware_ids, action_id, thread_id)
            if command == "aspirate96":
                return [self._interpret_aspirate96(bound, device_name, affected_labware, affected_labware_ids, action_id, thread_id)]
            if command == "dispense96":
                return [self._interpret_dispense96(bound, device_name, affected_labware, affected_labware_ids, action_id, thread_id)]
            if command == "pick_up_tips96":
                return [self._interpret_pick_up_tips96(bound, device_name, affected_labware, affected_labware_ids, action_id, thread_id)]
            return [self._interpret_drop_tips96(bound, device_name, affected_labware, affected_labware_ids, action_id, thread_id)]
        try:
            op = DeviceOperation(command)
        except ValueError:
            return []
        return [OperationRecord(
            operation=op,
            device_name=device_name,
            affected_labware=affected_labware,
            affected_labware_ids=affected_labware_ids,
            action_id=action_id,
            thread_id=thread_id,
            details=GenericOperationDetails(command=command, args_repr=repr(args)),
            timestamp=time.time(),
        )]

    def claimed_tip_positions(
        self, command: str, args: tuple[_V, ...], kwargs: dict[str, _V],
    ) -> dict[str, list[str]]:
        """Rack name -> requested positions, parsed from the raw call the
        action body made -- before dispatch, not after the driver returns."""
        if command == "pick_up_tips":
            bound = _bind_verb_call(command, args, kwargs)
            tip_spots = _bound_sequence_with_parent(bound, "tip_spots")
            claimed: dict[str, list[str]] = {}
            for rack, positions in consecutive_runs_by_parent(tip_spots):
                claimed.setdefault(rack, []).extend(positions)
            return claimed
        if command == "pick_up_tips96":
            # The 96-head draws from every spot at once; ask the rack for its layout.
            bound = _bind_verb_call(command, args, kwargs)
            rack = bound.get("tip_rack")
            if not isinstance(rack, ITipRack):
                raise _typed_labware_error(type(rack).__name__)
            return {rack.name: [spot.identifier for spot in rack.tip_spots()]}
        return {}

    def _interpret_aspirate(
        self,
        bound: Mapping[str, _V],
        device_name: str,
        affected_labware: list[str],
        affected_labware_ids: list[str],
        action_id: str,
        thread_id: str,
    ) -> list[OperationRecord]:
        """One record per labware the channels reached into.

        A single call can span two plates, and folding them into one record
        debits the first for the second's liquid and leaves the second reading
        untouched. The wire already splits them; the record now agrees.
        """
        volumes = _bound_sequence_float(bound, "volumes")
        raw = bound.get("containers")
        if not isinstance(raw, (IContainer, Sequence)):
            raise TypeError(f"aspirate containers expected container(s), got {type(raw).__name__}")
        containers = _containers_arg(raw, len(volumes))
        by_name = _id_lookup(affected_labware, affected_labware_ids)
        paired = list(zip(containers, volumes))
        now = time.time()
        return [
            OperationRecord(
                operation=DeviceOperation.ASPIRATE,
                device_name=device_name,
                affected_labware=[labware],
                affected_labware_ids=(
                    [by_name[labware]] if labware in by_name else []
                ),
                action_id=action_id,
                thread_id=thread_id,
                details=AspirateDetails(
                    labware=labware,
                    positions=[_tracking_position(c) for c, _ in run],
                    volumes=[v for _, v in run],
                ),
                timestamp=now,
            )
            for labware, run in consecutive_runs(
                paired, lambda pair: pair[0].resource_name,
            )
        ]

    def _interpret_dispense(
        self,
        bound: Mapping[str, _V],
        device_name: str,
        affected_labware: list[str],
        affected_labware_ids: list[str],
        action_id: str,
        thread_id: str,
    ) -> list[OperationRecord]:
        """One record per labware the channels reached into.

        A single call can span two plates, and folding them into one record
        debits the first for the second's liquid and leaves the second reading
        untouched. The wire already splits them; the record now agrees.
        """
        volumes = _bound_sequence_float(bound, "volumes")
        raw = bound.get("containers")
        if not isinstance(raw, (IContainer, Sequence)):
            raise TypeError(f"dispense containers expected container(s), got {type(raw).__name__}")
        containers = _containers_arg(raw, len(volumes))
        by_name = _id_lookup(affected_labware, affected_labware_ids)
        paired = list(zip(containers, volumes))
        now = time.time()
        return [
            OperationRecord(
                operation=DeviceOperation.DISPENSE,
                device_name=device_name,
                affected_labware=[labware],
                affected_labware_ids=(
                    [by_name[labware]] if labware in by_name else []
                ),
                action_id=action_id,
                thread_id=thread_id,
                details=DispenseDetails(
                    labware=labware,
                    positions=[_tracking_position(c) for c, _ in run],
                    volumes=[v for _, v in run],
                ),
                timestamp=now,
            )
            for labware, run in consecutive_runs(
                paired, lambda pair: pair[0].resource_name,
            )
        ]

    def _interpret_pick_up_tips(
        self,
        bound: Mapping[str, _V],
        device_name: str,
        affected_labware: list[str],
        affected_labware_ids: list[str],
        action_id: str,
        thread_id: str,
    ) -> list[OperationRecord]:
        """One record per rack the head reached into.

        Each names only its own rack as affected: a record that debits rack A
        must not claim to have touched rack B, or an audit of one rack reads
        operations that never moved its tips.
        """
        tip_spots = _bound_sequence_with_parent(bound, "tip_spots")
        by_name = _id_lookup(affected_labware, affected_labware_ids)
        now = time.time()
        runs = consecutive_runs_by_parent(tip_spots)
        channels, counted = channels_for_slices(
            [len(positions) for _, positions in runs], None,
        )
        return [
            OperationRecord(
                operation=DeviceOperation.PICK_UP_TIPS,
                device_name=device_name,
                affected_labware=[rack],
                affected_labware_ids=(
                    [by_name[rack]] if rack in by_name else []
                ),
                action_id=action_id,
                thread_id=thread_id,
                details=TipPickUpDetails(
                    tip_rack=rack, positions=positions,
                    use_channels=engaged, channels_were_counted=counted,
                ),
                timestamp=now,
            )
            for (rack, positions), engaged in zip(runs, channels)
        ]

    def _interpret_drop_tips(
        self,
        bound: Mapping[str, _V],
        device_name: str,
        affected_labware: list[str],
        affected_labware_ids: list[str],
        action_id: str,
        thread_id: str,
    ) -> list[OperationRecord]:
        """One record per rack, so tips come back to the rack they left."""
        tip_spots = _bound_sequence_with_parent(bound, "tip_spots")
        by_name = _id_lookup(affected_labware, affected_labware_ids)
        now = time.time()
        runs = consecutive_runs_by_parent(tip_spots)
        channels, _ = channels_for_slices(
            [len(positions) for _, positions in runs], None,
        )
        return [
            OperationRecord(
                operation=DeviceOperation.DROP_TIPS,
                device_name=device_name,
                affected_labware=[rack],
                affected_labware_ids=(
                    [by_name[rack]] if rack in by_name else []
                ),
                action_id=action_id,
                thread_id=thread_id,
                details=TipDropDetails(
                    tip_rack=rack,
                    positions=positions,
                    use_channels=engaged,
                    # The bridge always returns dropped tips to their rack
                    # (DropTipsRequest(to_waste=False)); discard_tips is the waste verb.
                    to_waste=False,
                ),
                timestamp=now,
            )
            for (rack, positions), engaged in zip(runs, channels)
        ]

    def _interpret_discard_tips(
        self,
        bound: Mapping[str, _V],
        device_name: str,
        affected_labware: list[str],
        affected_labware_ids: list[str],
        action_id: str,
        thread_id: str,
    ) -> list[OperationRecord]:
        """A discard names no rack: the tips go to waste and stop existing.

        Without its own record the head kept reporting them mounted forever.
        """
        channels = bound.get("use_channels")
        return [OperationRecord(
            operation=DeviceOperation.DISCARD_TIPS,
            device_name=device_name,
            affected_labware=affected_labware,
            affected_labware_ids=affected_labware_ids,
            action_id=action_id,
            thread_id=thread_id,
            details=TipDiscardDetails(
                use_channels=list(channels) if isinstance(channels, list) else None,
            ),
            timestamp=time.time(),
        )]

    # --- 96-head interpreters ---

    def _interpret_aspirate96(
        self,
        bound: Mapping[str, _V],
        device_name: str,
        affected_labware: list[str],
        affected_labware_ids: list[str],
        action_id: str,
        thread_id: str,
    ) -> OperationRecord:
        labware = bound.get("labware")
        if not isinstance(labware, _TYPED_LABWARE):
            raise _typed_labware_error(type(labware).__name__)
        return OperationRecord(
            operation=DeviceOperation.ASPIRATE96,
            device_name=device_name,
            affected_labware=affected_labware,
            affected_labware_ids=affected_labware_ids,
            action_id=action_id,
            thread_id=thread_id,
            details=Aspirate96Details(
                labware=labware.name,
                volume=_bound_float(bound, "volume"),
                flow_rate=_bound_optional_float(bound, "flow_rate"),
                liquid_height=_bound_optional_float(bound, "liquid_height"),
            ),
            timestamp=time.time(),
        )

    def _interpret_dispense96(
        self,
        bound: Mapping[str, _V],
        device_name: str,
        affected_labware: list[str],
        affected_labware_ids: list[str],
        action_id: str,
        thread_id: str,
    ) -> OperationRecord:
        labware = bound.get("labware")
        if not isinstance(labware, _TYPED_LABWARE):
            raise _typed_labware_error(type(labware).__name__)
        return OperationRecord(
            operation=DeviceOperation.DISPENSE96,
            device_name=device_name,
            affected_labware=affected_labware,
            affected_labware_ids=affected_labware_ids,
            action_id=action_id,
            thread_id=thread_id,
            details=Dispense96Details(
                labware=labware.name,
                volume=_bound_float(bound, "volume"),
                flow_rate=_bound_optional_float(bound, "flow_rate"),
                liquid_height=_bound_optional_float(bound, "liquid_height"),
            ),
            timestamp=time.time(),
        )

    def _interpret_pick_up_tips96(
        self,
        bound: Mapping[str, _V],
        device_name: str,
        affected_labware: list[str],
        affected_labware_ids: list[str],
        action_id: str,
        thread_id: str,
    ) -> OperationRecord:
        rack = bound.get("tip_rack")
        if not isinstance(rack, _TYPED_LABWARE):
            raise _typed_labware_error(type(rack).__name__)
        return OperationRecord(
            operation=DeviceOperation.PICK_UP_TIPS96,
            device_name=device_name,
            affected_labware=affected_labware,
            affected_labware_ids=affected_labware_ids,
            action_id=action_id,
            thread_id=thread_id,
            details=TipPickUp96Details(tip_rack=rack.name),
            timestamp=time.time(),
        )

    def _interpret_drop_tips96(
        self,
        bound: Mapping[str, _V],
        device_name: str,
        affected_labware: list[str],
        affected_labware_ids: list[str],
        action_id: str,
        thread_id: str,
    ) -> OperationRecord:
        raw_rack = bound.get("tip_rack")
        if raw_rack is not None and not isinstance(raw_rack, _TYPED_LABWARE):
            raise _typed_labware_error(type(raw_rack).__name__)
        return OperationRecord(
            operation=DeviceOperation.DROP_TIPS96,
            device_name=device_name,
            affected_labware=affected_labware,
            affected_labware_ids=affected_labware_ids,
            action_id=action_id,
            thread_id=thread_id,
            details=TipDrop96Details(
                tip_rack=None if raw_rack is None else raw_rack.name,
                # Mirrors the bridge: a given rack receives the tips back,
                # no rack means waste (there is no to_waste parameter).
                to_waste=raw_rack is None,
            ),
            timestamp=time.time(),
        )

    # --- Per-channel outcome interpretation ---

    def interpret_per_channel_outcomes(
        self,
        command: str,
        args: tuple[_V, ...],
        kwargs: dict[str, _V],
        result: LabwareStateResponse | None,
        device_name: str,
        affected_labware: list[str],
        affected_labware_ids: list[str],
        action_id: str,
        thread_id: str,
    ) -> list[OperationRecord]:
        """Emit one record per channel/well when the result carries per_channel_errors.

        On full success (per_channel_errors is empty) returns []; the action
        body then emits the consolidated AspirateDetails / DispenseDetails
        record via ``interpret`` instead. On partial failure, every well in
        the request gets its own record so ops_history can be queried by
        per-well certainty.
        """
        if not isinstance(result, LabwareStateResponse):
            return []
        if not result.per_channel_errors:
            return []
        if command == "aspirate":
            return self._per_channel_records(
                "aspirate", DeviceOperation.ASPIRATE, AspirateDetails,
                _bind_verb_call(command, args, kwargs), result.per_channel_errors,
                device_name, affected_labware, affected_labware_ids, action_id, thread_id,
            )
        if command == "dispense":
            return self._per_channel_records(
                "dispense", DeviceOperation.DISPENSE, DispenseDetails,
                _bind_verb_call(command, args, kwargs), result.per_channel_errors,
                device_name, affected_labware, affected_labware_ids, action_id, thread_id,
            )
        # Per-channel translation for tip ops / 96-head is deferred (the
        # cheshire-drivers PLR wrapper does not catch ChannelizedError on
        # those paths today). If a future driver populates per_channel_errors
        # on a command we haven't extended, log loud rather than silently
        # drop the attribution.
        logger.warning(
            "interpret_per_channel_outcomes: result has per_channel_errors for "
            "command=%r but the interpreter has no per-channel branch for it; "
            "%d error record(s) dropped.",
            command, len(result.per_channel_errors),
        )
        return []

    def _attribute_channels(
        self,
        command: str,
        containers: Sequence[IContainer],
        volumes: Sequence[float],
        per_channel_errors: list[ChannelError],
    ) -> list[tuple[str, str, float, ChannelError | None]]:
        """Per-channel (labware, position, volume, error|None) attribution.

        Itemized labware (plate) attributes an error to the channel whose well it
        names. A single-pool container (trough) shares the "A1" key across all its
        channels, so position cannot tell them apart; attribute those by COUNT
        instead (k errors mark k of the container's channels not-transferred, the
        rest transferred). That keeps the pool's net volume correct; position-based
        attribution would zero EVERY channel on any single failure and overstate
        the pool.

        The k failed records are the first k channels in iteration order, NOT the
        physical channel_id that errored: container per-channel identity is
        intentionally dropped, because a shared pool has no per-channel volume to
        attribute. Do not key off a container record's channel for identity.
        """
        container_labwares = {c.resource_name for c in containers if c.position is None}
        pending_container_errors: dict[str, list[ChannelError]] = {}
        position_errors: dict[tuple[str, str], ChannelError] = {}
        for e in per_channel_errors:
            if e.labware in container_labwares:
                pending_container_errors.setdefault(e.labware, []).append(e)
            else:
                key = (e.labware, e.well_position if e.well_position is not None else TROUGH_WELL_ID)
                position_errors[key] = e
        request_keys = {(c.resource_name, _tracking_position(c)) for c in containers}
        _warn_orphan_channel_errors(command, per_channel_errors, request_keys)
        out: list[tuple[str, str, float, ChannelError | None]] = []
        for container, vol in zip(containers, volumes):
            pos = _tracking_position(container)
            if container.resource_name in container_labwares:
                queue = pending_container_errors.get(container.resource_name)
                err = queue.pop(0) if queue else None
            else:
                err = position_errors.get((container.resource_name, pos))
            out.append((container.resource_name, pos, float(vol), err))
        return out

    def _per_channel_records(
        self,
        verb: str,
        operation: DeviceOperation,
        details_model: type[AspirateDetails] | type[DispenseDetails],
        bound: Mapping[str, _V],
        per_channel_errors: list[ChannelError],
        device_name: str,
        affected_labware: list[str],
        affected_labware_ids: list[str],
        action_id: str,
        thread_id: str,
    ) -> list[OperationRecord]:
        """One record per channel, each naming only the labware it touched.

        Aspirate and dispense differ in the operation and the details class and
        in nothing else, so they share this. A channel that errored records a
        zero move rather than no record: "we tried and it did not go" is a
        different thing from silence, and only the first lets a later reader see
        the attempt.
        """
        volumes = _bound_sequence_float(bound, "volumes")
        raw = bound.get("containers")
        if not isinstance(raw, (IContainer, Sequence)):
            raise TypeError(
                f"{verb} containers expected container(s), got {type(raw).__name__}"
            )
        containers = _containers_arg(raw, len(volumes))
        by_name = _id_lookup(affected_labware, affected_labware_ids)
        ts = time.time()
        records: list[OperationRecord] = []
        for labware, pos, vol, err in self._attribute_channels(
            verb, containers, volumes, per_channel_errors,
        ):
            if err is not None:
                details = details_model(
                    labware=labware, positions=[pos], volumes=[0.0],
                    certainty="definitely_not_transferred", error_code=err.error_code,
                )
            else:
                details = details_model(
                    labware=labware, positions=[pos], volumes=[vol],
                    certainty="confirmed_transferred",
                )
            records.append(OperationRecord(
                operation=operation,
                device_name=device_name,
                affected_labware=[labware],
                affected_labware_ids=(
                    [by_name[labware]] if labware in by_name else []
                ),
                action_id=action_id,
                thread_id=thread_id,
                details=details,
                timestamp=ts,
            ))
        return records

    # --- Driver-state interpretation ---

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
        """Emit DRIVER_OBSERVED records from a LabwareStateResponse.

        Returns an empty list when the result is not a LabwareStateResponse or
        when its labware_state is empty (driver does not provide state, or the
        bridge stripped it because the user did not opt into trust_driver_state).
        Otherwise emits one INITIAL_STATE record per labware in labware_state,
        carrying the driver's reported per-well volumes and per-tip-spot
        presence as InitialStateDetails. Each record's source is DRIVER_OBSERVED.

        The record says what the driver reported, never what the call was. A
        snapshot used to carry the triggering command's verb, so the deck's
        plate got a `pick_up_tips` entry for a pick that happened to the rack,
        and `op_count` counted a post-aspirate snapshot as another aspirate.
        The action id is what ties a snapshot to the call that produced it.
        """
        if not isinstance(result, LabwareStateResponse):
            return []
        if not result.labware_state:
            return []
        del command
        ts = time.time()
        records: list[OperationRecord] = []
        for labware_name, well_state in result.labware_state.items():
            tip_positions: list[str] | None = None
            if well_state.tips is not None:
                tip_positions = [pos for pos, present in well_state.tips.items() if present]
            records.append(
                OperationRecord(
                    operation=DeviceOperation.INITIAL_STATE,
                    device_name=device_name,
                    affected_labware=[labware_name],
                    action_id=action_id,
                    thread_id=thread_id,
                    details=InitialStateDetails(
                        labware=labware_name,
                        well_volumes=dict(well_state.volumes) if well_state.volumes is not None else None,
                        tip_positions_present=tip_positions,
                    ),
                    timestamp=ts,
                    source=TrackingSource.DRIVER_OBSERVED,
                )
            )
        return records


def _warn_orphan_channel_errors(
    command: str,
    per_channel_errors: list[ChannelError],
    request_keys: set[tuple[str, str]],
) -> None:
    """Log a warning when a ChannelError targets a (labware, position) not in the request.

    Surfaces driver/wrapper bugs where the per_channel_errors list and the
    request's wells disagree -- e.g., the wrapper's channel-to-well mapping
    desyncs from what the action body sees. Without this warning the
    interpreter would silently produce N records all flagged as
    confirmed_transferred even though the response said the call partially
    failed; nothing fails loud.
    """
    for err in per_channel_errors:
        pos = err.well_position if err.well_position is not None else TROUGH_WELL_ID
        if (err.labware, pos) not in request_keys:
            logger.warning(
                "interpret_per_channel_outcomes: orphan ChannelError on "
                "command=%r -- channel %d targets %s/%s which is not in the "
                "request's wells. Per-channel attribution will be incomplete.",
                command, err.channel_id, err.labware, err.well_position,
            )

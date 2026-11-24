"""The typed request behind each liquid-handler bridge call.

One place turns a bridge call's arguments into the request that goes on the
wire. The bridge builds it to send; the ledger builds it to read what the call
did.

Every builder takes the bridge method's own parameters, in its own order, so a
signature change shows up here rather than silently in one of the two readers.
"""

from typing import Sequence

from cheshire_drivers.labware_interfaces import (
    IContainer,
    IPlate,
    ITipRack,
    ITipSpot,
    ITrough,
)
from cheshire_drivers.liquid_handler_models import (
    Aspirate96Request,
    AspirateRequest,
    AspirateTarget,
    DiscardTipsRequest,
    Dispense96Request,
    DispenseRequest,
    DispenseTarget,
    DropTips96Request,
    DropTipsRequest,
    MixRequest,
    PickUpTips96Request,
    PickUpTipsRequest,
    PipettingPatch,
    ReturnTips96Request,
    TipPick,
)
from cheshire_drivers.pipetting import MixParams, PipettingProfile

from orca.resource_models.tip_runs import consecutive_runs, consecutive_runs_by_parent
from orca.devices.pipetting_parameters import resolve_pipetting


def as_container_sequence(
    containers: IContainer | Sequence[IContainer], volumes: list[float],
) -> Sequence[IContainer]:
    """Expand a single-pool container (trough) to one-per-channel, matching PLR;
    a sequence passes through. A single itemized well is rejected so a forgotten
    list does not silently fan one well across every channel -- pass wells in a
    list ([well]); only a no-position container broadcasts."""
    if isinstance(containers, IContainer):
        if containers.position is not None:
            raise TypeError(
                "aspirate/dispense: a single itemized well must be passed in a list "
                "(e.g. [well]); only a single-pool container broadcasts across channels"
            )
        return [containers] * len(volumes)
    return containers


def _group_tip_spots_by_parent(tip_spots: list[ITipSpot]) -> list[TipPick]:
    """One TipPick per consecutive same-rack run, in channel order."""
    return [
        TipPick(tip_rack=rack, positions=positions)
        for rack, positions in consecutive_runs_by_parent(tip_spots)
    ]


def _group_channels_by_parent(
    containers: Sequence[IContainer], volumes: list[float],
) -> list[tuple[str, list[str] | None, list[float]]]:
    """Group consecutive channels sharing one deck resource into wire slices.

    Each group is ``(resource_name, positions, volumes)``, where ``positions``
    is the per-channel well ids for an itemized labware, or None for a single
    container (a trough) whose channels carry no sub-position.
    """
    return [
        (
            name,
            None if paired[0][0].position is None
            else [c.position for c, _ in paired if c.position is not None],
            [vol for _, vol in paired],
        )
        for name, paired in consecutive_runs(
            list(zip(containers, volumes)), lambda pair: pair[0].resource_name,
        )
    ]


def _require_matching_lengths(
    verb: str, containers: Sequence[IContainer], volumes: list[float],
) -> None:
    if len(containers) != len(volumes):
        raise ValueError(
            f"{verb}: containers ({len(containers)}) and volumes ({len(volumes)}) "
            f"must be the same length"
        )


def aspirate_request(
    containers: IContainer | Sequence[IContainer],
    volumes: list[float],
    flow_rates: list[float] | None = None,
    offsets_z: list[float] | None = None,
    use_channels: list[int] | None = None,
    liquid_class: PipettingProfile | None = None,
    technique: PipettingProfile | None = None,
) -> AspirateRequest:
    expanded = as_container_sequence(containers, volumes)
    _require_matching_lengths("aspirate", expanded, volumes)
    return AspirateRequest(
        aspirations=[
            AspirateTarget(labware=name, positions=positions, volumes=vols)
            for name, positions, vols in _group_channels_by_parent(expanded, volumes)
        ],
        flow_rates=flow_rates,
        offsets_z=offsets_z,
        use_channels=use_channels,
        parameters=resolve_pipetting(liquid_class, technique),
    )


def dispense_request(
    containers: IContainer | Sequence[IContainer],
    volumes: list[float],
    flow_rates: list[float] | None = None,
    offsets_z: list[float] | None = None,
    use_channels: list[int] | None = None,
    liquid_class: PipettingProfile | None = None,
    technique: PipettingProfile | None = None,
) -> DispenseRequest:
    expanded = as_container_sequence(containers, volumes)
    _require_matching_lengths("dispense", expanded, volumes)
    return DispenseRequest(
        dispenses=[
            DispenseTarget(labware=name, positions=positions, volumes=vols)
            for name, positions, vols in _group_channels_by_parent(expanded, volumes)
        ],
        flow_rates=flow_rates,
        offsets_z=offsets_z,
        use_channels=use_channels,
        parameters=resolve_pipetting(liquid_class, technique),
    )


def pick_up_tips_request(tip_spots: list[ITipSpot]) -> PickUpTipsRequest:
    if not isinstance(tip_spots, list) or not all(isinstance(spot, ITipSpot) for spot in tip_spots):
        raise TypeError(f"tip_spots expected a list of tip spots, got {type(tip_spots).__name__}")
    return PickUpTipsRequest(picks=_group_tip_spots_by_parent(tip_spots))


def drop_tips_request(tip_spots: list[ITipSpot]) -> DropTipsRequest:
    """The bridge always returns dropped tips to their rack; discard is the waste verb."""
    return DropTipsRequest(drops=_group_tip_spots_by_parent(tip_spots), to_waste=False)


def discard_tips_request(use_channels: list[int] | None = None) -> DiscardTipsRequest:
    return DiscardTipsRequest(use_channels=use_channels)


def mix_request(
    containers: IContainer | Sequence[IContainer],
    params: MixParams,
    use_channels: list[int] | None = None,
    liquid_class: PipettingProfile | None = None,
    technique: PipettingProfile | None = None,
) -> MixRequest:
    """A mix runs in place, so one labware takes every channel it engages."""
    if isinstance(containers, IContainer):
        labware = containers.resource_name
        positions = None if containers.position is None else [containers.position]
    else:
        seq = list(containers)
        if not seq:
            raise ValueError("mix: containers must not be empty")
        labware = seq[0].resource_name
        positions = (
            None if seq[0].position is None
            else [c.position for c in seq if c.position is not None]
        )
    return MixRequest(
        labware=labware,
        positions=positions,
        volume=params.volume,
        repetitions=params.repetitions,
        parameters=PipettingPatch(flow_rate=params.flow_rate).apply_to(
            resolve_pipetting(liquid_class, technique)
        ),
        use_channels=use_channels,
    )


def aspirate96_request(
    labware: IPlate | ITrough,
    volume: float,
    flow_rate: float | None = None,
    liquid_height: float | None = None,
) -> Aspirate96Request:
    return Aspirate96Request(
        labware=labware.name,
        volume=volume,
        flow_rate=flow_rate,
        liquid_height=liquid_height,
    )


def dispense96_request(
    labware: IPlate | ITrough,
    volume: float,
    flow_rate: float | None = None,
    liquid_height: float | None = None,
) -> Dispense96Request:
    return Dispense96Request(
        labware=labware.name,
        volume=volume,
        flow_rate=flow_rate,
        liquid_height=liquid_height,
    )


def pick_up_tips96_request(tip_rack: ITipRack) -> PickUpTips96Request:
    return PickUpTips96Request(tip_rack=tip_rack.name)


def drop_tips96_request(tip_rack: ITipRack | None = None) -> DropTips96Request:
    """A named rack receives the tips back; no rack means waste."""
    return DropTips96Request(
        tip_rack=None if tip_rack is None else tip_rack.name,
        to_waste=tip_rack is None,
    )


def return_tips96_request() -> ReturnTips96Request:
    return ReturnTips96Request()

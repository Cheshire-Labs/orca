import asyncio
import logging
import time
from typing import Any, Callable, ClassVar, Protocol, Sequence, runtime_checkable

from cheshire_drivers.interfaces import (
    IDelidderDriver, ILiquidHandlerWithProtocolDriver, IPlateWasherDriver,
    IReaderDriver, IStorageDriver, IWasteDriver,
)
from cheshire_drivers.delidder_models import DelidRequest
from cheshire_drivers.gantry_models import GantryParkPosition, ParkGantryRequest
from cheshire_drivers.protocol_runner_models import RunProtocolRequest
from cheshire_drivers.reader_models import ReadRequest
from cheshire_drivers.labware_interfaces import IContainer, IPlate, ITipRack, ITipSpot, ITrough
from cheshire_drivers.pipetting import MixParams, PipettingProfile
from cheshire_drivers.liquid_handler_models import (
    AspirateRequest, AspirateTarget, Aspirate96Request, DeckLayoutConfig,
    DiscardStrandedTipsRequest,
    DiscardTipsRequest,
    DispenseRequest, DispenseTarget, Dispense96Request, DropTipsRequest, DropTips96Request,
    GetDeckStateRequest,
    LabwareStateResponse, LabwareWellState, MixRequest, PipettingPatch,
    PickUpTipsRequest, PickUpTips96Request,
    ReconcileHardwareStateRequest, ReconcileHardwareStateResponse,
    AddDeckLabwareRequest, ResetDeckLabwareRequest, RemoveDeckLabwareRequest,
    ReturnTips96Request, TipPick,
)
from cheshire_drivers.sims import (
    SimDelidderDriver, SimLiquidHandlerWithProtocolDriver, SimPlateWasherDriver,
    SimReaderDriver, SimStorageDriver, SimWasteDriver,
)
from orca.devices.driver_refusal import is_busy_refusal
from orca.devices.device_interfaces import (
    IDelidder, ILiquidHandler, IPlateSource, IPlateWasher, IReader, IStorage, IWaste,
)
from orca.resource_models.devices import Device
from orca.resource_models.labware_placeable_interface import IPlateMover
from orca.resource_models.tracked_lock import TrackedLock
from orca.resource_models.transporter_base import TransporterBase
from orca.resource_models.labware import LabwareInstance, PlateInstance
from orca.resource_models.tip_runs import (
    consecutive_runs,
    consecutive_runs_by_parent,
)
from orca.runtime.deck_layout_store import NullDeckLayoutStore
from orca.runtime.device_factory_context import resolve_drivers
from orca.runtime.interfaces import IDeckLayoutStore
from orca.runtime.pipetting_parameters import resolve_pipetting
from orca.runtime.run_modes import WorkflowRunMode

orca_logger = logging.getLogger("orca")

# How long an arm waits for a handler to finish what it is holding before giving
# up and telling an operator. Long enough for a pipetting sequence to run its
# course (tips go on and come off within one method, not across an incubation),
# short enough that tips left on by a method that ended are not an endless wait.
STEP_ASIDE_PATIENCE_SECONDS: float = 900.0
_STEP_ASIDE_RETRY_SECONDS: float = 2.0



def _group_tip_spots_by_parent(tip_spots: list[ITipSpot]) -> list[TipPick]:
    """One TipPick per consecutive same-rack run, in channel order.

    Shares its grouping with the ledger record. They were written twice and
    drifted: the wire split a two-rack pick correctly while the record wrote
    every position under the first rack, so one rack was debited for the
    other's tips.
    """
    return [
        TipPick(tip_rack=rack, positions=positions)
        for rack, positions in consecutive_runs_by_parent(tip_spots)
    ]


def _group_channels_by_parent(
    containers: Sequence[IContainer], volumes: list[float],
) -> list[tuple[str, list[str] | None, list[float]]]:
    """Group consecutive channels sharing one deck resource into wire slices.

    Same runs as the ledger record, through the same helper. Written twice they
    drifted once already, in exactly this shape: the wire split a two-labware
    call correctly and the record put everything under the first name.

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


def _as_container_sequence(
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


class PlateWasher(Device[IPlateWasherDriver], IPlateWasher):
    KIND: ClassVar[str] = "plate_washer"

    def __init__(
        self,
        name: str,
        sim_override: WorkflowRunMode | None = None,
    ) -> None:
        live, sim_drv = resolve_drivers(
            self.KIND, name, SimPlateWasherDriver,
        )
        super().__init__(name, live, sim_drv, sim_override=sim_override)
        
    async def run_protocol(self, protocol_filepath: str, params: dict[str, Any]) -> None:
        await self.driver.run_protocol(
            RunProtocolRequest(protocol_filepath=protocol_filepath, params=params)
        )

    

def _deck_modeling_sim_factory() -> Callable[[str], ILiquidHandlerWithProtocolDriver]:
    """Name-ignoring factory for the deck-modeling Chatterbox sim slot.

    `resolve_drivers` calls `default_sim_factory(name)` on the no-bound-factory
    pure-sim path, but the PLR wrapper owns a deck tree and takes no name. Lazy
    import keeps pylabrobot off the deckless-handler import path.
    """
    from cheshire_drivers.plr import ChatterboxLiquidHandlerWithProtocolDriver

    def _make(_name: str) -> ILiquidHandlerWithProtocolDriver:
        return ChatterboxLiquidHandlerWithProtocolDriver()

    return _make


class _LiquidHandlerBase(Device[ILiquidHandlerWithProtocolDriver], ILiquidHandler):
    """Shared base for liquid handlers: well-level verbs + run_protocol.

    Not author-facing. Concrete handlers are `LiquidHandler` (PLR handler with
    an orca-modeled deck and internal gripper) and `LiquidHandlerProtocol`
    (generic handler with no orca-modeled deck). Which capabilities a handler
    exposes is decided by its bound driver's `interfaces` ClassVar, not the
    class: `run_protocol` succeeds only when the driver advertises
    `IProtocolRunner`, well-level verbs only when it advertises `ILiquidHandler`.

    trust_driver_state: when True AND the driver advertises ``provides_state``,
    the driver's LabwareStateResponse passes through so the interpreter emits
    DRIVER_OBSERVED records; otherwise labware_state is stripped and trackers
    fall back to operation-derived bookkeeping. ``per_channel_errors`` always
    survives (outcome data, not state observation).
    """

    KIND: ClassVar[str] = "liquid_handler"

    # Deck-modeling handlers get a real Chatterbox sim deck (PURE_SIM
    # exercises occupancy); deckless handlers keep the no-op sim.
    _DECK_MODELING: ClassVar[bool] = False

    def __init__(
        self,
        name: str,
        sim: bool = False,
        sim_override: WorkflowRunMode | None = None,
        trust_driver_state: bool = True,
        site_names: list[str] | None = None,
    ) -> None:
        default_sim = _deck_modeling_sim_factory() if self._DECK_MODELING else (
            SimLiquidHandlerWithProtocolDriver
        )
        live, sim_drv = resolve_drivers(
            self.KIND, name, default_sim, deck_modeling=self._DECK_MODELING,
        )
        super().__init__(
            name, live, sim_drv, sim_override=sim_override, site_names=site_names,
        )
        # Effective trust = user opt-in AND the live driver advertising
        # `provides_state`; the factory supplies that driver, so we read it here.
        self._trust_driver_state = trust_driver_state and getattr(live, "provides_state", False)

    @property
    def trust_driver_state(self) -> bool:
        return self._trust_driver_state

    def _filter_state(self, response: LabwareStateResponse) -> LabwareStateResponse:
        """Strip labware_state when the user has not opted into driver state.

        Returns the response unchanged when trust_driver_state is True; otherwise
        returns a response with empty labware_state so the interpreter does not
        emit DRIVER_OBSERVED records. ``per_channel_errors`` is always preserved:
        per-channel partial-failure attribution is outcome data, not driver-state
        observation, so it must reach the interpreter regardless of trust setting.
        """
        if self._trust_driver_state:
            return response
        return LabwareStateResponse(
            success=response.success,
            per_channel_errors=response.per_channel_errors,
        )

    async def run_protocol(self, protocol_filepath: str, params: dict[str, Any]) -> None:
        await self.driver.run_protocol(
            RunProtocolRequest(protocol_filepath=protocol_filepath, params=params)
        )

    async def aspirate(
        self,
        containers: IContainer | Sequence[IContainer],
        volumes: list[float],
        flow_rates: list[float] | None = None,
        offsets_z: list[float] | None = None,
        use_channels: list[int] | None = None,
        liquid_class: PipettingProfile | None = None,
        technique: PipettingProfile | None = None,
    ) -> LabwareStateResponse:
        containers = _as_container_sequence(containers, volumes)
        if len(containers) != len(volumes):
            raise ValueError(
                f"aspirate: containers ({len(containers)}) and volumes ({len(volumes)}) "
                f"must be the same length"
            )
        aspirations = [
            AspirateTarget(labware=name, positions=positions, volumes=vols)
            for name, positions, vols in _group_channels_by_parent(containers, volumes)
        ]
        request = AspirateRequest(
            aspirations=aspirations,
            flow_rates=flow_rates,
            offsets_z=offsets_z,
            use_channels=use_channels,
            parameters=resolve_pipetting(liquid_class, technique),
        )
        return self._filter_state(await self.driver.aspirate(request))

    async def dispense(
        self,
        containers: IContainer | Sequence[IContainer],
        volumes: list[float],
        flow_rates: list[float] | None = None,
        offsets_z: list[float] | None = None,
        use_channels: list[int] | None = None,
        liquid_class: PipettingProfile | None = None,
        technique: PipettingProfile | None = None,
    ) -> LabwareStateResponse:
        containers = _as_container_sequence(containers, volumes)
        if len(containers) != len(volumes):
            raise ValueError(
                f"dispense: containers ({len(containers)}) and volumes ({len(volumes)}) "
                f"must be the same length"
            )
        dispenses = [
            DispenseTarget(labware=name, positions=positions, volumes=vols)
            for name, positions, vols in _group_channels_by_parent(containers, volumes)
        ]
        request = DispenseRequest(
            dispenses=dispenses,
            flow_rates=flow_rates,
            offsets_z=offsets_z,
            use_channels=use_channels,
            parameters=resolve_pipetting(liquid_class, technique),
        )
        return self._filter_state(await self.driver.dispense(request))

    async def pick_up_tips(self, tip_spots: list[ITipSpot]) -> LabwareStateResponse:
        request = PickUpTipsRequest(picks=_group_tip_spots_by_parent(tip_spots))
        return self._filter_state(await self.driver.pick_up_tips(request))

    async def drop_tips(self, tip_spots: list[ITipSpot]) -> LabwareStateResponse:
        request = DropTipsRequest(
            drops=_group_tip_spots_by_parent(tip_spots),
            to_waste=False,
        )
        return self._filter_state(await self.driver.drop_tips(request))

    async def discard_tips(self, use_channels: list[int] | None = None) -> LabwareStateResponse:
        request = DiscardTipsRequest(use_channels=use_channels)
        return self._filter_state(await self.driver.discard_tips(request))

    async def reconcile_hardware_state(self) -> ReconcileHardwareStateResponse:
        return await self.driver.reconcile_hardware_state(ReconcileHardwareStateRequest())

    async def discard_stranded_tips(self) -> ReconcileHardwareStateResponse:
        return await self.driver.discard_stranded_tips(DiscardStrandedTipsRequest())

    async def mix(
        self,
        containers: IContainer | Sequence[IContainer],
        params: MixParams,
        use_channels: list[int] | None = None,
        liquid_class: PipettingProfile | None = None,
        technique: PipettingProfile | None = None,
    ) -> LabwareStateResponse:
        if isinstance(containers, IContainer):
            labware = containers.resource_name
            positions = None if containers.position is None else [containers.position]
        else:
            seq = list(containers)
            if not seq:
                raise ValueError("mix: containers must not be empty")
            labware = seq[0].resource_name
            positions = None if seq[0].position is None else [c.position for c in seq if c.position is not None]
        request = MixRequest(
            labware=labware,
            positions=positions,
            volume=params.volume,
            repetitions=params.repetitions,
            parameters=PipettingPatch(flow_rate=params.flow_rate).apply_to(
                resolve_pipetting(liquid_class, technique)
            ),
            use_channels=use_channels,
        )
        return self._filter_state(await self.driver.mix(request))

    # --- 96-head bridge methods ---

    async def aspirate96(
        self,
        labware: IPlate | ITrough,
        volume: float,
        flow_rate: float | None = None,
        liquid_height: float | None = None,
    ) -> LabwareStateResponse:
        response = await self.driver.aspirate96(Aspirate96Request(
            labware=labware.name,
            volume=volume,
            flow_rate=flow_rate,
            liquid_height=liquid_height,
        ))
        return self._filter_state(response)

    async def dispense96(
        self,
        labware: IPlate | ITrough,
        volume: float,
        flow_rate: float | None = None,
        liquid_height: float | None = None,
    ) -> LabwareStateResponse:
        response = await self.driver.dispense96(Dispense96Request(
            labware=labware.name,
            volume=volume,
            flow_rate=flow_rate,
            liquid_height=liquid_height,
        ))
        return self._filter_state(response)

    async def pick_up_tips96(self, tip_rack: ITipRack) -> LabwareStateResponse:
        response = await self.driver.pick_up_tips96(PickUpTips96Request(
            tip_rack=tip_rack.name,
        ))
        return self._filter_state(response)

    async def drop_tips96(
        self,
        tip_rack: ITipRack | None = None,
    ) -> LabwareStateResponse:
        to_waste = tip_rack is None
        response = await self.driver.drop_tips96(DropTips96Request(
            tip_rack=tip_rack.name if tip_rack is not None else None,
            to_waste=to_waste,
        ))
        return self._filter_state(response)

    async def return_tips96(self) -> LabwareStateResponse:
        return self._filter_state(await self.driver.return_tips96(ReturnTips96Request()))


class CannotStepAsideError(RuntimeError):
    """An arm cannot reach into this handler's deck, because the handler will
    not move off it.

    Raised rather than passed over: the gantry stays over the site the arm is
    about to enter, so proceeding is a collision. Either the handler stayed busy
    past all patience, holding tips on a head or a plate in its jaws, or its
    driver cannot park at all. The driver's own refusal says which, and it is
    appended to the message."""


class DeckLabwareIdentityError(RuntimeError):
    """The deck already holds a driver resource under this instance's name,
    but it was materialized for a DIFFERENT instance id.

    The driver world keys deck labware by instance name, so this can only
    mean a minted-name collision or a desynced
    driver world; operating through it would touch the wrong plate."""


@runtime_checkable
class _ParksItsOwnMovingParts(Protocol):
    """A handler that can move clear of its own deck when asked."""

    async def park_gantry(self, request: ParkGantryRequest) -> None: ...


@runtime_checkable
class _AdvertisesItsCapabilities(Protocol):
    """A driver that reports what the device behind it can actually do.

    A remote driver carries the whole wire surface whatever it is bound to, so
    having the method proves nothing; the advertised set is what does.
    """

    @property
    def declared_interfaces(self) -> frozenset[str] | None: ...


def _advertises_parking(driver: _ParksItsOwnMovingParts) -> bool:
    """Does the device behind this driver actually park? Unknown counts as yes."""
    if not isinstance(driver, _AdvertisesItsCapabilities):
        return True
    declared = driver.declared_interfaces
    return declared is None or "IGantryParking" in declared


class LiquidHandler(_LiquidHandlerBase):
    """PLR liquid handler with an orca-modeled deck and internal gripper.

    Deck sites are flat routing nodes derived from the deck layout. There is
    no DESIGNATED handoff: any deck site the external arm teaches (a
    site-qualified teachpoint like "flex/D3-slot") is a transit point, and
    the internal gripper relays between deck sites for everything else -
    handoff-ness is topology, not declaration. A handler whose deck orca
    does not model (protocol/remote/gateway handlers) uses
    ``LiquidHandlerProtocol``.

    ``deck_layout`` names the layout in ``deck_layout_store`` to apply; required
    for DEVICE_SIM / LIVE (validated at submit), optional under PURE_SIM.
    """

    _DECK_MODELING: ClassVar[bool] = True

    def __init__(
        self,
        name: str,
        *,
        deck_layout_store: IDeckLayoutStore | None = None,
        deck_layout: str | None = None,
        sim: bool = False,
        sim_override: WorkflowRunMode | None = None,
        trust_driver_state: bool = True,
        park_gripper_at: GantryParkPosition | None = None,
    ) -> None:
        super().__init__(
            name, sim=sim, sim_override=sim_override,
            trust_driver_state=trust_driver_state,
        )
        self._park_gripper_at = park_gripper_at
        self._warned_it_cannot_step_aside = False
        self._deck_layout_store: IDeckLayoutStore = deck_layout_store or NullDeckLayoutStore()
        self._deck_layout = deck_layout
        self._gripper: TransporterBase | None = None
        # Per deck world: instance name -> instance id. The driver world keys
        # deck labware by instance name, so an id mismatch under one name is a desync.
        self._materialized_instances: dict[WorkflowRunMode, dict[str, str]] = {}
        # Keyed by resolved mode: sim driver, wire sim backend, and wire real
        # backend are three separate worlds; configuring one leaves the others bare.
        self._configured_worlds: dict[WorkflowRunMode, DeckLayoutConfig] = {}
        self._deck_configure_lock = TrackedLock(f"{name} deck-layout lock")

    @property
    def deck_layout_store(self) -> IDeckLayoutStore:
        return self._deck_layout_store

    @property
    def deck_layout(self) -> str | None:
        """Name of the configured deck layout in the store, or None (PURE_SIM only)."""
        return self._deck_layout

    async def invalidate_deck_world(self) -> None:
        """Forget that any deck world was configured.

        Called when the driver's session may have been rebuilt (initialize,
        connect, an operator taking the robot back): a rebuild silently empties
        the driver's deck, so the next ``deck_world_layout`` must
        re-dispatch the layout and the next reconcile must re-project occupancy.
        """
        async with self._deck_configure_lock.held_for("invalidate_deck_world"):
            self._configured_worlds.clear()

    async def deck_world_layout(self) -> tuple[DeckLayoutConfig, bool] | None:
        """The layout this driver world is laid out with, laying it out the
        first time from the store. ``(layout, just_configured)``, or None when
        this device declares no layout.

        Once a world is laid out, that layout is what it holds, whatever the
        store says now. A deck-layout edit is rebuild-required, so re-reading
        the store mid-run would answer a deck operation with a deck the driver
        does not have -- and every operation resolving separately is how two of
        them came to hold different snapshots of the same deck, with occupancy
        computed from one landing on a skeleton built from the other.

        Lock-guarded: the lazy-init walk and a facade-triggered reconcile can
        both reach here concurrently, and the configure await yields between
        the world check and the dispatch.
        """
        async with self._deck_configure_lock.held_for("configure_deck"):
            laid_out = self._configured_worlds.get(self.effective_mode)
            if laid_out is not None:
                return laid_out, False
            declared = await self.resolve_deck_config_async()
            if declared is None:
                return None
            await self.driver.configure_deck(declared)
            self._configured_worlds[self.effective_mode] = declared
            return declared, True

    async def resolve_deck_config_async(self) -> DeckLayoutConfig | None:
        """Resolve the configured layout from the store (DB-authoritative), or None."""
        if self._deck_layout is None:
            return None
        return await self._deck_layout_store.get(self._deck_layout)

    async def reset_labware_state(self) -> None:
        """Wipe deck occupancy (carriers preserved) so the deck projection holds
        no labware the ledger no longer knows about.

        Scoped to the ambient world: the reset dispatches through `driver`,
        so only that world's deck and instance map are cleared -- another
        configured world keeps its identity guard armed. Clear-all wants every
        world and asks for it by name, through
        ``reset_labware_state_everywhere``.
        """
        await self.driver.reset_deck_labware(ResetDeckLabwareRequest())
        self._world_instances().clear()

    async def reset_labware_state_everywhere(self) -> None:
        """The same wipe against every world this device has laid out.

        Scoping the panic button to the caller's world left the rest holding
        what they had. An operator's clear-all resolves to LIVE, and the plates
        a sim run put down are in the sim world; a resident loaded through the
        vendor's own app is in whichever world that session belongs to and in no
        orca record at all, which is the case only a deck-wide reset can reach.

        A deck that will not answer is logged and skipped, keeping its instance
        map so a later reset tries again. One unreachable deck must not stop the
        panic button clearing the decks that do answer.
        """
        worlds = self._laid_out_worlds()
        if self.effective_mode not in worlds:
            # A world with no record can still be holding labware.
            worlds.append(self.effective_mode)
        reached: list[ILiquidHandlerWithProtocolDriver] = []
        for driver in self._distinct_drivers(worlds):
            try:
                await driver.reset_deck_labware(ResetDeckLabwareRequest())
            except Exception as exc:
                orca_logger.warning(
                    "%s: a deck world would not reset (%s); it may still hold "
                    "labware.", self.name, exc,
                )
                continue
            reached.append(driver)
        for mode in worlds:
            if any(self.driver_under(mode) is d for d in reached):
                self._instances_in(mode).clear()

    def _world_instances(self) -> dict[str, str]:
        return self._instances_in(self.effective_mode)

    def _instances_in(self, mode: WorkflowRunMode) -> dict[str, str]:
        return self._materialized_instances.setdefault(mode, {})

    async def project_deck_labware(
        self, labware: LabwareInstance, *, at: str, catalog_ref: str,
        well_state: LabwareWellState | None,
    ) -> None:
        """Put exactly this labware on the driver deck at ``at``, replacing
        whatever the driver holds under its name.

        The narrow alternative to a deck-wide reconcile: one arrival, one move,
        or one state correction re-materializes that labware and leaves every
        neighbour on the deck untouched.
        """
        async with self.lock.held_for("add_deck_labware"):
            self._refuse_foreign_instance(labware)
            await self._unmaterialize(labware)
            await self.driver.add_deck_labware(AddDeckLabwareRequest(
                name=labware.name, catalog_ref=catalog_ref, at=at,
                well_state=well_state,
            ))
            self._world_instances()[labware.name] = labware.id

    async def retract_deck_labware(self, labware: LabwareInstance) -> None:
        """Take exactly this labware off the driver deck; leave the rest alone."""
        async with self.lock.held_for("retract_deck_labware"):
            await self._unmaterialize(labware)

    async def retract_deck_labware_from_every_world(
        self, labware: LabwareInstance,
    ) -> None:
        """Take this labware off every deck world that was laid out, not only
        the one the caller is dispatching in.

        A discharge says the labware is off the deck, and that is true in every
        world. Scoped to the caller's world it was not: an operator write
        resolves to LIVE, so a plate a PURE_SIM run put down survived its own
        discharge and then refused the next run's placement at that slot.

        A world nobody laid out is skipped, so this never opens a deck on a
        driver that was never told about the labware in the first place.
        """
        async with self.lock.held_for("retract_deck_labware"):
            await self._unmaterialize_across(labware, self._laid_out_worlds())

    async def retract_deck_labware_from_other_worlds(
        self, labware: LabwareInstance,
    ) -> None:
        """Take this labware off every laid-out world EXCEPT the caller's.

        For a move, where the caller's own world is being told where the labware
        went and the others only need to stop holding it where it was. Telling
        them where it went instead would put an operator's plate onto a
        simulator's deck, or a simulated one onto the instrument.
        """
        ambient = self.effective_mode
        async with self.lock.held_for("retract_deck_labware"):
            await self._unmaterialize_across(
                labware, [m for m in self._laid_out_worlds() if m is not ambient])

    def _laid_out_worlds(self) -> list[WorkflowRunMode]:
        """Every mode whose driver deck this device has actually built."""
        worlds = list(self._configured_worlds)
        worlds += [m for m in self._materialized_instances if m not in worlds]
        return worlds

    def _distinct_drivers(
        self, modes: Sequence[WorkflowRunMode],
    ) -> list[ILiquidHandlerWithProtocolDriver]:
        """One entry per driver these worlds dispatch to.

        DEVICE_SIM and LIVE both land on the live driver, so they are one deck
        wearing two names. Asking it twice is a second wire round trip that can
        only repeat the first answer.
        """
        drivers: list[ILiquidHandlerWithProtocolDriver] = []
        for mode in modes:
            driver = self.driver_under(mode)
            if not any(seen is driver for seen in drivers):
                drivers.append(driver)
        return drivers

    async def _unmaterialize(self, labware: LabwareInstance) -> None:
        """Drop the driver's deck object for this labware in the world the
        caller is dispatching in, if it holds one. Caller holds the device lock.

        Raises on a driver that cannot answer. A placement or a pick is acting
        on what this call was supposed to have done, so a failure here has to
        stop them rather than let them proceed on a deck that still holds it.
        """
        await self._drop_deck_object(self.driver, labware)
        self._world_instances().pop(labware.name, None)

    async def _unmaterialize_across(
        self, labware: LabwareInstance, modes: Sequence[WorkflowRunMode],
    ) -> None:
        """``_unmaterialize`` against named worlds. Caller holds the lock.

        A world that cannot answer is logged and skipped, and keeps its record
        of holding the labware so a later sweep tries again. Sweeping every
        world exists so one plate is gone from all of them; letting the first
        unreachable deck raise would leave it standing in a reachable one.
        """
        reached: list[ILiquidHandlerWithProtocolDriver] = []
        for driver in self._distinct_drivers(modes):
            try:
                await self._drop_deck_object(driver, labware)
            except Exception as exc:
                orca_logger.warning(
                    "%s: could not take %s off one of its deck worlds (%s); "
                    "that deck may still hold it.", self.name, labware.name, exc,
                )
                continue
            reached.append(driver)
        for mode in modes:
            if any(self.driver_under(mode) is d for d in reached):
                self._instances_in(mode).pop(labware.name, None)

    async def _drop_deck_object(
        self, driver: ILiquidHandlerWithProtocolDriver, labware: LabwareInstance,
    ) -> None:
        if await self._deck_holds_on(driver, labware.name):
            await driver.remove_deck_labware(
                RemoveDeckLabwareRequest(name=labware.name)
            )

    def _refuse_foreign_instance(self, labware: LabwareInstance) -> None:
        """A deck name orca materialized for a different instance is a desync,
        not something to overwrite."""
        prior = self._world_instances().get(labware.name)
        if prior is not None and prior != labware.id:
            raise DeckLabwareIdentityError(
                f"{self.name}: deck resource {labware.name!r} was materialized "
                f"for instance {prior}; cannot also project instance {labware.id}."
            )

    @property
    def gripper(self) -> TransporterBase | None:
        """This deck's own gripper, or None when the deck has too few sites
        to mesh one (``add_gripper_edges`` returns early).

        The whole mover, not just the identity a placement hook needs: the deck
        projection has to ask it what it is holding and where that came from.
        """
        return self._gripper

    def set_gripper(self, gripper: TransporterBase) -> None:
        self._gripper = gripper

    def _is_internal_hop(self, mover: IPlateMover) -> bool:
        """The mover IS this deck's own gripper, so the plate never leaves the
        device: no door to open, and move_plate relocates it in the driver
        world itself. Derived, not tracked -- the gripper is built one per
        device with edges only between that device's sites, so mover identity
        answers this exactly."""
        return self._gripper is not None and mover is self._gripper

    async def _do_notify_placed(self, labware: LabwareInstance, mover: IPlateMover, target: str | None = None) -> None:
        if target is None:
            await self.driver.close()
            return
        # Idempotent materialization: the gripper's move_plate has
        # already relocated a held plate; only a fresh arrival materializes.
        if self._is_internal_hop(mover):
            await self._step_aside()
            return
        if await self._deck_holds(labware.name):
            prior = self._world_instances().get(labware.name)
            if prior is not None and prior != labware.id:
                raise DeckLabwareIdentityError(
                    f"{self.name}: deck resource {labware.name!r} was materialized "
                    f"for instance {prior}; cannot also place instance {labware.id} "
                    f"at {target}."
                )
            # A same-named resource orca did not materialize (reconcile-placed
            # resident): adopt it instead of silently returning unmapped.
            self._world_instances()[labware.name] = labware.id
            return
        # catalog_ref = template labware_type; the instance PLR model can be a deprecated alias.
        catalog_ref = getattr(labware.template, "labware_type", None)
        if not isinstance(catalog_ref, str):
            raise ValueError(
                f"{self.name}: cannot materialize {labware} on the deck; "
                f"template {labware.template_name!r} has no catalog labware_type."
            )
        await self.driver.add_deck_labware(AddDeckLabwareRequest(
            name=labware.name,
            catalog_ref=catalog_ref,
            at=target,
            well_state=await labware.driver_well_state(),
        ))
        self._world_instances()[labware.name] = labware.id

    async def _do_notify_picked(self, labware: LabwareInstance, mover: IPlateMover, target: str | None = None) -> None:
        # Un-materialize only on a true departure (external arm pick); a
        # gripper hop keeps the plate in the driver world for its move_plate.
        if self._is_internal_hop(mover):
            return
        await self._unmaterialize(labware)
        await self.driver.close()

    async def _deck_holds(self, name: str) -> bool:
        return await self._deck_holds_on(self.driver, name)

    async def _deck_holds_on(
        self, driver: ILiquidHandlerWithProtocolDriver, name: str,
    ) -> bool:
        state = await driver.get_deck_state(GetDeckStateRequest())
        return any(resource.name == name for resource in state.labware)

    async def _do_prepare_for_pick(self, labware: LabwareInstance, mover: IPlateMover, target: str | None = None) -> None:
        if target is None or self._is_internal_hop(mover):
            return
        await self.driver.open()

    async def _do_prepare_for_place(self, labware: LabwareInstance, mover: IPlateMover, target: str | None = None) -> None:
        if self._is_internal_hop(mover):
            return
        await self.driver.open()

    async def step_aside_for(self, mover: IPlateMover) -> None:
        """Get the gantry off this deck before ``mover`` reaches into it, waiting
        if the handler is busy.

        This is the gate. ``_step_aside`` parks early after the handler's own
        handoff so the arm usually finds it already done, but early parking is an
        optimisation and only this runs before every arm.

        What it guarantees is bounded by what the handler can do: a handler that
        parks is parked before the arm goes in, and one that cannot park is
        warned about and passed. Nothing here detects a gantry that is in the
        way; it only asks handlers that can move to move.

        Asking on the way IN is what makes the waiting possible. The device lock
        is taken per driver call, not per action, and a device reservation
        sanctions labware arriving and leaving mid-action, so a handler can be
        part-way through a method when an arm arrives. It refuses while it is
        holding something -- tips on a head, or a plate in its own gripper -- and
        refusing is not failing: the work in progress wins and this asks again
        until it is done.

        Bounded because the refusal can outlive the work. Work that ends without
        putting down what it picked up leaves the handler permanently unwilling,
        and an arm silently waiting forever is worse than an error an operator
        can act on.
        """
        if self._is_internal_hop(mover):
            return
        driver = self._parking_driver()
        if driver is None:
            return
        request = ParkGantryRequest(at=self._park_gripper_at)
        give_up_after = time.monotonic() + STEP_ASIDE_PATIENCE_SECONDS
        announced = False
        while True:
            try:
                async with self.lock.held_for("park_gantry"):
                    await driver.park_gantry(request)
                return
            except Exception as exc:
                if not is_busy_refusal(exc):
                    raise
                if time.monotonic() >= give_up_after:
                    raise CannotStepAsideError(
                        f"{self.name}: still mid-transfer after "
                        f"{STEP_ASIDE_PATIENCE_SECONDS:.0f}s, so {mover.name} cannot "
                        f"reach into its deck. The handler is holding something it "
                        f"never put down: tips on a head, or a plate in its jaws. "
                        f"Its own refusal below says which. Clear that, then retry "
                        f"this move. ({exc})"
                    ) from exc
                if not announced:
                    announced = True
                    orca_logger.info(
                        "%s is mid-transfer, so %s is waiting to reach into its "
                        "deck. (%s)", self.name, mover.name, exc,
                    )
                await asyncio.sleep(_STEP_ASIDE_RETRY_SECONDS)

    async def _step_aside(self) -> None:
        """Park the gantry early, right after handing a labware to one of our sites.

        A gantry stays wherever its last operation left it, gripper hanging off
        it, and nothing else moves it. An arm coming in to that site meets it: on
        the bench a PF400 placing a tip rack into a Flex staging slot clipped the
        Flex gripper, and a taller slot would have been a real collision.

        Doing it here overlaps the park with the arm's travel, so the handoff is
        not paid for twice. It is not the safety gate; ``step_aside_for`` is,
        because only the way-in path can wait for a busy handler.

        So a refusal here is not an error. The handler is still holding
        something, the work that owns it wins, and the arm's own gate will ask
        again when it arrives. What is an error is a handler that cannot park at all: a probe
        that quietly does nothing reads as wired when it is not.
        """
        driver = self._parking_driver()
        if driver is None:
            return
        try:
            await driver.park_gantry(ParkGantryRequest(at=self._park_gripper_at))
        except Exception as exc:
            if not is_busy_refusal(exc):
                raise
            orca_logger.debug(
                "%s: busy, so it stayed put after its own handoff; the next arm "
                "in will wait for it. (%s)", self.name, exc,
            )

    def _parking_driver(self) -> _ParksItsOwnMovingParts | None:
        """The bound driver if it can park, else None having warned once.

        Declaring ``park_gripper_at`` for a driver that cannot park is a topology
        error rather than a missing capability, so that one raises: the author
        asked for something specific and needs to hear it did not happen.
        """
        driver = self.driver
        if isinstance(driver, _ParksItsOwnMovingParts) and _advertises_parking(driver):
            return driver
        if self._park_gripper_at is not None:
            raise CannotStepAsideError(
                f"{self.name}: park_gripper_at is declared but its driver "
                f"({type(driver).__name__}) cannot park."
            )
        self._warn_it_cannot_step_aside(driver)
        return None

    def _warn_it_cannot_step_aside(self, driver: object) -> None:
        """Once per device: a run makes this hop repeatedly."""
        if self._warned_it_cannot_step_aside:
            return
        self._warned_it_cannot_step_aside = True
        orca_logger.warning(
            "%s: cannot be moved off its own deck. Its driver (%s) does not "
            "declare IGantryParking, so an arm reaching into this deck meets "
            "whatever its last operation left there. Bind a driver that can "
            "park if another mover reaches this deck.",
            self.name,
            type(driver).__name__,
        )


class LiquidHandlerProtocol(_LiquidHandlerBase):
    """Liquid handler with no orca-modeled deck: the deliberate opt-out.

    Use for handlers whose deck is managed outside orca: protocol-driven
    handlers (e.g. a Bravo running VWorks, driven via ``run_protocol``) and
    remote/gateway handlers where the on-prem driver owns the deck. orca models
    no addressable deck sites and no internal-gripper handoff; the bound driver
    still decides which verbs (well-level vs ``run_protocol``) actually work.

    Unmodeled deck is not the same as no capacity. A protocol that consumes
    several labware at once (sample plate + tip box + assay plate) needs a
    position for each, because a position holds one labware. Declare them with
    ``site_names``; orca routes labware onto them without knowing where the
    vendor deck actually puts things. Default is a single position, which an
    action taking more than one input will reject.
    """


class Delidder(Device[IDelidderDriver], IDelidder):
    KIND: ClassVar[str] = "delidder"

    def __init__(
        self,
        name: str,
        sim_override: WorkflowRunMode | None = None,
    ) -> None:
        live, sim_drv = resolve_drivers(
            self.KIND, name, SimDelidderDriver,
        )
        super().__init__(name, live, sim_drv, sim_override=sim_override)

    async def delid(self):
        await self.driver.delid(DelidRequest())

class Waste(Device[IWasteDriver], IWaste):
    KIND: ClassVar[str] = "waste"

    def __init__(
        self,
        name: str,
        sim_override: WorkflowRunMode | None = None,
    ) -> None:
        live, sim_drv = resolve_drivers(
            self.KIND, name, SimWasteDriver,
        )
        super().__init__(name, live, sim_drv, sim_override=sim_override)


class Storage(Device[IStorageDriver], IStorage, IPlateSource):
    KIND: ClassVar[str] = "storage"

    def __init__(
        self,
        name: str,
        sim_override: WorkflowRunMode | None = None,
    ) -> None:
        live, sim_drv = resolve_drivers(
            self.KIND, name, SimStorageDriver,
        )
        super().__init__(name, live, sim_drv, sim_override=sim_override)

    async def dispense(self) -> None:
        await self.driver.dispense()

class Reader(Device[IReaderDriver], IReader):
    KIND: ClassVar[str] = "reader"

    def __init__(
        self,
        name: str,
        sim_override: WorkflowRunMode | None = None,
    ) -> None:
        live, sim_drv = resolve_drivers(
            self.KIND, name, SimReaderDriver,
        )
        super().__init__(name, live, sim_drv, sim_override=sim_override)

    async def read(self, protocol_filepath: str, output_filepath: str) -> None:
        await self.driver.read(
            ReadRequest(protocol_filepath=protocol_filepath, output_filepath=output_filepath)
        )
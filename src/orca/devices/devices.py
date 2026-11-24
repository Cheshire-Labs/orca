import asyncio
import logging
import time
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import (
    AsyncIterator,
    ClassVar,
    Protocol,
    Sequence,
    runtime_checkable,
)

from pydantic import JsonValue

from cheshire_drivers.delidder_models import DelidRequest
from cheshire_drivers.gantry_models import GantryParkPosition, ParkGantryRequest
from cheshire_drivers.interfaces import (
    IDelidderDriver,
    ILiquidHandlerWithProtocolDriver,
    IPlateWasherDriver,
    IReaderDriver,
    IStorageDriver,
    IWasteDriver,
)
from cheshire_drivers.labware_interfaces import (
    IContainer,
    IPlate,
    ITipRack,
    ITipSpot,
    ITrough,
)
from cheshire_drivers.liquid_handler_models import (
    AddDeckLabwareRequest,
    DeckLayoutConfig,
    DiscardStrandedTipsRequest,
    GetDeckStateRequest,
    LabwareStateResponse,
    LabwareWellState,
    ReconcileHardwareStateRequest,
    ReconcileHardwareStateResponse,
    RemoveDeckLabwareRequest,
    ResetDeckLabwareRequest,
)
from cheshire_drivers.pipetting import MixParams, PipettingProfile
from cheshire_drivers.protocol_runner_models import RunProtocolRequest
from cheshire_drivers.reader_models import ReadRequest

from orca.devices import lh_requests
from orca.devices.device_interfaces import (
    IDelidder,
    ILiquidHandler,
    IPlateSource,
    IPlateWasher,
    IReader,
    IStorage,
    IWaste,
)
from orca.devices.driver_refusal import is_busy_refusal
from orca.gateway.controller.exceptions import CommandNotSavedError
from orca.resource_models.devices import Device
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_placeable_interface import IPlateMover
from orca.resource_models.tracked_lock import TrackedLock
from orca.resource_models.transporter_base import TransporterBase
from orca.driver_management.device_factory_context import resolve_driver
from orca.resource_models.store_interfaces import IDeckLayoutStore
from orca.modes.run_modes import WorkflowRunMode, mode_scope

orca_logger = logging.getLogger("orca")

# How long an arm waits for a handler to finish what it is holding before giving
# up and telling an operator. Long enough for a pipetting sequence to run its
# course (tips go on and come off within one method, not across an incubation),
# short enough that tips left on by a method that ended are not an endless wait.
REACH_IN_PATIENCE_SECONDS: float = 900.0
_REACH_IN_RETRY_SECONDS: float = 2.0



class PlateWasher(Device[IPlateWasherDriver], IPlateWasher):
    KIND: ClassVar[str] = "plate_washer"

    def __init__(
        self,
        name: str,
    ) -> None:
        driver = resolve_driver(
            self.KIND, name, IPlateWasherDriver,
        )
        super().__init__(name, driver)
        
    async def run_protocol(self, protocol_filepath: str, params: dict[str, JsonValue]) -> None:
        await self.driver.run_protocol(
            RunProtocolRequest(protocol_filepath=protocol_filepath, params=params)
        )

    

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

    _DECK_MODELING: ClassVar[bool] = False

    @property
    def deck_modeling(self) -> bool:
        return self._DECK_MODELING

    def __init__(
        self,
        name: str,
        sim: bool = False,
        trust_driver_state: bool = True,
        site_names: list[str] | None = None,
    ) -> None:
        driver = resolve_driver(
            self.KIND, name, ILiquidHandlerWithProtocolDriver, deck_modeling=self._DECK_MODELING,
        )
        super().__init__(name, driver, site_names=site_names)
        # Effective trust = user opt-in AND the live driver advertising
        # `provides_state`; the factory supplies that driver, so we read it here.
        self._trust_driver_state = trust_driver_state and getattr(driver, "provides_state", False)

    @property
    def trust_driver_state(self) -> bool:
        return self._trust_driver_state

    def filter_driver_state(self, response: LabwareStateResponse) -> LabwareStateResponse:
        """Strip labware_state when the user has not opted into driver state.

        Returns the response unchanged when trust_driver_state is True; otherwise
        returns a response with empty labware_state so the interpreter does not
        emit DRIVER_OBSERVED records. ``per_channel_errors`` is always preserved:
        per-channel partial-failure attribution is outcome data, not driver-state
        observation, so it must reach the interpreter regardless of trust setting.

        Public because an operator's own command answers to the same setting,
        and it reaches the response from outside the bridge.
        """
        if self._trust_driver_state:
            return response
        return LabwareStateResponse(
            success=response.success,
            per_channel_errors=response.per_channel_errors,
        )

    async def run_protocol(self, protocol_filepath: str, params: dict[str, JsonValue]) -> None:
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
        request = lh_requests.aspirate_request(
            containers, volumes, flow_rates, offsets_z, use_channels,
            liquid_class, technique,
        )
        return self.filter_driver_state(await self.driver.aspirate(request))

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
        request = lh_requests.dispense_request(
            containers, volumes, flow_rates, offsets_z, use_channels,
            liquid_class, technique,
        )
        return self.filter_driver_state(await self.driver.dispense(request))

    async def pick_up_tips(self, tip_spots: list[ITipSpot]) -> LabwareStateResponse:
        request = lh_requests.pick_up_tips_request(tip_spots)
        return self.filter_driver_state(await self.driver.pick_up_tips(request))

    async def drop_tips(self, tip_spots: list[ITipSpot]) -> LabwareStateResponse:
        request = lh_requests.drop_tips_request(tip_spots)
        return self.filter_driver_state(await self.driver.drop_tips(request))

    async def discard_tips(self, use_channels: list[int] | None = None) -> LabwareStateResponse:
        request = lh_requests.discard_tips_request(use_channels)
        return self.filter_driver_state(await self.driver.discard_tips(request))

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
        request = lh_requests.mix_request(
            containers, params, use_channels, liquid_class, technique,
        )
        return self.filter_driver_state(await self.driver.mix(request))

    # --- 96-head bridge methods ---

    async def aspirate96(
        self,
        labware: IPlate | ITrough,
        volume: float,
        flow_rate: float | None = None,
        liquid_height: float | None = None,
    ) -> LabwareStateResponse:
        response = await self.driver.aspirate96(lh_requests.aspirate96_request(
            labware, volume, flow_rate, liquid_height,
        ))
        return self.filter_driver_state(response)

    async def dispense96(
        self,
        labware: IPlate | ITrough,
        volume: float,
        flow_rate: float | None = None,
        liquid_height: float | None = None,
    ) -> LabwareStateResponse:
        response = await self.driver.dispense96(lh_requests.dispense96_request(
            labware, volume, flow_rate, liquid_height,
        ))
        return self.filter_driver_state(response)

    async def pick_up_tips96(self, tip_rack: ITipRack) -> LabwareStateResponse:
        response = await self.driver.pick_up_tips96(
            lh_requests.pick_up_tips96_request(tip_rack),
        )
        return self.filter_driver_state(response)

    async def drop_tips96(
        self,
        tip_rack: ITipRack | None = None,
    ) -> LabwareStateResponse:
        response = await self.driver.drop_tips96(
            lh_requests.drop_tips96_request(tip_rack),
        )
        return self.filter_driver_state(response)

    async def return_tips96(self) -> LabwareStateResponse:
        return self.filter_driver_state(
            await self.driver.return_tips96(lh_requests.return_tips96_request()),
        )


class GantryWillNotParkError(RuntimeError):
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
    site-qualified teachpoint like "lh1/site_3") is a transit point, and
    the internal gripper relays between deck sites for everything else -
    handoff-ness is topology, not declaration. A handler whose deck orca
    does not model (protocol/remote/gateway handlers) uses
    ``LiquidHandlerProtocol``.

    ``deck_layout`` names the layout in ``deck_layouts/<name>.json`` to apply; every run
    mode requires it (validated at submit).
    """

    _DECK_MODELING: ClassVar[bool] = True

    def __init__(
        self,
        name: str,
        *,
        deck_layout: str | None = None,
        sim: bool = False,
        trust_driver_state: bool = True,
        park_gripper_at: GantryParkPosition | None = None,
    ) -> None:
        super().__init__(
            name, sim=sim,
            trust_driver_state=trust_driver_state,
        )
        self._park_gripper_at = park_gripper_at
        # Keyed by resolved mode: which driver cannot park, and whether that is
        # worth a warning, both change with the mode.
        self._told_it_cannot_park: set[WorkflowRunMode] = set()
        self._deck_layout_store: IDeckLayoutStore | None = None
        self._deck_layout = deck_layout
        self._gripper: TransporterBase | None = None
        # Per deck world: instance name -> instance id. The driver world keys
        # deck labware by instance name, so an id mismatch under one name is a desync.
        self._materialized_instances: dict[WorkflowRunMode, dict[str, str]] = {}
        # Keyed by resolved mode: orca-client keeps one deck per mode, so configuring one leaves the others bare.
        self._configured_worlds: dict[WorkflowRunMode, DeckLayoutConfig] = {}
        self._deck_configure_lock = TrackedLock(f"{name} deck-layout lock")

    def bind_deck_layouts(self, store: IDeckLayoutStore) -> None:
        """The build attaches `deck_layouts/<name>.json`; a topology never passes one."""
        self._deck_layout_store = store

    @property
    def deck_layout_store(self) -> IDeckLayoutStore:
        if self._deck_layout_store is None:
            raise RuntimeError(
                f"liquid handler {self.name!r} has no deck layouts attached; "
                "build_system attaches deck_layouts/<name>.json",
            )
        return self._deck_layout_store

    @property
    def deck_layout(self) -> str | None:
        """Name of the configured deck layout in the store, or None when none is named."""
        return self._deck_layout

    async def invalidate_deck_world(self) -> None:
        """Forget that any deck world was configured: the client may have rebuilt every driver it holds.

        A rebuild silently empties the driver's deck, so the next ``deck_world_layout`` must re-dispatch the layout
        and the next reconcile must re-project occupancy.
        """
        async with self._deck_configure_lock.held_for("invalidate_deck_world"):
            self._configured_worlds.clear()

    async def invalidate_deck_world_in_force(self) -> None:
        """Forget that the deck world in force was configured: a command to this mode's driver may have rebuilt it.

        Each run mode has its own driver, so a lifecycle command or a reconcile leaves the other worlds as they were.
        """
        async with self._deck_configure_lock.held_for("invalidate_deck_world"):
            self._configured_worlds.pop(await self.effective_mode(), None)

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
            world = await self.effective_mode()
            laid_out = self._configured_worlds.get(world)
            if laid_out is not None:
                return laid_out, False
            declared = await self.resolve_deck_config_async()
            if declared is None:
                return None
            try:
                await self.engine_commands.keep_world("configure_deck", lambda: self.driver.configure_deck(declared))
            except CommandNotSavedError:
                # The deck is laid out; only the record of it is missing, so it is not laid out again.
                self._configured_worlds[world] = declared
                raise
            self._configured_worlds[world] = declared
            return declared, True

    async def resolve_deck_config_async(self) -> DeckLayoutConfig | None:
        """Resolve the configured layout from the store (DB-authoritative), or None."""
        if self._deck_layout is None:
            return None
        return await self.deck_layout_store.get(self._deck_layout)

    async def reset_labware_state(self) -> None:
        """Wipe deck occupancy (carriers preserved) so the deck projection holds
        no labware the ledger no longer knows about.

        Scoped to the ambient world: the reset dispatches through `driver`,
        so only that world's deck and instance map are cleared -- another
        configured world keeps its identity guard armed. Clear-all wants every
        world and asks for it by name, through
        ``reset_labware_state_everywhere``.
        """
        await self.engine_commands.keep_world(
            "reset_deck_labware", lambda: self.driver.reset_deck_labware(ResetDeckLabwareRequest()),
        )
        (await self._world_instances()).clear()

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
        ambient = await self.effective_mode()
        if ambient not in worlds:
            # A world with no record can still be holding labware.
            worlds.append(ambient)
        for mode in worlds:
            with mode_scope(mode):
                if await self.effective_mode() is not mode:
                    continue
                try:
                    await self.engine_commands.keep_world(
                        "reset_deck_labware", lambda: self.driver.reset_deck_labware(ResetDeckLabwareRequest()),
                    )
                except CommandNotSavedError:
                    raise
                except Exception as exc:
                    orca_logger.warning(
                        "%s: its %s deck would not reset (%s); it may still hold "
                        "labware.", self.name, mode.value, exc,
                    )
                    continue
            self._instances_in(mode).clear()

    async def _world_instances(self) -> dict[str, str]:
        return self._instances_in(await self.effective_mode())

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
            await self._refuse_foreign_instance(labware)
            await self._unmaterialize(labware)
            await self.engine_commands.keep_world("add_deck_labware", lambda: self.driver.add_deck_labware(
                AddDeckLabwareRequest(name=labware.name, catalog_ref=catalog_ref, at=at, well_state=well_state),
            ))
            (await self._world_instances())[labware.name] = labware.id

    async def retract_deck_labware(self, labware: LabwareInstance) -> None:
        """Take exactly this labware off the driver deck; leave the rest alone."""
        async with self.lock.held_for("retract_deck_labware"):
            await self._unmaterialize(labware)

    def _laid_out_worlds(self) -> list[WorkflowRunMode]:
        """Every mode whose driver deck this device has actually built."""
        worlds = list(self._configured_worlds)
        worlds += [m for m in self._materialized_instances if m not in worlds]
        return worlds

    async def _unmaterialize(self, labware: LabwareInstance) -> None:
        """Drop the driver's deck object for this labware in the world the
        caller is dispatching in, if it holds one. Caller holds the device lock.

        Raises on a driver that cannot answer. A placement or a pick is acting
        on what this call was supposed to have done, so a failure here has to
        stop them rather than let them proceed on a deck that still holds it.
        """
        await self._drop_deck_object(self.driver, labware)
        (await self._world_instances()).pop(labware.name, None)

    async def _drop_deck_object(
        self, driver: ILiquidHandlerWithProtocolDriver, labware: LabwareInstance,
    ) -> None:
        if await self._deck_holds_on(driver, labware.name):
            await self.engine_commands.keep_world(
                "remove_deck_labware", lambda: driver.remove_deck_labware(RemoveDeckLabwareRequest(name=labware.name)),
            )

    async def _refuse_foreign_instance(self, labware: LabwareInstance) -> None:
        """A deck name orca materialized for a different instance is a desync,
        not something to overwrite."""
        prior = (await self._world_instances()).get(labware.name)
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

    def held_labware_id(self) -> str | None:
        held = self._gripper.labware if self._gripper is not None else None
        return held.id if held is not None else None

    def _is_internal_hop(self, mover: IPlateMover) -> bool:
        """The mover IS this deck's own gripper, so the plate never leaves the
        device: no door to open, and move_plate relocates it in the driver
        world itself. Derived, not tracked -- the gripper is built one per
        device with edges only between that device's sites, so mover identity
        answers this exactly."""
        return self._gripper is not None and mover is self._gripper

    async def _do_notify_placed(self, labware: LabwareInstance | None, mover: IPlateMover, target: str | None = None, site: str | None = None) -> None:
        if target is None:
            await self.engine_commands.run("close", self.driver.close)
            return
        # Idempotent materialization: the gripper's move_plate has
        # already relocated a held plate; only a fresh arrival materializes.
        if labware is None or self._is_internal_hop(mover):
            return
        if await self._deck_holds(labware.name):
            prior = (await self._world_instances()).get(labware.name)
            if prior is not None and prior != labware.id:
                raise DeckLabwareIdentityError(
                    f"{self.name}: deck resource {labware.name!r} was materialized "
                    f"for instance {prior}; cannot also place instance {labware.id} "
                    f"at {target}."
                )
            # A same-named resource orca did not materialize (reconcile-placed
            # resident): adopt it instead of silently returning unmapped.
            (await self._world_instances())[labware.name] = labware.id
            return
        # catalog_ref = template labware_type; the instance PLR model can be a deprecated alias.
        catalog_ref = getattr(labware.template, "labware_type", None)
        if not isinstance(catalog_ref, str):
            raise ValueError(
                f"{self.name}: cannot materialize {labware} on the deck; "
                f"template {labware.template_name!r} has no catalog labware_type."
            )
        request = AddDeckLabwareRequest(
            name=labware.name,
            catalog_ref=catalog_ref,
            at=target,
            well_state=await labware.driver_well_state(),
        )
        await self.engine_commands.keep_world("add_deck_labware", lambda: self.driver.add_deck_labware(request))
        (await self._world_instances())[labware.name] = labware.id

    async def _do_notify_picked(self, labware: LabwareInstance | None, mover: IPlateMover, target: str | None = None, site: str | None = None) -> None:
        # Un-materialize only on a true departure (external arm pick); a
        # gripper hop keeps the plate in the driver world for its move_plate.
        if self._is_internal_hop(mover):
            return
        if labware is not None:
            await self._unmaterialize(labware)
        await self.engine_commands.run("close", self.driver.close)

    async def _deck_holds(self, name: str) -> bool:
        return await self._deck_holds_on(self.driver, name)

    async def _deck_holds_on(
        self, driver: ILiquidHandlerWithProtocolDriver, name: str,
    ) -> bool:
        state = await self.engine_commands.keep_world(
            "get_deck_state", lambda: driver.get_deck_state(GetDeckStateRequest()),
        )
        return any(resource.name == name for resource in state.labware)

    async def _do_prepare_for_pick(self, labware: LabwareInstance | None, mover: IPlateMover, target: str | None = None, site: str | None = None) -> None:
        if target is None or self._is_internal_hop(mover):
            return
        await self.engine_commands.run("open", self.driver.open)

    async def _do_prepare_for_place(self, labware: LabwareInstance | None, mover: IPlateMover, target: str | None = None, site: str | None = None) -> None:
        if self._is_internal_hop(mover):
            return
        await self.engine_commands.run("open", self.driver.open)

    @asynccontextmanager
    async def reach_in_lease(self, mover: IPlateMover, purpose: str) -> AsyncIterator[None]:
        """Park the gantry off the deck and hold the device while ``mover`` reaches in.

        A handler holding tips or a plate refuses to park. The lease is released
        between asks, because putting down what it holds is a device call that
        needs this lock. Bounded, because work that ends without putting down
        what it picked up leaves the handler refusing for good.

        The handler's own gripper never leaves the deck, so it is not parked for.
        """
        driver = None if self._is_internal_hop(mover) else await self._parking_driver()
        give_up_after = time.monotonic() + REACH_IN_PATIENCE_SECONDS
        announced = False
        while True:
            async with self.lock.held_for(f"{purpose} by {mover.name}"):
                refusal = await self._park_unless_busy(driver)
                if refusal is None:
                    yield
                    return
            if time.monotonic() >= give_up_after:
                raise GantryWillNotParkError(
                    f"{self.name}: still mid-transfer after "
                    f"{REACH_IN_PATIENCE_SECONDS:.0f}s, so {mover.name} cannot "
                    f"reach into its deck. The handler is holding something it "
                    f"never put down: tips on a head, or a plate in its jaws. "
                    f"Its own refusal below says which. Clear that, then retry "
                    f"this move. ({refusal})"
                ) from refusal
            if not announced:
                announced = True
                orca_logger.info(
                    "%s is mid-transfer, so %s is waiting to reach into its "
                    "deck. (%s)", self.name, mover.name, refusal,
                )
            await asyncio.sleep(_REACH_IN_RETRY_SECONDS)

    async def _park_unless_busy(
        self, driver: _ParksItsOwnMovingParts | None,
    ) -> Exception | None:
        """Park, or return the handler's busy refusal. Any other failure raises."""
        if driver is None:
            return None
        try:
            await self.engine_commands.run(
                "park_gantry", lambda: driver.park_gantry(ParkGantryRequest(at=self._park_gripper_at)),
            )
        except Exception as exc:
            if not is_busy_refusal(exc):
                raise
            return exc
        return None

    async def _parking_driver(self) -> _ParksItsOwnMovingParts | None:
        """The bound driver if it can park, else None having warned once.

        Declaring ``park_gripper_at`` for a driver that cannot park is a topology
        error rather than a missing capability, so that one raises: the author
        asked for something specific and needs to hear it did not happen.
        """
        driver = self.driver
        if isinstance(driver, _ParksItsOwnMovingParts) and _advertises_parking(driver):
            return driver
        if self._park_gripper_at is not None:
            raise GantryWillNotParkError(
                f"{self.name}: park_gripper_at is declared but its driver "
                f"({type(driver).__name__}) cannot park."
            )
        await self._warn_it_cannot_park(driver)
        return None

    async def _warn_it_cannot_park(
        self, driver: ILiquidHandlerWithProtocolDriver,
    ) -> None:
        """Once per device per mode: a run makes this hop repeatedly.

        Only a live device gets the warning and its advice. In simulation the driver that cannot park is a simulator
        nobody chose, so the fact goes out at INFO with nothing to act on.
        """
        mode = await self.effective_mode()
        if mode in self._told_it_cannot_park:
            return
        self._told_it_cannot_park.add(mode)
        if mode is not WorkflowRunMode.LIVE:
            orca_logger.info(
                "%s: nothing parks a gantry in simulation. Its driver (%s) does not declare IGantryParking; the "
                "driver that has to park is the one a live device binds.",
                self.name,
                type(driver).__name__,
            )
            return
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
    handlers (driven via ``run_protocol``) and
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
    ) -> None:
        driver = resolve_driver(
            self.KIND, name, IDelidderDriver,
        )
        super().__init__(name, driver)

    async def delid(self):
        await self.driver.delid(DelidRequest())

class Waste(Device[IWasteDriver], IWaste):
    KIND: ClassVar[str] = "waste"

    def __init__(
        self,
        name: str,
    ) -> None:
        driver = resolve_driver(
            self.KIND, name, IWasteDriver,
        )
        super().__init__(name, driver)


class Storage(Device[IStorageDriver], IStorage, IPlateSource):
    KIND: ClassVar[str] = "storage"

    def __init__(
        self,
        name: str,
    ) -> None:
        driver = resolve_driver(
            self.KIND, name, IStorageDriver,
        )
        super().__init__(name, driver)

    def dispensing(self) -> AbstractAsyncContextManager[None]:
        return self.lock.held_for("dispense")

    async def dispense(self) -> None:
        await self.engine_commands.run("dispense", self.driver.dispense)

class Reader(Device[IReaderDriver], IReader):
    KIND: ClassVar[str] = "reader"

    def __init__(
        self,
        name: str,
    ) -> None:
        driver = resolve_driver(
            self.KIND, name, IReaderDriver,
        )
        super().__init__(name, driver)

    async def read(self, protocol_filepath: str, output_filepath: str) -> None:
        await self.driver.read(
            ReadRequest(protocol_filepath=protocol_filepath, output_filepath=output_filepath)
        )
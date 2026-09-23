import asyncio
import logging
from typing import ClassVar, List, Optional

from cheshire_drivers.interfaces import ITransporterDriver
from cheshire_drivers.sims import SimTransporterDriver
from cheshire_drivers.labware_models import LabwareIdentity
from cheshire_drivers.move_parameters import MoveParameterPatch
from cheshire_drivers.teachpoints import Teachpoint
from cheshire_drivers.transporter_models import (
    EnsureSeededRequest,
    InitializeRequest,
    PickAtCoordsRequest,
    PlaceAtCoordsRequest,
    ResetWorldRequest,
    UnseedPositionRequest,
)
from orca.resource_models.simulation_manager import SimulationManager
from orca.resource_models.resources import IModeAware
from orca.resource_models.transporter_base import TransporterBase
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import ILabwareLocationObserver, Location
from orca.runtime.db import create_memory_engine
from orca.runtime.device_factory_context import resolve_drivers
from orca.runtime.interfaces import (
    IGripProfileStore,
    IMoveDefaultsStore,
    ITeachpointStore,
)
from orca.runtime.move_parameters import ResolvedMoveParameters, resolve_move_parameters
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.sqlite_teachpoint_store import SqliteTeachpointStore
from orca.runtime.teachpoint_service import TeachpointService


orca_logger = logging.getLogger("orca")


def _labware_identity(labware: LabwareInstance) -> LabwareIdentity:
    """Project a `LabwareInstance` to its wire-shape `LabwareIdentity`.

    Carries the cross-process subset orca-client needs to keep its
    transporter sim graph consistent with orca-core's labware tracker.
    """
    return LabwareIdentity(
        labware_id=labware.id,
        barcode=labware.barcode,
        labware_type=labware.labware_type,
    )


class Transporter(TransporterBase, IModeAware):
    """Generic transporter resource bound to a teachpoint registry.

    Resolution is cloud-side here. `pick`/`place` look up the destination
    teachpoint via the bound store, walk any gateway chain
    outermost-first, and dispatch `pick_at_coords` / `place_at_coords` to
    the driver with a fully-resolved Teachpoint payload. Drivers receive
    coords, never names; the wire payload is the source of truth for the
    orca-client side.

    Mid-run mutations to the store are visible to the next move because
    resolution happens on every dispatch. The topology builder awaits
    each Teachpoint's `position_id` from the store via `get_teachpoints`
    (= `await store.list()`) to compute the reachability graph at build
    time.

    """

    KIND: ClassVar[str] = "transporter"
    # Driver the pure-sim path falls back to when no factory is bound. A
    # subclass that means a different machine (see `Translator`) names its
    # own, so the fallback cannot quietly hand it an arm.
    DEFAULT_SIM_DRIVER: ClassVar[type[ITransporterDriver]] = SimTransporterDriver

    def __init__(
        self,
        name: str,
        teachpoint_store: Optional[ITeachpointStore] = None,
        sim_override: WorkflowRunMode | None = None,
    ) -> None:
        super().__init__(name)
        live_driver, sim_driver_resolved = resolve_drivers(
            self.KIND, name, self.DEFAULT_SIM_DRIVER,
        )
        # Per-device run-mode override declared at topology construction time.
        # See `Device.sim_override` for the contract; the same field is read
        # by `SystemTopologyRegistry` for both Devices and Transporters and
        # threaded onto `SimulationManager` so dispatch routes through the
        # v3.4 12-row resolver natively.
        self._sim_override = sim_override
        self._sim_manager = SimulationManager(
            live_driver,
            sim_driver_resolved,
            sim_override=sim_override,
        )
        self._teachpoint_store: ITeachpointStore = (
            teachpoint_store
            or TeachpointService(SqliteTeachpointStore(create_memory_engine()))
        )
        self._move_defaults_store: Optional[IMoveDefaultsStore] = None
        self._position_aliases: dict[str, str] = {}

    @property
    def driver(self) -> ITransporterDriver:
        return self._sim_manager.driver

    def mode_under(self, base: WorkflowRunMode) -> WorkflowRunMode:
        """The run mode this transporter dispatches under given a base mode.

        Same answer the driver swap uses; see `SimulationManager`.
        """
        return self._sim_manager.mode_under(base)

    def driver_under(self, base: WorkflowRunMode) -> ITransporterDriver:
        """The driver a dispatch under `base` lands on; see `SimulationManager`."""
        return self._sim_manager.driver_under(base)

    @property
    def effective_mode(self) -> WorkflowRunMode:
        """The run mode this transporter is dispatching under right now.

        Same answer the driver swap uses; see `SimulationManager`.
        """
        return self._sim_manager.effective_mode

    @property
    def live_driver(self) -> ITransporterDriver:
        """Live driver, unconditionally - for metadata-only reads.

        Mirror of `Device.live_driver`; see that contract.
        """
        return self._sim_manager.live_driver

    @property
    def single_carriage(self) -> bool:
        """Sourced from the driver: the hardware's proxy owns the physical
        truth (a translator driver declares it; a PF400/arm driver does not).
        Metadata read via live_driver, mode-independent by contract."""
        return self.live_driver.single_carriage

    @property
    def is_initialized(self) -> bool:
        """Returns whether the transporter is initialized or not."""
        return self.driver.is_initialized
    
    async def initialize(self) -> None:
        """Opens the link if it is down, then brings the arm up.

        Asks for no motion. Homing is `home`, asked for on its own: an arm that
        has not homed since power-on refuses the first move that needs a known
        position, and that refusal is the prompt to home deliberately rather
        than have every bring-up sweep the arm through whatever is in front of
        it. A driver whose vendor stack offers only a compound bring-up homes
        anyway, and logs that it did.
        """
        if not self.driver.is_connected:
            await self.driver.connect()
        await self.driver.initialize(InitializeRequest())

    async def connect(self) -> None:
        """Opens the link to the transporter. Moves nothing."""
        await self.driver.connect()

    async def disconnect(self) -> None:
        """Hands the transporter back. Moves nothing, but drops motor power."""
        await self.driver.disconnect()

    @property
    def sim_override(self) -> WorkflowRunMode | None:
        """Per-device run-mode override declared at construction time.

        Mirror of `Device.sim_override`. Surfaced on
        `TopologyCard.topology_sim_override` via the topology registry.
        """
        return self._sim_override

    async def ensure_initialized(self) -> None:
        """Bring this mover up if the run's bring-up walk did not.

        The walk only covers what a workflow declares, so a thread body that
        moves labware somewhere undeclared reaches a mover nobody brought up.
        Doing it here rather than widening the walk keeps a run that never
        touches the arm from driving it. PURE_SIM needs no bring-up, matching
        the walk.
        """
        if self.effective_mode is WorkflowRunMode.PURE_SIM:
            return
        if self.is_initialized:
            return
        await self.initialize()

    async def _do_pick(self, location: Location) -> None:
        assert location.labware is not None
        await self.ensure_initialized()
        teachpoint = await self._resolve_teachpoint(location.position_id)
        gateway_path = await self._resolve_gateway_path(teachpoint)
        handling = await self.resolve_handling(
            teachpoint, location.labware.labware_type,
            location.labware.carry_override,
            location.labware.size_z,
        )
        await self.driver.pick_at_coords(PickAtCoordsRequest(
            teachpoint=teachpoint,
            labware_type=location.labware.labware_type,
            gateway_path=gateway_path,
            expected_labware=_labware_identity(location.labware),
            handling=handling.parameters,
        ))

    async def _do_place(self, location: Location) -> None:
        held = self.labware
        assert held is not None
        teachpoint = await self._resolve_teachpoint(location.position_id)
        gateway_path = await self._resolve_gateway_path(teachpoint)
        handling = await self.resolve_handling(
            teachpoint, held.labware_type,
            held.carry_override,
            held.size_z,
        )
        await self.driver.place_at_coords(PlaceAtCoordsRequest(
            teachpoint=teachpoint,
            labware_type=held.labware_type,
            gateway_path=gateway_path,
            expected_labware=_labware_identity(held),
            handling=handling.parameters,
        ))

    def bind_move_defaults(self, store: IMoveDefaultsStore) -> None:
        """Inject the deployment's editable defaults for this transporter.

        The runtime binds this at start rather than the topology author wiring it,
        so a deployment that never mentions move defaults still resolves against
        the stored row instead of quietly falling back to the seed.
        """
        self._move_defaults_store = store

    async def resolve_handling(
        self, teachpoint: Teachpoint, labware_type: str | None = None,
        carry_override: MoveParameterPatch | None = None,
        labware_height: float | None = None,
    ) -> ResolvedMoveParameters:
        """Everything the arm needs for this move, narrowed layer by layer.

        Resolution happens here rather than in the driver because this is where a
        labware instance and a teachpoint are in scope at the same moment. The
        driver receives numbers, and stays swappable because of it.

        `labware_type` is what is being carried. Omitting it resolves the move
        without the labware layer, which is the honest answer for a caller asking
        "how does this arm reach that position" rather than "how will it hold
        this plate".

        `carry_override` is what this one piece of labware says about how it is
        being carried right now. An ad-hoc caller holding only a type has none,
        which is the honest answer: it is asking about the type, not about a
        particular plate.

        `labware_height` is how tall the thing being carried is, read off the
        labware itself. It is the base of the labware layer, under any grip
        profile, so a profile can still overrule it. Omitting it leaves the
        height to the layers above, which is right for a caller that is not
        carrying anything and wrong for one that is.

        Public because the ad-hoc operator surfaces resolve the same way an
        execution does. Two answers to "how is this labware handled" is how a
        plate ends up moving differently depending on who asked for the move.
        """
        resolved = resolve_move_parameters(
            await self.move_defaults(),
            teachpoint,
            self._labware_layer(labware_height, await self.grip_profile(labware_type)),
            labware_type,
            carry_override,
        )
        orca_logger.debug(
            "%s move parameters at %s: %s (from %s)",
            self, teachpoint.position_id, resolved.parameters, resolved.sources,
        )
        return resolved

    async def resolve_handling_at(
        self, position_id: str, labware_type: str | None = None,
    ) -> ResolvedMoveParameters:
        """`resolve_handling` for a caller holding only a position id."""
        return await self.resolve_handling(
            await self._resolve_teachpoint(position_id), labware_type,
        )

    @staticmethod
    def _labware_layer(
        height: float | None, profile: Optional[MoveParameterPatch],
    ) -> Optional[MoveParameterPatch]:
        """What the labware contributes: its height, then anything measured for it."""
        if height is None:
            return profile
        if profile is None:
            return MoveParameterPatch(resource_height=height)
        fields = {"resource_height": height}
        fields.update(profile.model_dump(exclude_none=True))
        return MoveParameterPatch(**fields)


    async def move_defaults(self) -> MoveParameterPatch:
        """What this deployment has tuned for this arm; empty when nobody has.

        Public because a bare gripper command has no teachpoint to narrow by and
        still has to reach the same layer-one edit a move resolves through.
        """
        if self._move_defaults_store is None:
            return MoveParameterPatch()
        return await self._move_defaults_store.get(self.name) or MoveParameterPatch()

    async def reset_world(self) -> None:
        """Wipe this transporter's world projection (authoritative clear).

        Reaches orphaned seeds the per-move unseed path can't. Forwards to the
        effective driver (real-hardware drivers no-op); also drops this
        transporter's own gripper-held identity.
        """
        await self.driver.reset_world(ResetWorldRequest())
        await super().reset_labware_state()

    async def reset_labware_state_everywhere(self) -> None:
        """The same clear against both driver worlds. Driven by clear-all.

        An operator's clear-all resolves to LIVE while the seeds a sim run left
        are in the sim driver's world, and the panic button exists to leave
        neither behind. A driver that will not answer is logged and skipped, so
        one dead arm cannot stop the others being cleared.
        """
        for driver in self._sim_manager.all_drivers:
            try:
                await driver.reset_world(ResetWorldRequest())
            except Exception as exc:
                orca_logger.warning(
                    "%s: a world would not reset (%s); it may still hold seeds.",
                    self.name, exc,
                )
        await super().reset_labware_state()

    async def reset_labware_state(self) -> None:
        """Reset this transporter's world projection. Satisfies
        `ILabwareStateHolder`; delegates to `reset_world`."""
        await self.reset_world()

    def register_position_alias(self, node_id: str, taught_name: str) -> None:
        """Map a flat site node back to the operator's taught name: a
        device-named teachpoint covers that device's arm-reachable sites, so
        coordinate resolution follows the same alias."""
        self._position_aliases[node_id] = taught_name

    def covers_position(self, position_id: str) -> bool:
        return position_id in self._position_aliases

    def _taught_name(self, position_id: str) -> str:
        return self._position_aliases.get(position_id, position_id)

    async def _resolve_teachpoint(self, position_id: str) -> Teachpoint:
        """Resolve a Teachpoint for the given position_id via the bound store.

        Returns a fully-flattened `Teachpoint` (access fields inlined) suitable
        for the wire-source-of-truth contract. Raises if the store has no
        Teachpoint registered for `position_id` on this transporter.
        """
        tp = await self._teachpoint_store.resolve(position_id)
        if tp is None and position_id in self._position_aliases:
            tp = await self._teachpoint_store.resolve(self._position_aliases[position_id])
        if tp is None:
            raise ValueError(
                f"{self}: no teachpoint registered for position_id {position_id!r} on this transporter"
            )
        return tp

    async def _resolve_gateway_path(self, tp: Teachpoint) -> List[Teachpoint]:
        """Walk the gateway chain outermost-first.

        Each teachpoint can declare a `gateway` name pointing at an outer
        waypoint; the chain is followed until a teachpoint has no gateway.
        The returned list is reversed so callers can iterate "approach"
        order (outermost gateway first, destination last) and reverse for
        retract.

        Raises on circular references or missing gateway teachpoints.
        """
        path: List[Teachpoint] = []
        visited: set[str] = set()
        current = tp.gateway
        while current:
            if current in visited:
                raise ValueError(
                    f"{self}: circular gateway reference detected at {current!r}"
                )
            visited.add(current)
            gateway_tp = await self._teachpoint_store.resolve(current)
            if gateway_tp is None:
                raise ValueError(
                    f"{self}: gateway teachpoint {current!r} is not registered"
                )
            path.append(gateway_tp)
            current = gateway_tp.gateway
        return list(reversed(path))

    async def get_teachpoints(self) -> List[Teachpoint]:
        """The store-backed view of this transporter's teachpoints.

        The topology builder awaits this to determine reachability at build
        time. Path A: drivers consult the store at every dispatch, so this
        returns the System-graph-builder view directly from the store; the
        driver does not maintain an independent working set.
        """
        return await self._teachpoint_store.list()

    @property
    def teachpoint_store(self) -> ITeachpointStore:
        """The registry that backs this transporter's named positions."""
        return self._teachpoint_store

    async def notify_labware_location_change(
        self, event: str, location: Location, labware: LabwareInstance
    ) -> None:
        """Mirror `location.labware` into the driver's labware world.

        The driver's wire-shape ops (`seed_position` / `ensure_seeded` /
        `unseed_position`) accept a `LabwareIdentity` -- the rule is
        uniform across every event boundary: read `location.labware` and
        make the driver agree.

          * has labware -> ensure_seeded (idempotent)
          * is None     -> unseed_position (idempotent)

        Works for every location flavor without special cases: PlatePad
        (label tracks 1:1 with the pad's content), Gateway-shaker
        (briefly staged then loaded into interior, stage clears),
        Gateway-LH-deck (briefly staged then routed to a child slot,
        stage clears). The driver follows the stage state, not the loaded
        state, because the stage is the only thing transporters touch.

        Real-hardware drivers (PLR-backed) implement these as no-ops --
        physical state is the source of truth on hardware. Sim drivers
        (in-process or remote/wire) consume the dispatch to keep their
        synthetic graphs in sync with the cloud-side ledger.
        """
        identity = _labware_identity(labware)
        if location.accessible_labware is labware:
            await self.driver.ensure_seeded(
                EnsureSeededRequest(
                    position_id=self._taught_name(location.position_id),
                    labware=identity,
                )
            )
        else:
            # Not at the approach point (picked away OR loaded inside the
            # device): the taught position no longer presents this plate.
            await self.driver.unseed_position(
                UnseedPositionRequest(
                    position_id=self._taught_name(location.position_id),
                    labware=identity,
                )
            )

    def __str__(self) -> str:
        return f"Transporter: {self._name}"
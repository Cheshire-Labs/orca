import logging
from collections.abc import Iterable
from typing import ClassVar, List, Optional

from cheshire_drivers.interfaces import ITransporterDriver
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
from orca.gateway.controller.exceptions import CommandNotSavedError
from orca.modes.device_modes import DeviceMode, IDeviceModes
from orca.resource_models.device_error import TeachpointNotFoundError
from orca.resource_models.device_run_mode import DeviceRunMode
from orca.resource_models.resources import EngineCommandLog, IModeAware, engine_time_limit
from orca.resource_models.transporter_base import TransporterBase
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.driver_management.device_factory_context import resolve_driver
from orca.resource_models.store_interfaces import (
    IMoveDefaultsStore,
    ITeachpointStore,
)
from orca.resource_models.move_parameters import ResolvedMoveParameters, resolve_move_parameters
from orca.modes.run_modes import BaseMode, WorkflowRunMode, mode_scope, out_of_run


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

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self._driver = resolve_driver(self.KIND, name, ITransporterDriver)
        self._run_mode = DeviceRunMode(name)
        self._engine_commands = EngineCommandLog(name, self._engine_time_limit)
        self._teachpoint_store: ITeachpointStore | None = None
        self._move_defaults_store: Optional[IMoveDefaultsStore] = None
        self._position_aliases: dict[str, str] = {}

    @property
    def driver(self) -> ITransporterDriver:
        return self._driver

    def _engine_time_limit(self, command: str) -> float | None:
        return engine_time_limit(type(self._driver), command)

    async def mode_under(self, base: WorkflowRunMode) -> WorkflowRunMode:
        """The run mode this transporter dispatches under given a base mode; see `DeviceRunMode`."""
        return await self._run_mode.mode_under(base)

    async def effective_mode(self) -> WorkflowRunMode:
        """The run mode this transporter is dispatching under right now; see `DeviceRunMode`."""
        return await self._run_mode.effective_mode()

    @property
    def single_carriage(self) -> bool:
        """Sourced from the driver: the hardware's proxy owns the physical
        truth (a translator driver declares it; an arm driver does not)."""
        return self.driver.single_carriage

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
    def engine_commands(self) -> EngineCommandLog:
        return self._engine_commands

    def use_device_modes(self, modes: IDeviceModes, base_mode: BaseMode = out_of_run) -> None:
        self._run_mode.use_device_modes(modes, base_mode)

    async def setting(self) -> DeviceMode:
        return await self._run_mode.setting()

    async def ensure_initialized(self) -> None:
        """Before a pick, bring the arm up if this server session has not, or the orca-client session it brought up has ended.

        Sends connect when this session has not opened the link, then initialize, saved as one engine command.
        """
        if self.is_initialized:
            return
        await self.engine_commands.run("initialize", self.initialize)

    async def _do_pick(self, location: Location) -> None:
        labware = location.labware
        await self.ensure_initialized()
        teachpoint = await self._resolve_teachpoint(location.position_id)
        gateway_path = await self._resolve_gateway_path(teachpoint)
        handling = await self._handling_for(teachpoint, labware)
        # No labware type with nothing on record: the driver grips with its own default.
        request = PickAtCoordsRequest(
            teachpoint=teachpoint,
            labware_type="" if labware is None else labware.labware_type,
            gateway_path=gateway_path,
            expected_labware=None if labware is None else _labware_identity(labware),
            handling=handling.parameters,
        )
        await self.engine_commands.run("pick_at_coords", lambda: self.driver.pick_at_coords(request))

    async def _do_place(self, location: Location) -> None:
        held = self.labware
        teachpoint = await self._resolve_teachpoint(location.position_id)
        gateway_path = await self._resolve_gateway_path(teachpoint)
        handling = await self._handling_for(teachpoint, held)
        request = PlaceAtCoordsRequest(
            teachpoint=teachpoint,
            labware_type="" if held is None else held.labware_type,
            gateway_path=gateway_path,
            expected_labware=None if held is None else _labware_identity(held),
            handling=handling.parameters,
        )
        await self.engine_commands.run("place_at_coords", lambda: self.driver.place_at_coords(request))

    async def _handling_for(self, teachpoint: Teachpoint, labware: LabwareInstance | None) -> ResolvedMoveParameters:
        if labware is None:
            return await self.resolve_handling(teachpoint)
        return await self.resolve_handling(teachpoint, labware.labware_type, labware.carry_override, labware.size_z)

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
        return MoveParameterPatch.model_validate(
            {"resource_height": height, **profile.model_dump(exclude_none=True)},
        )


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
        await self.engine_commands.keep_world("reset_world", lambda: self.driver.reset_world(ResetWorldRequest()))
        await super().reset_labware_state()

    async def rebuild_world_from(self, locations: Iterable[Location]) -> bool:
        """Clear the world this mover dispatches in, then seed each position it watches that the ledger says holds labware.

        A mover holding a plate is left alone and False is returned: its world is mid-move, and a clear would drop
        the plate it carries. It is rebuilt at a later command, once its jaws are empty.
        """
        if self.labware is not None:
            return False
        await self.engine_commands.keep_world("reset_world", lambda: self.driver.reset_world(ResetWorldRequest()))
        for location in locations:
            labware = location.labware
            if labware is not None and location.is_observed_by(self):
                await self.notify_labware_location_change("rebuilt", location, labware)
        return True

    async def reset_labware_state_everywhere(self) -> None:
        """The same clear in every world this transporter dispatches to: its simulator's and its real driver's.

        An operator's clear-all resolves to LIVE while the seeds a sim run left are in the simulator's
        world, and the panic button exists to leave neither behind. A world that will not answer is
        logged and skipped, so one dead arm cannot stop the others being cleared.
        """
        reached: set[WorkflowRunMode] = set()
        for base in (WorkflowRunMode.SIM, WorkflowRunMode.LIVE):
            world = await self.mode_under(base)
            if world in reached:
                continue
            with mode_scope(base):
                try:
                    await self.engine_commands.keep_world("reset_world", lambda: self.driver.reset_world(ResetWorldRequest()))
                except CommandNotSavedError:
                    raise
                except Exception as exc:
                    orca_logger.warning(
                        "%s: its %s world would not reset (%s); it may still hold seeds.",
                        self.name, world.value, exc,
                    )
                    continue
            reached.add(world)
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
        tp = await self.teachpoint_store.resolve(position_id)
        if tp is None and position_id in self._position_aliases:
            tp = await self.teachpoint_store.resolve(self._position_aliases[position_id])
        if tp is None:
            raise TeachpointNotFoundError(
                f"No teachpoint registered for position_id {position_id!r} on device {self._name!r}",
                available_teachpoints=[taught.position_id for taught in await self.teachpoint_store.list()],
            )
        return tp

    async def refuse_unless_taught(self, position_id: str) -> None:
        await self._resolve_teachpoint(position_id)

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
            gateway_tp = await self.teachpoint_store.resolve(current)
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
        return await self.teachpoint_store.list()

    def bind_teachpoints(self, store: ITeachpointStore) -> None:
        """The build attaches `teachpoints/<name>.json`; a topology never passes one."""
        self._teachpoint_store = store

    @property
    def teachpoint_store(self) -> ITeachpointStore:
        """The registry that backs this transporter's named positions."""
        if self._teachpoint_store is None:
            raise RuntimeError(
                f"transporter {self.name!r} has no teachpoints attached; "
                "build_system attaches teachpoints/<name>.json",
            )
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
            request = EnsureSeededRequest(position_id=self._taught_name(location.position_id), labware=identity)
            await self.engine_commands.keep_world("ensure_seeded", lambda: self.driver.ensure_seeded(request))
        else:
            # Not at the approach point (picked away OR loaded inside the
            # device): the taught position no longer presents this plate.
            removal = UnseedPositionRequest(position_id=self._taught_name(location.position_id), labware=identity)
            await self.engine_commands.keep_world("unseed_position", lambda: self.driver.unseed_position(removal))

    def __str__(self) -> str:
        return f"Transporter: {self._name}"
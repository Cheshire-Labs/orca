"""One small lab, built the way a user builds one, and the reads every SDK feature test checks against.

Every device runs on the check run's real device path with a driver that records each command, so a test
can read what each device was asked to do, each plate's stored record, the operation history and the events.
"""

import asyncio
import time
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TypeVar

from cheshire_drivers import CartesianCoordinates as C
from cheshire_drivers import DeckLayoutConfig, DeckResourceConfig, Teachpoint
from cheshire_drivers.liquid_handler_models import (
    AspirateRequest,
    DiscardTipsRequest,
    DispenseRequest,
    LabwareStateResponse,
    MovePlateRequest,
    PickUpTipsRequest,
)
from cheshire_drivers.protocol_runner_models import RunProtocolRequest
from cheshire_drivers.shaker_models import ShakeRequest
from cheshire_drivers.sims import (
    SimShakerDriver,
    SimStorageDriver,
    SimTransporterDriver,
)
from cheshire_drivers.transporter_models import (
    PickAtCoordsRequest,
    PlaceAtCoordsRequest,
)
from pydantic import JsonValue

import orca.orca as orca
from orca.devices.devices import LiquidHandler, Storage
from orca.devices.shaker import Shaker
from orca.events.event_bus import EventBus
from orca.events.runtime_event import RuntimeEvent
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.labware import CanContinueFn, LabwareInitialState
from orca.resource_models.plate_hotel import PlateHotel
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.resource_pool import ResourcePool
from orca.resource_models.sharing import GroupSharing, SubmissionBatching
from orca.resource_models.transporter import Transporter
from orca.runtime.sinks import CollectorSink
from orca.runtime.status_models import ThreadSnapshot
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import LocationValue, SystemBuild, Topology
from orca.sdk.labware import PlateTemplate, TipRackTemplate
from orca.spawn import LEAVE_IN_PLACE, REUSE_EXISTING
from orca.state.ops_store import IOpsHistoryStore
from orca.state.records import TrackingRecord
from orca.variables.errors import OptionValue
from orca.workflow_models.labware_group import LabwareGroup
from orca.workflow_models.method_template import IMethodTemplate, MethodTemplate
from orca.workflow_models.status_enums import RecoveryDecision
from orca.workflow_models.submission import Submission
from orca.workflow_models.submission_modes import BatchMode
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.thread_template import ThreadTemplate
from orca.workflow_models.workflow_templates import WorkflowTemplate
from tests.memory_activity import memory_ops_store, memory_runtime
from tests.real_device_path import the_real_device_path
from tests.recording_handler import RecordingHandler
from tests.setup_package import (
    PackageSetupStoreFactory,
    package_stores,
    with_deck_layouts,
    with_teachpoints,
    write_deck_layouts,
    write_teachpoints,
)
from tests.test_helpers import state_every_liquid_handler_head_empty, wait_until

PLATE_TYPE = "Cor_Falcon_96_wellplate_340ul_Fb_Black"
TIP_TYPE = "hamilton_96_tiprack_10uL_filter"
DECK = DeckLayoutConfig(
    deck_type="STARlet",
    resources=[
        DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
        DeckResourceConfig(name="carrier-15", catalog_ref="TIP_CAR_480_A00", rail=15),
    ],
)
PLATE_SITE = "carrier-7-0"
SECOND_PLATE_SITE = "carrier-7-1"
TIP_SITE = "carrier-15-0"
TIPS_START = f"lh/{TIP_SITE}"
LH_ENTRY = "lh/carrier-7-2"
SHELVES = ("hotel/shelf_1", "hotel/shelf_2", "hotel/shelf_3")
GRIPPER = "arm/gripper"
FINAL = ("COMPLETED", "ABORTED", "STOPPED", "FAILED")


@dataclass(frozen=True)
class RecordedCommand:
    device: str
    method: str
    args: dict[str, JsonValue]
    started: float
    ended: float | None = None


class Hold:
    """A command that starts and then waits until the test lets it finish."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.finish = asyncio.Event()

    async def wait(self) -> None:
        self.started.set()
        await self.finish.wait()


@dataclass
class _Log:
    """What one device's driver was asked to do, with an optional hold or failure per method."""

    device: str
    commands: list[RecordedCommand] = field(default_factory=list)
    holds: dict[str, Hold] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)
    """Fail the next N calls of a method."""
    fail_at: dict[str, set[int]] = field(default_factory=dict)
    """Fail these calls of a method, counted from 1."""

    async def run(self, method: str, args: dict[str, JsonValue]) -> int:
        """Record the start, honour a hold or a planned failure; returns the index to close with `ended`."""
        self.commands.append(RecordedCommand(self.device, method, args, time.perf_counter()))
        index = len(self.commands) - 1
        hold = self.holds.get(method)
        if hold is not None:
            await hold.wait()
        call = sum(1 for c in self.commands if c.method == method)
        planned = self.failures.get(method, 0) > 0 or call in self.fail_at.get(method, set())
        if planned:
            self.failures[method] = max(0, self.failures.get(method, 0) - 1)
            self.ended(index)
            raise RuntimeError(f"simulated {method} failure on {self.device}")
        return index

    def ended(self, index: int) -> None:
        done = self.commands[index]
        self.commands[index] = RecordedCommand(done.device, done.method, done.args, done.started, time.perf_counter())


class KitShaker(SimShakerDriver):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.log = _Log(name)

    async def shake(self, request: ShakeRequest) -> None:
        index = await self.log.run("shake", request.model_dump(mode="json"))
        await super().shake(request)
        self.log.ended(index)


def _arm_args(request: PickAtCoordsRequest | PlaceAtCoordsRequest) -> dict[str, JsonValue]:
    """The request, with its teachpoint flattened to the position, coordinates and access it names."""
    args: dict[str, JsonValue] = request.model_dump(mode="json", exclude={"teachpoint", "gateway_path"})
    teachpoint = request.teachpoint
    coordinates = teachpoint.coordinates
    args["position_id"] = teachpoint.position_id
    args["coordinates"] = None if coordinates is None else coordinates.model_dump(mode="json")
    args["access_type"] = teachpoint.access_type
    args["gripper_offset"] = teachpoint.gripper_offset
    args["vertical_clearance"] = teachpoint.vertical_clearance
    args["horizontal_clearance"] = teachpoint.horizontal_clearance
    return args


class KitArm(SimTransporterDriver):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.log = _Log(name)

    async def pick_at_coords(self, request: PickAtCoordsRequest) -> None:
        index = await self.log.run("pick_at_coords", _arm_args(request))
        await super().pick_at_coords(request)
        self.log.ended(index)

    async def place_at_coords(self, request: PlaceAtCoordsRequest) -> None:
        index = await self.log.run("place_at_coords", _arm_args(request))
        await super().place_at_coords(request)
        self.log.ended(index)


class KitStorage(SimStorageDriver):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.log = _Log(name)

    async def dispense(self) -> None:
        index = await self.log.run("dispense", {})
        await super().dispense()
        self.log.ended(index)


class KitHandler(RecordingHandler):
    """The recording liquid handler, with the same hold and failure controls as the other kit drivers."""

    def __init__(self, name: str = "lh") -> None:
        super().__init__()
        self.log = _Log(name)

    async def aspirate(self, request: AspirateRequest) -> LabwareStateResponse:
        index = await self.log.run("aspirate", request.model_dump(mode="json"))
        response = await super().aspirate(request)
        self.log.ended(index)
        return response

    async def dispense(self, request: DispenseRequest) -> LabwareStateResponse:
        index = await self.log.run("dispense", request.model_dump(mode="json"))
        response = await super().dispense(request)
        self.log.ended(index)
        return response

    async def pick_up_tips(self, request: PickUpTipsRequest) -> LabwareStateResponse:
        index = await self.log.run("pick_up_tips", request.model_dump(mode="json"))
        response = await super().pick_up_tips(request)
        self.log.ended(index)
        return response

    async def run_protocol(self, request: RunProtocolRequest) -> None:
        index = await self.log.run("run_protocol", request.model_dump(mode="json"))
        await super().run_protocol(request)
        self.log.ended(index)

    async def move_plate(self, request: MovePlateRequest) -> None:
        index = await self.log.run("move_plate", request.model_dump(mode="json"))
        await super().move_plate(request)
        self.log.ended(index)

    async def discard_tips(self, request: DiscardTipsRequest) -> LabwareStateResponse:
        index = await self.log.run("discard_tips", request.model_dump(mode="json"))
        response = await super().discard_tips(request)
        self.log.ended(index)
        return response


def plate_template(
    name: str = "plate",
    volume: float = 100.0,
    *,
    group_sharing: GroupSharing = GroupSharing.PER_GROUP,
    submission_batching: SubmissionBatching = SubmissionBatching.ISOLATED,
    can_continue_fn: CanContinueFn | None = None,
) -> PlateTemplate:
    return PlateTemplate(
        name, labware_type=PLATE_TYPE, initial_state=LabwareInitialState(uniform_volume=volume),
        group_sharing=group_sharing, submission_batching=submission_batching, can_continue_fn=can_continue_fn,
    )


def tips_template(*, shared: bool = False) -> TipRackTemplate:
    """A tip rack; `shared` lets every group in a submission, and every joining submission, use the one rack."""
    if not shared:
        return TipRackTemplate("tips", labware_type=TIP_TYPE, with_tips=True)
    return TipRackTemplate(
        "tips", labware_type=TIP_TYPE, with_tips=True,
        group_sharing=GroupSharing.SHARED_ACROSS_GROUPS, submission_batching=SubmissionBatching.BATCHABLE,
    )


def tips_thread(tips: TipRackTemplate, allows: list[MethodTemplate]) -> ThreadTemplate:
    """A deck-resident tip rack that serves every listed method while any feeder is live."""
    @orca.thread(labware=tips, start=(TIPS_START, REUSE_EXISTING), end=(TIPS_START, LEAVE_IN_PLACE))
    async def tips_journey(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        while ctx.has_more_work():
            yield orca.join(allows=allows)

    return tips_journey


_TransporterT = TypeVar("_TransporterT", bound=Transporter)
KitDriver = KitShaker | KitArm | KitStorage | KitHandler


def taught(stores: PackageSetupStoreFactory, transporter: _TransporterT, positions: Sequence[str]) -> _TransporterT:
    """`transporter` with one teachpoint per position, written to its setup file."""
    return with_teachpoints(transporter, write_teachpoints(stores, transporter.name, [
        Teachpoint(position, C(100.0 * i, 200, 300, 0, 90, 180), orientation="right")
        for i, position in enumerate(positions)
    ]))


@dataclass
class Lab:
    """The devices a test's workflow names. Built before the workflow, because its actions reference them."""

    stores: PackageSetupStoreFactory
    shaker_1: Shaker
    shaker_2: Shaker
    shakers: ResourcePool
    lh: LiquidHandler
    arm: Transporter
    hotel: PlateHotel
    pad_1: PlatePad
    pad_2: PlatePad
    stacker: Storage
    waste: Storage
    drivers: dict[str, KitDriver]
    more_locations: dict[str, LocationValue] = field(default_factory=dict)
    more_pools: list[ResourcePool] = field(default_factory=list)
    more_transporters: list[Transporter] = field(default_factory=list)

    def pool(self, name: str) -> ResourcePool:
        return next(p for p in [self.shakers, *self.more_pools] if p.name == name)

    def driver(self, name: str) -> KitDriver:
        return self.drivers[name]

    def log(self, name: str) -> _Log:
        return self.drivers[name].log

    def topology(self) -> Topology:
        locations: dict[str, LocationValue] = {
            "shaker_1": self.shaker_1, "shaker_2": self.shaker_2, "lh": self.lh, "hotel": self.hotel,
            "pad_1": self.pad_1, "pad_2": self.pad_2, "stacker": self.stacker, "waste": self.waste,
            **self.more_locations,
        }
        return Topology(
            locations=locations, transporters=[self.arm, *self.more_transporters], pools=[self.shakers, *self.more_pools],
        )


def new_lab(*, extra_teachpoints: Sequence[str] = (), second_handler: bool = False) -> Lab:
    """Two pooled shakers, a liquid handler with a deck, an arm, a three-shelf hotel, two pads, a stacker and a waste.

    `second_handler` adds `lh_2`, a copy of `lh`, and a pool `handlers` holding both.
    """
    path = the_real_device_path()
    stores = package_stores()
    drivers: dict[str, KitDriver] = {
        "shaker_1": KitShaker("shaker_1"), "shaker_2": KitShaker("shaker_2"), "arm": KitArm("arm"),
        "stacker": KitStorage("stacker"), "waste": KitStorage("waste"), "lh": KitHandler(),
    }
    for name, driver in drivers.items():
        path.simulate_with(name, driver)
    shaker_1, shaker_2 = Shaker("shaker_1"), Shaker("shaker_2")
    lh = with_deck_layouts(LiquidHandler("lh", deck_layout="default"), write_deck_layouts(stores, "lh", {"default": DECK}))
    second_entry = [LH_ENTRY.replace("lh/", "lh_2/")] if second_handler else []
    arm = taught(stores, Transporter("arm"), [
        "stacker", "waste", "pad_1", "pad_2", *SHELVES, "shaker_1", "shaker_2", LH_ENTRY, *second_entry, *extra_teachpoints,
    ])
    lab = Lab(
        stores=stores, shaker_1=shaker_1, shaker_2=shaker_2, shakers=ResourcePool("shakers", [shaker_1, shaker_2]),
        lh=lh, arm=arm, hotel=PlateHotel("hotel", shelves=3), pad_1=PlatePad("pad_1"), pad_2=PlatePad("pad_2"),
        stacker=Storage("stacker"), waste=Storage("waste"), drivers=drivers,
    )
    if second_handler:
        drivers["lh_2"] = KitHandler("lh_2")
        path.simulate_with("lh_2", drivers["lh_2"])
        lh_2 = with_deck_layouts(
            LiquidHandler("lh_2", deck_layout="default"), write_deck_layouts(stores, "lh_2", {"default": DECK}),
        )
        lab.more_locations["lh_2"] = lh_2
        lab.more_pools.append(ResourcePool("handlers", [lh, lh_2]))
    return lab


@dataclass
class FeatureWorld:
    lab: Lab
    build: SystemBuild
    runtime: SystemRuntime
    ops: IOpsHistoryStore
    collector: CollectorSink

    @property
    def workflow(self) -> WorkflowTemplate:
        workflow = self.build.workflow
        assert workflow is not None
        return workflow


async def build_world(lab: Lab, workflow: WorkflowTemplate) -> FeatureWorld:
    """Build through `orca.build_system` and start the runtime. Shut it down with `close_world`."""
    ops = memory_ops_store()
    build = await orca.build_system(
        name="SDK features", workflow=workflow, topology=lab.topology(), stores=lab.stores,
        event_bus=EventBus(), ops_history_store=ops,
    )
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    collector = CollectorSink()
    runtime.register_sink(collector)
    await runtime.start()
    await state_every_liquid_handler_head_empty(runtime)
    return FeatureWorld(lab, build, runtime, ops, collector)


async def close_world(world: FeatureWorld) -> None:
    for driver in world.lab.drivers.values():
        for hold in driver.log.holds.values():
            hold.finish.set()
    await world.runtime.shutdown(confirm=True)


async def submit_one(
    world: FeatureWorld,
    *,
    variables: Mapping[str, OptionValue] | None = None,
    groups: Sequence[LabwareGroup] = (),
    batch_mode: BatchMode = BatchMode.STANDALONE,
    deployment_profile: str | None = None,
    mode: WorkflowRunMode = WorkflowRunMode.CHECK_RUN,
) -> Submission:
    """Submit the world's workflow, as a check run unless `mode` says otherwise."""
    return await world.runtime.submit(
        world.workflow, groups=groups, variables=variables, batch_mode=batch_mode,
        deployment_profile=deployment_profile, mode=mode,
    )


async def submit(
    world: FeatureWorld,
    *,
    variables: Mapping[str, OptionValue] | None = None,
    groups: Sequence[LabwareGroup] = (),
    batch_mode: BatchMode = BatchMode.STANDALONE,
    mode: WorkflowRunMode = WorkflowRunMode.CHECK_RUN,
) -> str:
    """Submit the world's workflow; returns the execution id."""
    return (await submit_one(world, variables=variables, groups=groups, batch_mode=batch_mode, mode=mode)).execution_id


def statuses(world: FeatureWorld, execution_id: str) -> dict[str, str]:
    return {t.name: t.status for t in world.runtime.list_threads(execution_id)}


async def wait_for_the_end(world: FeatureWorld, execution_id: str, *, timeout: float = 60.0) -> dict[str, str]:
    """Each thread's final status, once every thread has stopped."""
    def ended() -> bool:
        now = statuses(world, execution_id)
        return bool(now) and all(s in FINAL for s in now.values())

    try:
        await wait_until(ended, timeout=timeout)
    except TimeoutError:
        raise TimeoutError(f"the run never ended: {statuses(world, execution_id)}") from None
    return statuses(world, execution_id)


async def run_to_end(
    world: FeatureWorld,
    *,
    variables: Mapping[str, OptionValue] | None = None,
    groups: Sequence[LabwareGroup] = (),
    timeout: float = 60.0,
) -> tuple[str, dict[str, str]]:
    execution_id = await submit(world, variables=variables, groups=groups)
    return execution_id, await wait_for_the_end(world, execution_id, timeout=timeout)


async def wait_for_a_pause(
    world: FeatureWorld, execution_id: str, *, template: str | None = None, timeout: float = 60.0,
) -> ThreadSnapshot:
    """The first thread to pause; `template` keeps only threads carrying that labware template."""
    def paused() -> list[ThreadSnapshot]:
        return [
            t for t in world.runtime.list_threads(execution_id)
            if template in (None, t.labware_template_name) and t.status == "PAUSED"
        ]

    try:
        await wait_until(lambda: bool(paused()), timeout=timeout)
    except TimeoutError:
        raise TimeoutError(f"nothing paused: {statuses(world, execution_id)}") from None
    return paused()[0]


async def run_to_pause(
    world: FeatureWorld, *, variables: Mapping[str, OptionValue] | None = None, template: str | None = None,
) -> tuple[str, ThreadSnapshot]:
    execution_id = await submit(world, variables=variables)
    return execution_id, await wait_for_a_pause(world, execution_id, template=template)


async def recover(world: FeatureWorld, execution_id: str, thread_id: str, decision: RecoveryDecision) -> None:
    """A recovery decision, sent the way an operator surface sends it."""
    await world.runtime.threads.recover(execution_id, thread_id, decision, confirm=True)


@dataclass(frozen=True)
class PlateRecord:
    labware_id: str
    name: str
    template_name: str
    barcode: str | None
    location: str | None
    volumes: dict[str, float]
    history: list[str]


async def plate_record(world: FeatureWorld, labware_id: str) -> PlateRecord:
    """One plate instance as the record stores it: where it is, each well's volume, every place it has been."""
    snapshot = await world.runtime.labware.get_by_id(labware_id)
    volumes = await world.runtime.labware.get_well_volumes(labware_id)
    history = await world.runtime.labware.get_history(labware_id)
    return PlateRecord(
        labware_id=snapshot.id, name=snapshot.name, template_name=snapshot.template_name, barcode=snapshot.barcode,
        location=snapshot.current_location, volumes=dict(volumes.volumes),
        history=[event.position_id for event in history],
    )


def labware_of(world: FeatureWorld, execution_id: str, template: str) -> list[str]:
    """The ids of the run's labware of one template, in the order their threads spawned."""
    return [
        t.labware_id for t in world.runtime.list_threads(execution_id)
        if t.labware_id is not None and t.labware_template_name == template
    ]


def commands(world: FeatureWorld, device: str, method: str | None = None) -> list[RecordedCommand]:
    """Every command the device's driver received, in order; `method` keeps only that one."""
    return [c for c in world.lab.log(device).commands if method is None or c.method == method]


def positions(world: FeatureWorld, transporter: str, method: str) -> list[str]:
    """The position each pick or place of `transporter` named, in order."""
    return [str(c.args["position_id"]) for c in commands(world, transporter, method)]


def speeds(world: FeatureWorld, device: str) -> list[float]:
    """The speed of each shake the device received, in order."""
    found: list[float] = []
    for c in commands(world, device, "shake"):
        speed = c.args["speed"]
        assert isinstance(speed, float)
        found.append(speed)
    return found


def wells_named(command: RecordedCommand) -> list[tuple[str, str]]:
    """(labware name, well) for each channel of an aspirate or dispense."""
    named: list[tuple[str, str]] = []
    for key in ("aspirations", "dispenses"):
        targets = command.args.get(key)
        if not isinstance(targets, list):
            continue
        for target in targets:
            assert isinstance(target, dict)
            labware, positions = target["labware"], target["positions"]
            assert isinstance(labware, str) and isinstance(positions, list)
            named.extend((labware, str(position)) for position in positions)
    return named


def events(world: FeatureWorld, prefix: str = "") -> list[RuntimeEvent]:
    return [e for e in world.collector.events if e.event_name.startswith(prefix)]


async def operations(world: FeatureWorld, execution_id: str) -> list[TrackingRecord]:
    """The saved operation records of one run."""
    return await world.ops.list(execution_id)

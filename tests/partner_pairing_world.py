"""The world the partner-pairing tests run in: sample plates that each name the index plate they pair with."""
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field

import orca.orca as orca
from orca.config import OrcaConfig
from orca.resource_models.capacity import CapacityPolicy
from orca.resource_models.labware import AnyLabwareTemplate, CanContinueFn, LabwareInstance, LabwareTemplate, PlateTemplate
from orca.resource_models.labware_placement import Arrival
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.resource_pool import ResourcePool
from orca.resource_models.sharing import GroupSharing
from orca.runtime.labware_service import LabwareService
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.status_models import ThreadSnapshot
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, ThreadTemplate, WorkflowTemplate
from orca.spawn import LEAVE_IN_PLACE, REUSE_EXISTING
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system_interface import ISystem
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.labware_group import BarcodeAcquisition, LabwareGroup, LabwareGroupMember
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import ActionTemplate, IMethodTemplate
from orca.workflow_models.submission import BatchMode
from orca.workflow_models.thread_context import ThreadContext

from tests.labware_catalog import bundled_labware_catalog
from tests.memory_activity import memory_ops_store, memory_runtime, memory_variable_store
from tests.memory_activity import memory_unsaved_positions
from tests.test_helpers import create_test_device, create_test_transporter, wait_until, wire_system_map

TERMINAL_STATUSES = {"COMPLETED", "ABORTED", "STOPPED", "FAILED"}
_LABWARE_TYPE = "Cor_Falcon_96_wellplate_340ul_Fb_Black"


@dataclass
class Pairings:
    """What the tag step saw: one (sample barcode, index barcode) per run of the step."""
    seen: list[tuple[str | None, str | None]] = field(default_factory=list)
    adapters: list[str | None] = field(default_factory=list)
    loaded: list[str | None] = field(default_factory=list)


@dataclass
class PairingWorld:
    system: ISystem
    workflow: WorkflowTemplate
    event_bus: EventBus
    pairings: Pairings
    store: InMemoryLabwareStore
    runtime: SystemRuntime

    async def register(self, template_name: str, barcode: str) -> LabwareInstance:
        """A plate the system holds and the store knows, standing nowhere yet."""
        plate = await self.system.get_labware_template(template_name).create_instance()
        plate.barcode = barcode
        self.system.add_labware(plate)
        await self.store.register(plate)
        return plate

    async def stand(self, plate: LabwareInstance, location_name: str) -> None:
        """Put a registered plate on the deck at ``location_name``."""
        location = self.system.get_location(location_name)
        self.system.labware_placer.claim(plate, location)
        await self.system.labware_placer.arrived(plate, location, Arrival.BOUND)

    def where(self, plate: LabwareInstance) -> list[str]:
        """Every location the plate has stood at, in order."""
        return self.system.labware_location_service.get_history(plate).get_history_names()

    async def submit(self, groups: list[LabwareGroup], batch_mode: BatchMode = BatchMode.STANDALONE) -> str:
        submission = await self.runtime.submit(
            self.workflow, groups=groups, batch_mode=batch_mode, mode=WorkflowRunMode.CHECK_RUN,
        )
        return submission.execution_id

    def threads(self, execution_id: str) -> dict[str, ThreadSnapshot]:
        return {thread.name: thread for thread in self.runtime.list_threads(execution_id)}

    async def wait_for_a_pause(self, execution_id: str, thread_prefix: str, timeout: float = 120.0) -> ThreadSnapshot:
        """The first thread whose name starts with ``thread_prefix`` to pause."""
        def paused() -> list[ThreadSnapshot]:
            return [
                thread for name, thread in self.threads(execution_id).items()
                if name.startswith(thread_prefix) and thread.status == "PAUSED"
            ]

        await wait_until(lambda: bool(paused()), timeout=timeout, message=f"no {thread_prefix} thread paused")
        return paused()[0]

    async def wait_for_the_end(self, execution_id: str, timeout: float = 120.0) -> dict[str, str]:
        """Each thread's last status, once every thread has stopped."""
        def statuses() -> dict[str, str]:
            return {name: thread.status for name, thread in self.threads(execution_id).items()}

        await wait_until(
            lambda: bool(statuses()) and all(s in TERMINAL_STATUSES for s in statuses().values()),
            timeout=timeout, message="the run never ended",
        )
        return statuses()

    async def run_to_the_end(self, groups: list[LabwareGroup]) -> dict[str, str]:
        return await self.wait_for_the_end(await self.submit(groups))


def sample_group(group_id: str, sample_barcode: str, index_barcode: str | None = None) -> LabwareGroup:
    """One sample plate by barcode; ``index_barcode`` makes the group name its index plate in the submission."""
    members = [LabwareGroupMember(thread_template_name="sample", acquisition=BarcodeAcquisition(barcode=sample_barcode))]
    if index_barcode is not None:
        members.append(LabwareGroupMember(thread_template_name="index", acquisition=BarcodeAcquisition(barcode=index_barcode)))
    return LabwareGroup(id=group_id, members=tuple(members))


async def build_pairing_world(
    index_for_sample: Mapping[str, str],
    index_thread_first: Callable[[ThreadContext], None] | None = None,
    tags_per_sample: int = 1,
    index_serves_one_tag_then_goes_home: bool = False,
    adapter_for_sample: Mapping[str, str] | None = None,
    index_capacity: CapacityPolicy | None = None,
    index_can_continue: CanContinueFn | None = None,
    index_is_a_resident: bool = False,
    index_is_shared_by_every_group: bool = False,
    index_thread_last: Callable[[ThreadContext], None] | None = None,
    between_tags: Callable[[], Awaitable[object]] | None = None,
    station_sites: int = 3,
    adapter_owns_the_index_step: bool = False,
    adapter_is_shared_by_every_group: bool = False,
    index_stays_at_waste: bool = False,
) -> PairingWorld:
    """Each sample thread names its index plate: ``index_for_sample[sample barcode]`` is the index barcode.

    ``index_thread_first`` runs at the start of the index thread, before it joins the tag step.
    ``adapter_for_sample`` adds a second partner, an adapter, to the tag step; a sample not in it names none.
    ``adapter_owns_the_index_step`` moves the index plate out of the tag step and into a step the adapter
    thread owns, so the thread that needs the index plate is one the engine spawned.
    ``index_thread_last`` runs after the index thread's one join, when it serves one tag and goes home.
    """
    pairings = Pairings()
    tag_station = create_test_device("tag_station", site_names=[f"site-{n + 1}" for n in range(station_sites)])
    transporter = create_test_transporter("robot1", ["sample_pad", "index_pad", "adapter_pad", "hotel", "tag_station", "waste"])
    sample = PlateTemplate("sample", labware_type=_LABWARE_TYPE)
    index = PlateTemplate(
        "index", labware_type=_LABWARE_TYPE, can_continue_fn=index_can_continue,
        group_sharing=GroupSharing.SHARED_ACROSS_GROUPS if index_is_shared_by_every_group else GroupSharing.PER_GROUP,
    )
    adapter = PlateTemplate(
        "adapter", labware_type=_LABWARE_TYPE,
        group_sharing=GroupSharing.SHARED_ACROSS_GROUPS if adapter_is_shared_by_every_group else GroupSharing.PER_GROUP,
    )
    with_adapter = adapter_for_sample is not None or adapter_owns_the_index_step
    inputs: list[LabwareTemplate | AnyLabwareTemplate] = [sample] if adapter_owns_the_index_step else [sample, index]
    if with_adapter:
        inputs.append(adapter)

    registry = ResourceRegistry()
    registry.add_resource(tag_station)
    registry.add_resource(transporter)
    tag_station_pool = ResourcePool("tag_station", [tag_station])
    registry.add_resource_pool(tag_station_pool)
    system_map = SystemMap(registry)
    await wire_system_map(
        system_map, devices={"tag_station": tag_station}, pads=["sample_pad", "index_pad", "adapter_pad", "hotel", "waste"],
    )

    @orca.action(device=tag_station_pool, inputs=inputs)
    async def tag(ctx: ActionContext) -> None:
        index_barcode = None if adapter_owns_the_index_step else ctx.labware("index").barcode
        pairings.seen.append((ctx.labware("sample").barcode, index_barcode))
        if with_adapter:
            pairings.adapters.append(ctx.labware("adapter").barcode)
        await ctx.device().shake(duration=1, speed=500)

    async def _tag_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield tag
    tag_method = MethodTemplate("tag_method", func=_tag_method)

    @orca.action(device=tag_station_pool, inputs=[adapter, index])
    async def load_index(ctx: ActionContext) -> None:
        pairings.loaded.append(ctx.labware("index").barcode)
        await ctx.device().shake(duration=1, speed=500)

    async def _load_index_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield load_index
    load_index_method = MethodTemplate("load_index_method", func=_load_index_method)

    async def _sample_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        own_barcode = ctx.labware.barcode
        if own_barcode is not None and own_barcode in index_for_sample:
            ctx.set_partner_constraint("index", {"barcode": index_for_sample[own_barcode]})
        if adapter_for_sample is not None and own_barcode is not None and own_barcode in adapter_for_sample:
            ctx.set_partner_constraint("adapter", {"barcode": adapter_for_sample[own_barcode]})
        for tag_number in range(tags_per_sample):
            if tag_number and between_tags is not None:
                await between_tags()
            yield tag_method
    sample_thread = ThreadTemplate(
        labware_template=sample,
        start=system_map.get_location("sample_pad"),
        end=system_map.get_location("waste"),
        func=_sample_thread,
        contributes_to=["adapter"] if adapter_owns_the_index_step
        else ["index", "adapter"] if with_adapter else ["index"],
    )

    async def _index_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        if index_thread_first is not None:
            index_thread_first(ctx)
        if index_serves_one_tag_then_goes_home:
            yield orca.join(allows=[tag_method, load_index_method])
            if index_thread_last is not None:
                index_thread_last(ctx)
            return
        while ctx.has_more_work():
            yield orca.join(allows=[tag_method, load_index_method])
    index_thread = ThreadTemplate(
        labware_template=index,
        start=(system_map.get_location("index_pad"), REUSE_EXISTING) if index_is_a_resident
        else system_map.get_location("index_pad"),
        end=(system_map.get_location("index_pad"), LEAVE_IN_PLACE) if index_is_a_resident or index_serves_one_tag_then_goes_home
        # A plate left on the system at its end can be named by a later group; SIM takes a removed one away.
        else (system_map.get_location("waste"), LEAVE_IN_PLACE) if index_stays_at_waste
        else system_map.get_location("waste"),
        func=_index_thread,
    )

    workflow = WorkflowTemplate("pair_index_plates")
    workflow.add_thread(sample_thread, is_start=True)
    workflow.add_thread(index_thread)
    workflow.register_auto_spawn(index_thread, capacity=index_capacity)
    if with_adapter:
        async def _adapter_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            if adapter_is_shared_by_every_group:
                while ctx.has_more_work():
                    yield orca.join(allows=[tag_method])
                return
            yield orca.join(allows=[tag_method])
            if adapter_owns_the_index_step:
                yield load_index_method
        adapter_thread = ThreadTemplate(
            labware_template=adapter,
            start=system_map.get_location("adapter_pad"),
            end=system_map.get_location("waste"),
            func=_adapter_thread,
            contributes_to=["index"] if adapter_owns_the_index_step else [],
        )
        workflow.add_thread(adapter_thread)
        workflow.register_auto_spawn(adapter_thread)

    event_bus = EventBus()
    store = InMemoryLabwareStore()
    builder = SdkToSystemBuilder(
        name="pairing_system",
        description="",
        labwares=[sample, index, adapter] if with_adapter else [sample, index],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=event_bus, config=OrcaConfig(), ops_history_store=memory_ops_store(),
        variable_store=memory_variable_store(), labware_catalog=bundled_labware_catalog(),
        labware_service=LabwareService(store),
        unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    system = builder.get_system()
    runtime = await memory_runtime(system, event_bus=event_bus)
    return PairingWorld(system, workflow, event_bus, pairings, store, runtime)

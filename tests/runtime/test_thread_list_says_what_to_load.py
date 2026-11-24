"""The thread list tells an operator what to put down before a run, and what it starts holding."""

from collections.abc import AsyncGenerator

import orca.orca as orca

from orca.modes.run_modes import OUT_OF_RUN_BASE
from orca.devices.shaker import Shaker
from orca.events.event_bus import EventBus
from orca.runtime.startup import build_device_gateway
from orca.operations.catalog import _thread_to_dto
from orca.runtime.facades.registry import RegistryFacade
from orca.sdk.package import build_system
from orca.sdk.labware import LabwareInitialState, PlateTemplate, TipRackTemplate
from orca.sdk.workflow import MethodTemplate
from orca.spawn import REUSE_EXISTING
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.memory_activity import memory_ops_store
from tests.no_devices import NullDeviceConnectionSource
from tests.setup_package import package_stores
from tests.unit.test_add_workflow_template_thread_order import _topology
from orca.runtime.server_events import ServerEvents

PLATE = "Cor_Falcon_96_wellplate_340ul_Fb_Black"


async def test_the_entry_plate_and_the_reused_plate_are_put_down_before_the_run_with_their_starting_volumes() -> None:
    stores = package_stores()
    topology = _topology(stores)
    sample = PlateTemplate("sample", labware_type=PLATE, initial_state=LabwareInitialState(uniform_volume=150.0))
    buffer = PlateTemplate("buffer", labware_type=PLATE)
    later = PlateTemplate("later", labware_type=PLATE)
    shaker = topology.device("shaker_1", Shaker)

    @orca.action(device=shaker, inputs=[sample, buffer, later])
    async def shake(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=100)

    @orca.method
    async def shake_step(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake

    @orca.thread(labware=sample, start="pad_1", end="pad_1")
    async def sample_journey(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        yield shake_step

    @orca.thread(labware=buffer, start=("shaker_1", REUSE_EXISTING), end="shaker_1")
    async def buffer_journey(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        yield orca.join(allows=[shake_step])

    @orca.thread(labware=later, start="pad_1", end="pad_1")
    async def later_journey(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        yield orca.join(allows=[shake_step])

    @orca.workflow(name="load_me")
    def load_me(wf: WorkflowContext) -> None:
        wf.start(sample_journey)
        wf.thread(buffer_journey)
        wf.thread(later_journey)

    built = await build_system(
        "loads", topology, stores, workflow=load_me, event_bus=EventBus(), ops_history_store=memory_ops_store(),
    )
    facade = RegistryFacade(
        system=built.system,
        list_reservations_fn=lambda eid: [],
        cancel_reservation_fn=lambda eid, rid: None,
        connections=NullDeviceConnectionSource(),
        faults=build_device_gateway(ServerEvents(), None).controller,
        base_mode=lambda: OUT_OF_RUN_BASE,
    )

    rows = {t.labware_template_name: t for t in facade.list_thread_templates()}

    assert {n: r.placed_before_run for n, r in rows.items()} == {"sample": True, "buffer": True, "later": False}
    assert rows["sample"].initial_state == LabwareInitialState(uniform_volume=150.0)
    assert rows["buffer"].initial_state is None
    assert rows["sample"].labware_type == PLATE


async def test_the_thread_list_carries_what_to_load_over_the_wire() -> None:
    stores = package_stores()
    topology = _topology(stores)
    sample = PlateTemplate("sample", labware_type=PLATE, initial_state=LabwareInitialState(uniform_volume=150.0))
    shaker = topology.device("shaker_1", Shaker)

    @orca.action(device=shaker, inputs=[sample])
    async def shake(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=100)

    @orca.method
    async def shake_step(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake

    @orca.thread(labware=sample, start="pad_1", end="pad_1")
    async def sample_journey(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        yield shake_step

    @orca.workflow(name="load_me")
    def load_me(wf: WorkflowContext) -> None:
        wf.start(sample_journey)

    built = await build_system(
        "loads", topology, stores, workflow=load_me, event_bus=EventBus(), ops_history_store=memory_ops_store(),
    )
    facade = RegistryFacade(
        system=built.system,
        list_reservations_fn=lambda eid: [],
        cancel_reservation_fn=lambda eid, rid: None,
        connections=NullDeviceConnectionSource(),
        faults=build_device_gateway(ServerEvents(), None).controller,
        base_mode=lambda: OUT_OF_RUN_BASE,
    )

    [row] = [_thread_to_dto(t) for t in facade.list_thread_templates()]

    assert (row.labware_type, row.placed_before_run, row.initial_state) == (
        PLATE, True, LabwareInitialState(uniform_volume=150.0),
    )


def test_a_tip_rack_reports_the_tips_it_starts_with_when_none_are_declared() -> None:
    full = TipRackTemplate("full", labware_type="opentrons_96_filtertiprack_200ul", with_tips=True)
    empty = TipRackTemplate("empty", labware_type="opentrons_96_filtertiprack_200ul", with_tips=False)

    assert full.initial_state == LabwareInitialState(max_fill=True)
    assert empty.initial_state == LabwareInitialState(tip_positions=[])

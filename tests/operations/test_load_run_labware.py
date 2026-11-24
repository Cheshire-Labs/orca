"""Loading what a run needs: one call records each thread's labware on its start position, with its volume."""

from collections.abc import AsyncGenerator, AsyncIterator
from unittest.mock import patch

import orca.orca as orca
import pytest
import pytest_asyncio
from cheshire_drivers import AccessConfig, CartesianCoordinates, Teachpoint
from cheshire_drivers.liquid_handler_models import TROUGH_WELL_ID
from httpx import ASGITransport, AsyncClient

from orca.daemon.app import create_app
from orca.devices.shaker import Shaker
from orca.events.event_bus import EventBus
from orca.operations.blocker_models import BlockersPayload
from orca.operations.labware import RegisterLabwareOperation
from orca.operations.labware_models import RegisterLabwareRequest, RegisterLabwareResponse
from orca.operations.run_loading_models import LoadRunLabwareRequest, LoadRunLabwareResponse, RunLabwareItem
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.sdk.build import Topology, build_system_over
from orca.sdk.package import build_system
from orca.sdk.labware import LabwareInitialState, PlateTemplate, TipRackTemplate, TroughTemplate
from orca.sdk.workflow import MethodTemplate
from orca.spawn import REUSE_EXISTING
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from orca.runtime.server_events import ServerEvents
from orca.runtime.sinks import CollectorSink
from tests.memory_activity import memory_daemon_system, memory_ops_store
from tests.memory_activity import memory_unsaved_positions
from tests.setup_package import package_stores, with_teachpoints, write_access_configs, write_teachpoints
from tests.memory_activity import announced_labware_service

PLATE = "Cor_Falcon_96_wellplate_340ul_Fb_Black"
TIPS = "opentrons_96_filtertiprack_200ul"
WORKFLOW = "load_me"
SAMPLE = RunLabwareItem(thread_template_name="sample")
TIP_RACK = RunLabwareItem(thread_template_name="tips")
LATER = RunLabwareItem(thread_template_name="later")


def _topology() -> Topology:
    stores = package_stores()
    access = AccessConfig(name="vertical", access_type="vertical")
    write_access_configs(stores, [access])
    points = [
        Teachpoint(name, CartesianCoordinates(x, 0, 0, 0, 90, 180), orientation="right", access=access)
        for x, name in ((0, "shaker_1"), (100, "pad_1"), (200, "pad_2"))
    ]
    arm = with_teachpoints(Transporter("arm"), write_teachpoints(stores, "arm", points))
    return Topology(
        locations={"shaker_1": Shaker("shaker_1"), "pad_1": PlatePad("pad_1"), "pad_2": PlatePad("pad_2")},
        transporters=[arm],
    )


@pytest.fixture
def labware_seen() -> CollectorSink:
    """The LABWARE events the loaded runtime sends."""
    return CollectorSink()


@pytest_asyncio.fixture
async def client(labware_seen: CollectorSink) -> AsyncIterator[AsyncClient]:
    topology = _topology()
    sample = PlateTemplate("sample", labware_type=PLATE, initial_state=LabwareInitialState(uniform_volume=150.0))
    tips = TipRackTemplate("tips", labware_type=TIPS, with_tips=True)
    later = PlateTemplate("later", labware_type=PLATE)
    shaker = topology.device("shaker_1", Shaker)

    @orca.action(device=shaker, inputs=[sample, tips, later])
    async def shake(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=100)

    @orca.method
    async def shake_step(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake

    @orca.thread(labware=sample, start="pad_1", end="pad_1")
    async def sample_journey(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        yield shake_step

    @orca.thread(labware=tips, start=("pad_2", REUSE_EXISTING), end="pad_2")
    async def tips_journey(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        yield shake_step

    @orca.thread(labware=later, start="pad_1", end="pad_1")
    async def later_journey(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        yield shake_step

    @orca.workflow(name=WORKFLOW)
    def load_me(wf: WorkflowContext) -> None:
        wf.start(sample_journey)
        wf.thread(tips_journey)
        wf.thread(later_journey)

    labware_events = ServerEvents()
    labware_events.add_sink(labware_seen)
    built = await build_system_over(
        "loads", topology, package_stores(), workflow=load_me, event_bus=EventBus(), ops_history_store=memory_ops_store(),
        labware_service=announced_labware_service(labware_events),
        unsaved_positions=memory_unsaved_positions(),
    )
    injected = await memory_daemon_system(built.system)
    await injected.runtime.start()
    try:
        app = create_app(injected=injected)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://daemon.test") as c:
            yield c
    finally:
        await injected.runtime.shutdown(confirm=True)


async def _load(client: AsyncClient, items: list[RunLabwareItem], clear_all_first: bool = False) -> LoadRunLabwareResponse:
    request = LoadRunLabwareRequest(workflow_name=WORKFLOW, items=items, clear_all_first=clear_all_first)
    resp = await client.post(
        f"/api/workflows/{WORKFLOW}/load-run-labware", json=request.model_dump(mode="json", exclude={"workflow_name"}),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return LoadRunLabwareResponse.model_validate({k: v for k, v in body.items() if k not in BlockersPayload.model_fields})


async def _where(client: AsyncClient) -> dict[str, str | None]:
    listed = (await client.get("/api/labware-instances")).json()["labware"]
    return {lw["id"]: lw["current_location"] for lw in listed}


async def _volumes(client: AsyncClient, labware_id: str) -> set[float]:
    resp = await client.get(f"/api/labware-instances/{labware_id}/contents")
    return set(resp.json()["volumes"].values())


def _loaded(result: LoadRunLabwareResponse) -> dict[str, str]:
    return {item.thread_template_name: item.labware_id for item in result.loaded}


def _failed(result: LoadRunLabwareResponse) -> list[tuple[str, str]]:
    return [(item.thread_template_name, item.code) for item in result.not_loaded]


async def test_with_no_items_every_thread_put_down_before_the_run_is_loaded(client: AsyncClient) -> None:
    loaded = _loaded(await _load(client, []))

    where = await _where(client)
    assert {name: where[i] for name, i in loaded.items()} == {"sample": "pad_1", "tips": "pad_2"}


async def test_reused_labware_already_in_place_is_kept_as_it_is(client: AsyncClient) -> None:
    tips = _loaded(await _load(client, [TIP_RACK]))["tips"]

    result = await _load(client, [])

    assert [(k.thread_template_name, k.labware_id) for k in result.kept] == [("tips", tips)]
    assert list(_loaded(result)) == ["sample"]


async def test_an_entry_plate_already_in_place_is_recorded_fresh(client: AsyncClient) -> None:
    first = _loaded(await _load(client, [SAMPLE]))["sample"]

    second = _loaded(await _load(client, [SAMPLE]))["sample"]

    where = await _where(client)
    assert first not in where and where[second] == "pad_1"


async def test_a_thread_not_put_down_before_the_run_is_refused(client: AsyncClient) -> None:
    result = await _load(client, [LATER])

    assert _failed(result) == [("later", "validation_error")]
    assert await _where(client) == {}


async def test_a_typed_volume_is_set_in_every_well(client: AsyncClient) -> None:
    loaded = _loaded(await _load(client, [RunLabwareItem(thread_template_name="sample", volume_ul=40.0)]))

    assert await _volumes(client, loaded["sample"]) == {40.0}


async def test_the_workflows_own_volume_is_left_as_declared(client: AsyncClient) -> None:
    loaded = _loaded(await _load(client, [RunLabwareItem(thread_template_name="sample", volume_ul=150.0)]))

    assert await _volumes(client, loaded["sample"]) == {150.0}


async def test_an_item_that_fails_does_not_stop_the_others(client: AsyncClient) -> None:
    result = await _load(client, [
        RunLabwareItem(thread_template_name="tips", volume_ul=5.0), RunLabwareItem(thread_template_name="nope"), SAMPLE,
    ])

    assert list(_loaded(result)) == ["sample"]
    assert _failed(result) == [("tips", "validation_error"), ("nope", "thread_template_not_found")]


async def test_an_unexpected_error_answers_that_item_and_the_rest_go_on(client: AsyncClient) -> None:
    register = RegisterLabwareOperation.run

    async def broken_for_the_sample(op: RegisterLabwareOperation, req: RegisterLabwareRequest) -> RegisterLabwareResponse:
        if req.template_name == "sample":
            raise RuntimeError("the store went away")
        return await register(op, req)

    with patch.object(RegisterLabwareOperation, "run", broken_for_the_sample):
        result = await _load(client, [])

    assert [(i.thread_template_name, i.code, i.message) for i in result.not_loaded] == [
        ("sample", "internal_error", "the store went away"),
    ]
    assert list(_loaded(result)) == ["tips"]


async def test_clearing_first_takes_every_labware_off_the_record_and_loads_every_thread(client: AsyncClient) -> None:
    earlier = _loaded(await _load(client, [TIP_RACK]))["tips"]

    result = await _load(client, [], clear_all_first=True)

    assert earlier in result.cleared and result.kept == []
    assert sorted(await _where(client)) == sorted(_loaded(result).values())
    assert list(_loaded(result)) == ["sample", "tips"]


async def test_each_write_is_announced_in_the_order_the_load_makes_it(
    client: AsyncClient, labware_seen: CollectorSink,
) -> None:
    await _load(client, [SAMPLE])
    labware_seen.clear()

    await _load(client, [SAMPLE])
    reload = [e.status for e in labware_seen.events]
    labware_seen.clear()
    await _load(client, [TIP_RACK], clear_all_first=True)
    cleared_first = [e.status for e in labware_seen.events]

    assert reload[0] == "CLEARED" and "SAVED" in reload[1:]
    assert cleared_first[0] == "CLEARED" and cleared_first[-1] in {"SAVED", "MOVED"}


TROUGH = "agilent_1_reservoir_290ml"


@pytest_asyncio.fixture
async def trough_client() -> AsyncIterator[AsyncClient]:
    """A run whose one labware is a trough: a single pool, no well grid."""
    topology = _topology()
    reagent = TroughTemplate("reagent", labware_type=TROUGH)
    shaker = topology.device("shaker_1", Shaker)

    @orca.action(device=shaker, inputs=[reagent])
    async def shake(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=100)

    @orca.method
    async def shake_step(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake

    @orca.thread(labware=reagent, start="pad_1", end="pad_1")
    async def reagent_journey(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        yield shake_step

    @orca.workflow(name="load_a_trough")
    def load_a_trough(wf: WorkflowContext) -> None:
        wf.start(reagent_journey)

    built = await build_system(
        "loads_a_trough", topology, package_stores(), workflow=load_a_trough, event_bus=EventBus(),
        ops_history_store=memory_ops_store(),
    )
    injected = await memory_daemon_system(built.system)
    await injected.runtime.start()
    try:
        app = create_app(injected=injected)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://daemon.test") as c:
            yield c
    finally:
        await injected.runtime.shutdown(confirm=True)


async def test_a_trough_is_filled_through_its_one_pool(trough_client: AsyncClient) -> None:
    request = LoadRunLabwareRequest(
        workflow_name="load_a_trough", items=[RunLabwareItem(thread_template_name="reagent", volume_ul=50000.0)],
    )
    resp = await trough_client.post(
        "/api/workflows/load_a_trough/load-run-labware", json=request.model_dump(mode="json", exclude={"workflow_name"}),
    )
    assert resp.status_code == 200, resp.text
    loaded = LoadRunLabwareResponse.model_validate(
        {k: v for k, v in resp.json().items() if k not in BlockersPayload.model_fields},
    )

    [item] = loaded.loaded
    contents = await trough_client.get(f"/api/labware-instances/{item.labware_id}/contents")
    assert contents.json()["volumes"] == {TROUGH_WELL_ID: 50000.0}

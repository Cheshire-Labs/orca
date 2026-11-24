"""An operator can put down labware the deployment package never declared.

The bench report this closes: a NEST 195 mL trough was set on a Flex so a reset
run could pour into it, and no operator surface could tell the system it was
there. `register-labware` needs a template and the deployment package declared
none for a trough; the labware editor only defines what a trough *is*; the deck
layout declares carriers, not labware. The one door that appeared to work, the
deck editor's `add_deck_labware`, wrote the driver's deck and left the ledger
calling the site empty.

Naming a catalog labware type derives an ad-hoc template on the spot. Placement
then goes the way every other placement goes, through the chokepoint that writes
the slot, the ledger and the driver deck together.
"""

from orca.sdk.build import build_system_over
from orca.events.event_bus import EventBus
import pytest

import orca.orca as orca
from cheshire_drivers import CartesianCoordinates as C
from cheshire_drivers import DeckLayoutConfig, DeckResourceConfig, Teachpoint
from cheshire_drivers import RecordingLiquidHandlerDriver
from cheshire_drivers.liquid_handler_models import GetDeckStateRequest

from orca.devices.devices import LiquidHandler, Storage
from orca.resource_models.adhoc_labware import (
    LabwareHasNoModel,
    UnplaceableLabwareCategory,
    adhoc_template_name,
)
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.resource_models.labware_catalog_protocol import LabwareNotFound
from orca.resource_models.labware import LabwareInstance, instance_name_for
from orca.runtime.labware_service import LabwareService
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.sdk.build import Topology
from orca.sdk.labware import PlateTemplate
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from tests.setup_package import package_stores, write_deck_layouts, write_teachpoints
from tests.setup_package import with_teachpoints, with_deck_layouts
from tests.memory_activity import memory_ops_store, memory_runtime
from tests.memory_activity import memory_unsaved_positions
from tests.real_device_path import real_device_path, the_real_device_path
from tests.recording_handler import RecordingHandler
from orca.resource_models.labware_service_interface import ILabwareService
from tests.memory_activity import memory_labware_service

# The system build must reach every position these runs touch.
pytestmark = pytest.mark.positions_bound_by_the_build

# The labware from the bench report. Nothing in this deployment declares a
# trough template, which is the whole point.
TROUGH_TYPE = "nest_1_reservoir_195ml"
TROUGH_SITE = "lh/carrier-25-0"
PLATE_SITE = "lh/carrier-7-0"
# In the catalog, but cheshire-drivers exposes no factory for it. 135 of the 216
# placeable seeded rows are like this, so it is the common answer, not an edge.
UNBUILDABLE_TYPE = "Cor_Falcon_tube_50mL_Vb"

DECK_CONFIG = DeckLayoutConfig(
    deck_type="STARlet",
    resources=[
        DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
        DeckResourceConfig(name="carrier-25", catalog_ref="Trough_CAR_4R200_A00", rail=25),
    ],
)


async def _build(recorder: RecordingLiquidHandlerDriver, *, wf_name: str, labware_service: ILabwareService | None = None):
    """A one-liquid-handler deployment declaring a plate template and nothing else."""
    stores = package_stores()
    sample_plate = PlateTemplate(
        "sample_plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black",
    )

    the_real_device_path().simulate_with("lh", recorder)
    lh = with_deck_layouts(LiquidHandler(
        "lh",
        deck_layout="default",
    ), write_deck_layouts(stores, "lh", {"default": DECK_CONFIG}))
    stacker = Storage("stacker")
    pad = PlatePad("pad")
    arm = with_teachpoints(Transporter(
        "arm",
    ), write_teachpoints(stores, "arm", [
            Teachpoint("stacker", C(0, 200, 300, 0, 90, 180), orientation="right"),
            Teachpoint("pad", C(200, 200, 300, 0, 90, 180), orientation="right"),
            Teachpoint("lh/carrier-7-2", C(400, 200, 300, 0, 90, 180), orientation="right"),
        ]))

    @orca.action(device=lh, inputs=[sample_plate])
    async def touch_plate(ctx: ActionContext) -> None:
        ctx.labware("sample_plate")

    @orca.method
    async def touch_method(ctx: MethodContext):
        yield touch_plate

    @orca.thread(labware=sample_plate, start="stacker", end="pad")
    async def plate_journey(ctx: ThreadContext):
        yield touch_method

    @orca.workflow(name=wf_name)
    def workflow(wf):
        wf.start(plate_journey)

    topology = Topology(
        locations={"stacker": stacker, "pad": pad, "lh": lh},
        transporters=[arm],
    )
    build = await build_system_over(
        name="Ad-hoc Labware", workflow=workflow, topology=topology, stores=stores, event_bus=EventBus(), ops_history_store=memory_ops_store(),
        labware_service=labware_service or memory_labware_service(),
        unsaved_positions=memory_unsaved_positions(),
    )
    return build


class RehydratingLabwareStore(InMemoryLabwareStore):
    """An in-memory store that hands back rows, the way a real one does.

    `InMemoryLabwareStore` returns the very object it was given, so a labware
    comes back across a rebuild still holding the PLR object the previous
    runtime built. A DB-backed store keeps fields, not objects, so the labware
    comes back bare and has to be rebuilt from its template. That is the path
    an ad-hoc template has to survive, and the plain fake cannot reach it.

    Mirrors what a hosted Db-backed labware store does on rehydrate.
    """

    def _rehydrated(self, instance: LabwareInstance | None) -> LabwareInstance | None:
        if instance is None:
            return None
        return LabwareInstance(
            template_name=instance.template_name,
            labware_type=instance.labware_type,
            barcode=instance.barcode,
            instance_id=instance.id,
            name=instance_name_for(instance.template_name, instance.id),
        )

    async def get_by_id(self, labware_id: str) -> LabwareInstance | None:
        return self._rehydrated(await super().get_by_id(labware_id))

    async def get_by_barcode(self, barcode: str) -> LabwareInstance | None:
        return self._rehydrated(await super().get_by_barcode(barcode))


async def _runtime(recorder: RecordingLiquidHandlerDriver, *, wf_name: str, store):
    build = await _build(recorder, wf_name=wf_name, labware_service=LabwareService(store))
    runtime = await memory_runtime(
        build.system, event_bus=build.event_bus,
    )
    await runtime.start()
    return runtime, build


def _recorder() -> RecordingLiquidHandlerDriver:
    return RecordingHandler()


async def _deck_names(recorder: RecordingLiquidHandlerDriver) -> set[str]:
    state = await recorder.get_deck_state(GetDeckStateRequest())
    return {resource.name for resource in state.labware}


@pytest.mark.asyncio
async def test_a_catalog_type_nothing_declared_can_still_be_placed() -> None:
    """The bench case: a trough goes down, and both sides of the system know."""
    recorder = _recorder()
    runtime, build = await _runtime(
        recorder, wf_name="adhoc_place", store=InMemoryLabwareStore(),
    )

    snap = await runtime.labware.register(
        labware_type=TROUGH_TYPE, location=TROUGH_SITE, confirm=True,
    )

    assert snap.template_name == adhoc_template_name(TROUGH_TYPE)
    resident = build.system.system_map.get_location(TROUGH_SITE).labware
    assert resident is not None, "register did not write the deck-site slot"
    assert resident.name in await _deck_names(recorder), (
        "the ledger has the trough but the driver deck does not; the two sides "
        "of the system disagree, which is the defect this closes"
    )
    assert any(row.id == snap.id for row in await runtime.labware.list_all()), (
        "the trough is not readable through list-labware"
    )


@pytest.mark.asyncio
async def test_the_derived_template_comes_back_after_a_rebuild() -> None:
    """The assertion that rules out injecting a template into the live system only.

    A template that lives only in the running System is gone on the next
    rebuild. Its labware then restores identity-only, the deck rebuild has no
    catalog_ref to use and skips it, and the trough the ledger still remembers
    stops existing for the driver. Derived from the labware's own persisted
    labware_type, it comes back.
    """
    store = RehydratingLabwareStore()
    runtime, _ = await _runtime(_recorder(), wf_name="adhoc_rebuild_a", store=store)
    snap = await runtime.labware.register(
        labware_type=TROUGH_TYPE, location=TROUGH_SITE, confirm=True,
    )
    await runtime.shutdown(confirm=True)

    # A rebuild: a brand-new System, and an orca-client whose driver deck starts
    # empty, over the store the first runtime wrote.
    async with real_device_path():
        rebuilt_recorder = _recorder()
        assert await _deck_names(rebuilt_recorder) == set()
        _rebuilt, rebuilt_build = await _runtime(
            rebuilt_recorder, wf_name="adhoc_rebuild_b", store=store,
        )

        resident = rebuilt_build.system.system_map.get_location(TROUGH_SITE).labware
        assert resident is not None, "the trough did not survive the rebuild"
        assert resident.id == snap.id, "the rebuild minted a different trough"
        assert resident.has_plr_backing, (
            "the trough came back identity-only, so its declared layout is "
            "unreadable and the deck cannot model it"
        )
        # A restart does not reach the device; the deck is rebuilt from the ledger before the next command.
        await _rebuilt.system.device_worlds.bring_up_to_date(TROUGH_SITE.split("/")[0])
        assert resident.name in await _deck_names(rebuilt_recorder), (
            "the trough is back in the ledger but not on the driver deck"
        )


@pytest.mark.asyncio
async def test_a_declared_template_still_registers_by_name() -> None:
    """The negative control: adding the second way in did not disturb the first."""
    recorder = _recorder()
    runtime, build = await _runtime(
        recorder, wf_name="adhoc_by_name", store=InMemoryLabwareStore(),
    )

    snap = await runtime.labware.register(
        "sample_plate", location=PLATE_SITE, confirm=True,
    )

    assert snap.template_name == "sample_plate"
    resident = build.system.system_map.get_location(PLATE_SITE).labware
    assert resident is not None
    assert resident.name in await _deck_names(recorder)


@pytest.mark.asyncio
async def test_the_snapshot_names_the_catalog_type_of_a_declared_template_instance() -> None:
    runtime, _ = await _runtime(
        _recorder(), wf_name="declared_type", store=InMemoryLabwareStore(),
    )

    snap = await runtime.labware.register(
        "sample_plate", location=PLATE_SITE, confirm=True,
    )

    assert snap.labware_type == "Cor_Falcon_96_wellplate_340ul_Fb_Black"
    [row] = [r for r in await runtime.labware.list_all() if r.id == snap.id]
    assert row.labware_type == "Cor_Falcon_96_wellplate_340ul_Fb_Black"


@pytest.mark.asyncio
async def test_the_snapshot_names_the_catalog_type_of_an_ad_hoc_instance() -> None:
    runtime, _ = await _runtime(
        _recorder(), wf_name="adhoc_type", store=InMemoryLabwareStore(),
    )

    snap = await runtime.labware.register(
        labware_type=TROUGH_TYPE, location=TROUGH_SITE, confirm=True,
    )

    assert snap.labware_type == TROUGH_TYPE
    assert snap.template_name == adhoc_template_name(TROUGH_TYPE)


@pytest.mark.asyncio
async def test_naming_the_labware_no_way_is_refused() -> None:
    runtime, _ = await _runtime(
        _recorder(), wf_name="adhoc_named_none", store=InMemoryLabwareStore(),
    )

    with pytest.raises(ValueError):
        await runtime.labware.register(confirm=True)


@pytest.mark.asyncio
async def test_naming_the_labware_both_ways_is_refused() -> None:
    """A declared template already fixes its labware type, so the pair could
    contradict each other and there is no right answer to pick."""
    runtime, _ = await _runtime(
        _recorder(), wf_name="adhoc_named_both", store=InMemoryLabwareStore(),
    )

    with pytest.raises(ValueError):
        await runtime.labware.register(
            "sample_plate", labware_type=TROUGH_TYPE, confirm=True,
        )


@pytest.mark.asyncio
async def test_an_unknown_catalog_type_is_refused_before_anything_is_written() -> None:
    recorder = _recorder()
    runtime, build = await _runtime(
        recorder, wf_name="adhoc_unknown", store=InMemoryLabwareStore(),
    )

    with pytest.raises(LabwareNotFound):
        await runtime.labware.register(
            labware_type="no_such_labware", location=TROUGH_SITE, confirm=True,
        )

    assert build.system.system_map.get_location(TROUGH_SITE).labware is None
    assert await _deck_names(recorder) == set()


@pytest.mark.asyncio
async def test_a_carrier_is_deck_furniture_and_cannot_be_registered() -> None:
    """A carrier is declared by the deck layout; there is no instance to mint."""
    runtime, _ = await _runtime(
        _recorder(), wf_name="adhoc_carrier", store=InMemoryLabwareStore(),
    )

    with pytest.raises(UnplaceableLabwareCategory):
        await runtime.labware.register(
            labware_type="PLT_CAR_L5AC_A00", location=TROUGH_SITE, confirm=True,
        )


@pytest.mark.asyncio
async def test_a_catalog_type_nothing_can_build_is_named_not_a_500() -> None:
    """Most seeded rows have no PLR factory, so this is the common answer.

    It used to escape as a bare RuntimeError: a 500 on REST, and on MCP the
    literal string "internal server error" with the reason thrown away.
    """
    recorder = _recorder()
    runtime, build = await _runtime(
        recorder, wf_name="adhoc_unbuildable", store=InMemoryLabwareStore(),
    )

    with pytest.raises(LabwareHasNoModel) as exc:
        await runtime.labware.register(
            labware_type=UNBUILDABLE_TYPE, location=TROUGH_SITE, confirm=True,
        )

    assert UNBUILDABLE_TYPE in str(exc.value)
    assert build.system.system_map.get_location(TROUGH_SITE).labware is None
    assert await _deck_names(recorder) == set()


@pytest.mark.asyncio
async def test_a_refused_type_leaves_no_template_behind() -> None:
    """The refusal has to leave the registry as it found it.

    The derived template used to be registered before the instance was minted,
    so a type nothing can build left a template on the system for good, which
    register's own contract says never happens.
    """
    runtime, build = await _runtime(
        _recorder(), wf_name="adhoc_no_residue", store=InMemoryLabwareStore(),
    )
    before = {t.name for t in build.system.labware_templates}

    with pytest.raises(LabwareHasNoModel):
        await runtime.labware.register(
            labware_type=UNBUILDABLE_TYPE, confirm=True,
        )

    assert {t.name for t in build.system.labware_templates} == before


@pytest.mark.asyncio
async def test_a_declared_name_that_collides_is_refused_not_used() -> None:
    """A declared template may take the name a derivation produces.

    Handing it back would mint the wrong labware under the right name and send
    the wrong geometry to the driver deck, with nothing said.
    """
    runtime, build = await _runtime(
        _recorder(), wf_name="adhoc_collision", store=InMemoryLabwareStore(),
    )
    build.system.add_labware_template(PlateTemplate(
        adhoc_template_name(TROUGH_TYPE),
        labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black",
    ))

    with pytest.raises(ValueError, match="already declared"):
        await runtime.labware.register(labware_type=TROUGH_TYPE, confirm=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"template_name": "", "labware_type": None}, id="blank_template"),
        pytest.param({"template_name": None, "labware_type": "  "}, id="blank_type"),
    ],
)
async def test_a_blank_name_reads_as_unset_not_as_a_name(kwargs) -> None:
    """A client that always sends every field says "unset" with "" as readily
    as with null. A blank used to pass the guard and 404 on a name nobody wrote."""
    runtime, _ = await _runtime(
        _recorder(), wf_name=f"adhoc_blank_{len(kwargs)}", store=InMemoryLabwareStore(),
    )

    with pytest.raises(ValueError):
        await runtime.labware.register(confirm=True, **kwargs)


@pytest.mark.asyncio
async def test_a_declared_template_names_the_labware_alongside_a_blank_type() -> None:
    """The other half: an explicit blank must not refuse a well-formed intent."""
    recorder = _recorder()
    runtime, build = await _runtime(
        recorder, wf_name="adhoc_blank_partner", store=InMemoryLabwareStore(),
    )

    snap = await runtime.labware.register(
        "sample_plate", labware_type="", location=PLATE_SITE, confirm=True,
    )

    assert snap.template_name == "sample_plate"
    assert build.system.system_map.get_location(PLATE_SITE).labware is not None

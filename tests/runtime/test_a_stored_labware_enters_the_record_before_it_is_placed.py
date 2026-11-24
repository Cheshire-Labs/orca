"""An operator who states where a labware is that only the database knows gets it placed with its facts.

A database store builds a new object on every read. After a restart, a labware with no saved position has no
object in the engine, so stating its position hands the store's copy to the placer.
"""

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from cheshire_drivers import CartesianCoordinates as C
from cheshire_drivers import DeckLayoutConfig, DeckResourceConfig, RecordingLiquidHandlerDriver, SimLiquidHandlerDriver, Teachpoint
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orca.devices.devices import LiquidHandler, Storage
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_placement import Arrival
from orca.resource_models.location import Location
from orca.runtime.db import create_memory_engine
from orca.runtime.facades.labware import LabwareFacade
from orca.runtime.db.base import Base
from orca.events.event_bus import EventBus
from orca.runtime.labware_service import LabwareService
from orca.resource_models.transporter import Transporter
from orca.runtime.sql_labware_store import SqlLabwareStore
from orca.runtime.startup import in_memory_unsaved_positions
from orca.sdk.build import SystemBuild, Topology, build_system_over
from orca.sdk.labware import TipRackTemplate
from orca.state.identity import LabwareRef
from orca.state.records import InitialStateDetails
from tests.memory_activity import memory_ops_store, memory_runtime
from tests.real_device_path import the_real_device_path
from tests.recording_handler import RecordingHandler
from tests.setup_package import package_stores, with_deck_layouts, with_teachpoints, write_deck_layouts, write_teachpoints

pytestmark = pytest.mark.asyncio

DECK_CONFIG = DeckLayoutConfig(
    deck_type="STARlet",
    resources=[DeckResourceConfig(name="carrier-15", catalog_ref="TIP_CAR_480_A00", rail=15)],
)
RACK_SITE = "lh/carrier-15-0"


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_memory_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _deck_over_a_database(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[SystemBuild, LabwareService, TipRackTemplate]:
    """A liquid handler's deck whose labware record is an SQLite store, which builds a new object on every read."""
    stores = package_stores()
    tips = TipRackTemplate("tips", labware_type="hamilton_96_tiprack_10uL_filter", with_tips=True)
    # A double that advertises what LiquidHandler declares, as a correct install does.
    the_real_device_path().simulate_with("lh", RecordingHandler())
    lh = with_deck_layouts(LiquidHandler("lh", deck_layout="default"), write_deck_layouts(stores, "lh", {"default": DECK_CONFIG}))
    arm = with_teachpoints(Transporter("arm"), write_teachpoints(stores, "arm", [
        Teachpoint("pad", C(0, 200, 300, 0, 90, 180), orientation="right"),
    ]))
    labware_service = LabwareService(SqlLabwareStore(session_factory))
    build = await build_system_over(
        "StoredLabware", Topology(locations={"lh": lh, "pad": Storage("pad")}, transporters=[arm]), stores,
        labwares=[tips], event_bus=EventBus(), ops_history_store=memory_ops_store(),
        labware_service=labware_service, unsaved_positions=in_memory_unsaved_positions(),
    )
    return build, labware_service, tips


async def test_a_rack_only_the_database_knows_is_placed_with_its_declared_tips(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    build, labware_service, tips = await _deck_over_a_database(session_factory)
    stored = await tips.create_instance()
    await labware_service.register(stored)
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        await runtime.labware.edit_location(stored.id, RACK_SITE, reason="it is on the deck", confirm=True)

        assert (await build.system.labware_contents.of(stored.ref)).is_known
        await runtime.labware.edit_location(stored.id, RACK_SITE, reason="still there", confirm=True)
        assert [labware.id for labware in build.system.labwares].count(stored.id) == 1
    finally:
        await runtime.shutdown(confirm=True)


async def test_a_second_statement_while_the_first_enters_the_rack_places_the_one_object_with_its_tips(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch,
) -> None:
    build, labware_service, tips = await _deck_over_a_database(session_factory)
    stored = await tips.create_instance()
    await labware_service.register(stored)
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    await runtime.start()
    ledger = build.system.labware_contents
    seed_at_birth = ledger.seed_at_birth
    entering = asyncio.Event()
    finish_entering = asyncio.Event()

    async def held_seed_at_birth(labware: LabwareRef, declared: InitialStateDetails | None) -> None:
        if not entering.is_set():
            entering.set()
            await finish_entering.wait()
        await seed_at_birth(labware, declared)

    monkeypatch.setattr(ledger, "seed_at_birth", held_seed_at_birth)
    placer = build.system.labware_placer
    arrived = placer.arrived
    tips_known_when_placed: list[bool] = []

    async def noted_arrived(labware: LabwareInstance, location: Location, how: Arrival) -> None:
        tips_known_when_placed.append((await ledger.of(labware.ref)).is_known)
        await arrived(labware, location, how)

    monkeypatch.setattr(placer, "arrived", noted_arrived)
    try:
        first = asyncio.create_task(
            runtime.labware.edit_location(stored.id, RACK_SITE, reason="it is on the deck", confirm=True),
        )
        await asyncio.wait_for(entering.wait(), timeout=10)
        second = asyncio.create_task(
            runtime.labware.edit_location(stored.id, RACK_SITE, reason="it is on the deck", confirm=True),
        )
        finish_entering.set()
        await asyncio.gather(first, second)

        [held] = [labware for labware in build.system.labwares if labware.id == stored.id]
        assert build.system.system_map.get_location(RACK_SITE).labware is held
        assert tips_known_when_placed and all(tips_known_when_placed)
    finally:
        finish_entering.set()
        await runtime.shutdown(confirm=True)


async def test_a_finished_statement_leaves_no_lock_behind_for_its_labware(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    build, labware_service, tips = await _deck_over_a_database(session_factory)
    stored = await tips.create_instance()
    await labware_service.register(stored)
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        await runtime.labware.edit_location(stored.id, RACK_SITE, reason="it is on the deck", confirm=True)
        await runtime.labware.reset_location(stored.id, RACK_SITE, reason="start its history over", confirm=True)

        assert isinstance(runtime.labware, LabwareFacade)
        assert len(runtime.labware._statements) == 0
    finally:
        await runtime.shutdown(confirm=True)

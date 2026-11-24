"""A carry override outlives the process that set it.

Layer four is the only move-parameter layer stored against one physical object
rather than a registry, so the labware row is the only thing that remembers it.
A restart that dropped it would carry a lidded plate as a bare one on the very
next move, with nothing in the log to say why the numbers changed.
"""

import pytest
from cheshire_drivers.move_parameters import MoveParameterPatch
from orca.resource_models.labware import LabwareInstance
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from orca.runtime.db.base import Base
from orca.runtime.sql_labware_store import SqlLabwareStore


pytestmark = pytest.mark.asyncio


@pytest.fixture
async def store():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield SqlLabwareStore(async_sessionmaker(engine, expire_on_commit=False))
    await engine.dispose()


def _plate() -> LabwareInstance:
    return LabwareInstance("plate_tmpl", "costar_96")


async def test_a_plate_carried_normally_stores_nothing(store) -> None:
    """"Carried like anything else" gets one representation, not an empty blob
    that later reads as a plate somebody tuned."""
    plate = _plate()
    await store.register(plate)

    restored = await store.get_by_id(plate.id)

    assert restored is not None
    assert restored.carry_override == MoveParameterPatch()


async def test_an_override_comes_back_after_a_restart(store) -> None:
    plate = _plate()
    plate.carry_with(z_offset=2.0, speed=30.0)

    await store.register(plate)
    restored = await store.get_by_id(plate.id)

    assert restored is not None
    assert restored.carry_override == MoveParameterPatch(z_offset=2.0, speed=30.0)


async def test_re_registering_records_the_new_override(store) -> None:
    """The second register is an update, not an insert, so the row has to be
    written on that path too or an edit is lost at the next restart."""
    plate = _plate()
    await store.register(plate)

    plate.carry_with(speed=30.0)
    await store.register(plate)

    restored = await store.get_by_id(plate.id)
    assert restored is not None
    assert restored.carry_override == MoveParameterPatch(speed=30.0)


async def test_carrying_normally_again_clears_the_row(store) -> None:
    plate = _plate()
    plate.carry_with(speed=30.0)
    await store.register(plate)

    plate.carry_normally()
    await store.register(plate)

    restored = await store.get_by_id(plate.id)
    assert restored is not None
    assert restored.carry_override == MoveParameterPatch()

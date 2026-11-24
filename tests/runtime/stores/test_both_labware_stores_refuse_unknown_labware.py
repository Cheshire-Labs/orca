"""Every ILabwareStore refuses a location update for labware it never registered."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.store_interfaces import ILabwareStore
from orca.runtime.errors import LabwareNotFoundError
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.sql_labware_store import SqlLabwareStore


@pytest.fixture(params=["in_memory", "sql"])
def store(
    request: pytest.FixtureRequest, session_factory: async_sessionmaker[AsyncSession],
) -> ILabwareStore:
    if request.param == "in_memory":
        return InMemoryLabwareStore()
    return SqlLabwareStore(session_factory)


async def test_a_location_update_for_unregistered_labware_is_refused(store: ILabwareStore) -> None:
    with pytest.raises(LabwareNotFoundError) as refused:
        await store.update_location("never-registered", "pad_1")
    assert refused.value.labware_id == "never-registered"


async def test_a_registered_labware_takes_its_location(store: ILabwareStore) -> None:
    labware = LabwareInstance(template_name="plate_96", labware_type="plate")
    await store.register(labware)

    await store.update_location(labware.id, "pad_1")

    found = await store.get_by_position("pad_1")
    assert found is not None
    assert found.id == labware.id

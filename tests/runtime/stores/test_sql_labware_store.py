"""SqlLabwareStore over SQLite."""

from collections.abc import Sequence
from pathlib import Path

import pytest
from orca.resource_models.labware import LabwareInstance
from orca.runtime.errors import BarcodeInUseError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from orca.runtime.db.base import Base
from orca.runtime.db.models import LabwareInstanceRow
from orca.runtime.sql_labware_store import SqlLabwareStore


pytestmark = pytest.mark.asyncio


async def test_register_and_get_by_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlLabwareStore(session_factory)
    instance = LabwareInstance(
        template_name="tips_384", labware_type="tiprack", barcode="BC-001"
    )

    await store.register(instance, execution_id="exec-1")
    loaded = await store.get_by_id(instance.id)

    assert loaded is not None
    assert loaded.id == instance.id
    assert loaded.template_name == "tips_384"
    # The rehydrated name must be the same instance_name_for shape the mint
    # produced -- it is the driver-deck key and ledger join key.
    assert loaded.name == instance.name
    assert loaded.barcode == "BC-001"


async def test_get_by_barcode(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlLabwareStore(session_factory)
    instance = LabwareInstance(
        template_name="assay_plate", labware_type="plate", barcode="BC-042"
    )
    await store.register(instance)

    loaded = await store.get_by_barcode("BC-042")
    assert loaded is not None
    assert loaded.id == instance.id


async def test_get_missing_returns_none(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlLabwareStore(session_factory)
    assert await store.get_by_id("nope") is None
    assert await store.get_by_barcode("nope") is None
    assert await store.get_by_position("nowhere") is None


async def test_get_by_position_returns_resident_at_position(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlLabwareStore(session_factory)
    instance = LabwareInstance(template_name="reservoir", labware_type="trough")
    await store.register(instance)
    await store.update_location(instance.id, "lh/carrier-25-0")

    loaded = await store.get_by_position("lh/carrier-25-0")
    assert loaded is not None
    assert loaded.id == instance.id
    assert loaded.template_name == "reservoir"


async def test_update_location(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlLabwareStore(session_factory)
    instance = LabwareInstance(template_name="plate", labware_type="plate")
    await store.register(instance, execution_id="exec-1")

    await store.update_location(instance.id, "shaker-1")
    await store.update_location(instance.id, "reader-1")


async def test_update_location_unknown_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlLabwareStore(session_factory)
    with pytest.raises(KeyError):
        await store.update_location("missing", "anywhere")


async def test_list_active_locations_returns_pairs_for_placed_labware(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlLabwareStore(session_factory)
    plate_a = LabwareInstance(template_name="plate", labware_type="plate")
    plate_b = LabwareInstance(template_name="plate", labware_type="plate")
    plate_c = LabwareInstance(template_name="plate", labware_type="plate")
    await store.register(plate_a)
    await store.register(plate_b)
    await store.register(plate_c)

    await store.update_location(plate_a.id, "shaker-1")
    await store.update_location(plate_b.id, "reader-1")
    # plate_c never placed; should be excluded.

    pairs = await store.list_active_locations()
    pair_dict = dict(pairs)
    assert pair_dict[plate_a.id] == "shaker-1"
    assert pair_dict[plate_b.id] == "reader-1"
    assert plate_c.id not in pair_dict


async def test_list_active_locations_empty_when_no_placements(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlLabwareStore(session_factory)
    instance = LabwareInstance(template_name="plate", labware_type="plate")
    await store.register(instance)

    assert await store.list_active_locations() == []


async def test_register_idempotent_on_same_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlLabwareStore(session_factory)
    instance = LabwareInstance(template_name="plate", labware_type="plate")
    await store.register(instance)
    instance._barcode = "BC-NEW"  # noqa: SLF001 -- exercising re-register update path
    await store.register(instance, execution_id="exec-2")

    loaded = await store.get_by_id(instance.id)
    assert loaded is not None
    assert loaded.barcode == "BC-NEW"


async def test_clear_location_keeps_row_and_history(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A lifecycle end is not a retraction: the active position clears (so a
    reboot cannot resurrect the labware at its end slot), but identity and
    the location trail stay queryable after discharge."""
    store = SqlLabwareStore(session_factory)
    instance = LabwareInstance(
        template_name="assay_plate", labware_type="plate", barcode="BC-77"
    )
    await store.register(instance)
    await store.update_location(instance.id, "pad_1")

    await store.clear_location(instance.id)

    assert await store.list_active_locations() == []
    assert await store.get_by_id(instance.id) is not None
    history = await store.get_location_history(instance.id)
    assert [name for name, _prev, _ts in history] == ["pad_1"]


async def test_clear_location_is_idempotent_and_tolerates_unknown_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlLabwareStore(session_factory)
    await store.clear_location("nope")
    instance = LabwareInstance(template_name="p", labware_type="plate")
    await store.register(instance)
    await store.clear_location(instance.id)
    await store.clear_location(instance.id)
    assert await store.list_active_locations() == []


async def test_register_tolerates_a_competing_writer_taking_the_same_identity(
    tmp_path: Path,
) -> None:
    """The runtime mirrors placements from its own task, so an operator
    register and that mirror can both find an identity unregistered and both
    insert it. The one that loses must land as an update, not blow up the
    operator's call."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'labware.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    plain = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    instance = LabwareInstance(template_name="r5_tips", labware_type="tiprack")
    competitor = SqlLabwareStore(plain)
    competitor_has_written = False

    class _CompetingWriterCommitsFirst(AsyncSession):
        async def flush(self, objects: Sequence[LabwareInstanceRow] | None = None) -> None:
            nonlocal competitor_has_written
            if not competitor_has_written:
                competitor_has_written = True
                await competitor.register(instance)
            await super().flush(objects)

    store = SqlLabwareStore(
        async_sessionmaker(
            engine, class_=_CompetingWriterCommitsFirst, expire_on_commit=False
        )
    )

    await store.register(instance, execution_id="exec-1")

    loaded = await store.get_by_id(instance.id)
    assert loaded is not None
    assert loaded.template_name == "r5_tips"
    await engine.dispose()


async def test_register_still_rejects_a_second_identity_claiming_one_barcode(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Falling through to an update covers our own id only. Two labware
    claiming one barcode is a real conflict and must still be refused."""
    store = SqlLabwareStore(session_factory)
    first = LabwareInstance(
        template_name="r5_plate", labware_type="plate", barcode="BC-DUP"
    )
    second = LabwareInstance(
        template_name="r5_plate", labware_type="plate", barcode="BC-DUP"
    )
    await store.register(first)

    with pytest.raises(BarcodeInUseError) as exc_info:
        await store.register(second)
    assert exc_info.value.barcode == "BC-DUP"
    assert exc_info.value.existing_labware_id == first.id


async def test_changing_a_barcode_to_one_another_labware_holds_is_refused_naming_it(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """edit-barcode re-registers an existing row, so the refusal comes from the
    update, not the insert."""
    store = SqlLabwareStore(session_factory)
    holder = LabwareInstance(template_name="p", labware_type="plate", barcode="BC-A")
    other = LabwareInstance(template_name="p", labware_type="plate", barcode="BC-B")
    await store.register(holder)
    await store.register(other)
    other.barcode = "BC-A"

    with pytest.raises(BarcodeInUseError) as exc_info:
        await store.register(other)
    assert exc_info.value.existing_labware_id == holder.id
    reloaded = await store.get_by_id(other.id)
    assert reloaded is not None and reloaded.barcode == "BC-B"

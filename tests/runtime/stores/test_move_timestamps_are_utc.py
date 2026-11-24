"""A move's timestamp is the UTC instant it happened.

The `labware_events.ts` column has no time zone, and `utc_now()` writes UTC
into it. `datetime.timestamp()` reads a naive value as LOCAL time, so the old
projection shifted every move by the machine's UTC offset. On the bench (EDT)
every move came back four hours in the future, which put every move after every
action in `get-labware-journey`'s chronological merge whatever really happened
first. Actions were never affected: they are stamped with `time.time()`.
"""

import time
from datetime import datetime, timezone

from orca.resource_models.labware import LabwareInstance
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orca.runtime.db.base import epoch_seconds
from orca.runtime.db.models import LabwareEventRow
from orca.runtime.sql_labware_store import SqlLabwareStore


async def test_epoch_seconds_reads_a_naive_column_as_utc() -> None:
    # 2026-08-31T16:00:00Z. Hard-coded because the defect is invisible on a
    # machine whose local zone is UTC, and the contract is absolute.
    stored = datetime(2026, 8, 31, 16, 0, 0)

    assert epoch_seconds(stored) == 1788192000.0


async def test_epoch_seconds_leaves_an_aware_value_alone() -> None:
    aware = datetime(2026, 8, 31, 16, 0, 0, tzinfo=timezone.utc)

    assert epoch_seconds(aware) == 1788192000.0


async def test_a_stored_move_reads_back_as_the_instant_it_was_stored(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The clock a journey merges on, end to end.

    The stored value is pinned rather than taken from the wall clock: read as
    local time it comes back shifted by the machine's UTC offset, which is
    zero on a UTC machine and so invisible there. CI runs on UTC.
    """
    store = SqlLabwareStore(session_factory)
    instance = LabwareInstance(template_name="assay_plate", labware_type="plate")
    await store.register(instance)
    await store.update_location(instance.id, "pad_1")

    # 2026-08-31T16:00:00Z, written the way utc_now() writes: naive UTC.
    async with session_factory() as session:
        result = await session.execute(
            select(LabwareEventRow).where(
                LabwareEventRow.labware_id == instance.id,
                LabwareEventRow.event_type == "relocated",
            )
        )
        row = result.scalars().one()
        row.ts = datetime(2026, 8, 31, 16, 0, 0)
        await session.commit()

    history = await store.get_location_history(instance.id)
    (_name, _previous, ts), = history
    assert ts == 1788192000.0, (
        f"stored 2026-08-31T16:00:00Z, read back {ts}; the column is naive "
        f"UTC and must not be read as local time"
    )


async def test_a_move_is_not_stamped_in_the_future(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The clock a journey merges on. A move recorded now must read as now,
    not as now plus the machine's UTC offset."""
    store = SqlLabwareStore(session_factory)
    instance = LabwareInstance(template_name="assay_plate", labware_type="plate")
    await store.register(instance)

    before = time.time()
    await store.update_location(instance.id, "pad_1")
    after = time.time()

    history = await store.get_location_history(instance.id)
    (_name, _previous, ts), = history
    assert before - 1 <= ts <= after + 1, (
        f"move stamped {ts - after:.0f}s away from the wall clock; the column "
        f"is naive UTC and must not be read as local time"
    )

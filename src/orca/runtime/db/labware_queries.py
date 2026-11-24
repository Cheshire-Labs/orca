"""The labware store's reads and writes, for any database. Identity and position are in
``labware_instances``; what happened to each labware is in ``labware_events``."""

from typing import Optional

from cheshire_drivers.move_parameters import MoveParameterPatch
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from orca.resource_models.labware import LabwareInstance, instance_name_for
from orca.runtime.db.base import epoch_seconds
from orca.runtime.db.models import LabwareEventRow, LabwareInstanceRow
from orca.runtime.errors import BarcodeInUseError, LabwareNotFoundError


def _carry_override_row(
    instance: LabwareInstance,
) -> Optional[dict[str, JsonValue]]:
    """Sparse on the way in: absent when this labware is carried normally."""
    named = instance.carry_override.model_dump(exclude_none=True)
    return named or None


def _rehydrate(row: LabwareInstanceRow) -> LabwareInstance:
    """Build a LabwareInstance from a persisted row.

    The name comes from `instance_name_for`, because the name is the driver-deck key.
    `template` and `ops_source` stay unset: they belong to a live runtime graph.
    """
    instance = LabwareInstance(
        template_name=row.template_name,
        labware_type=row.labware_type,
        barcode=row.barcode,
        instance_id=row.id,
        name=instance_name_for(row.template_name, row.id),
    )
    instance.restore_carry_override(
        MoveParameterPatch.model_validate(row.carry_override or {}),
    )
    return instance


async def _raise_if_barcode_held_elsewhere(
    session: AsyncSession, instance: LabwareInstance,
) -> None:
    """Name the holder when a failed write was another labware's barcode."""
    if instance.barcode is None:
        return
    holder_id = (await session.execute(
        select(LabwareInstanceRow.id).where(LabwareInstanceRow.barcode == instance.barcode)
    )).scalar_one_or_none()
    if holder_id is not None and holder_id != instance.id:
        raise BarcodeInUseError(instance.barcode, holder_id)


async def register(
    session: AsyncSession, instance: LabwareInstance, execution_id: str | None,
) -> None:
    """Persist a labware identity, whether or not it is already stored.

    Registering the same identity twice is normal: the operator surfaces and
    the placer, saving a position, both write identities, and two tasks can do
    it at once. So an insert that loses that race falls through to the update
    path. A second labware claiming one barcode raises
    BarcodeInUseError naming the holder.
    """
    row = await session.get(LabwareInstanceRow, instance.id)
    if row is None:
        session.add(
            LabwareInstanceRow(
                id=instance.id,
                template_name=instance.template_name,
                labware_type=instance.labware_type,
                barcode=instance.barcode,
                position_id=None,
                execution_id=execution_id,
                carry_override=_carry_override_row(instance),
            )
        )
        try:
            # The event's foreign key needs the row inserted first; one flush may order them either way.
            await session.flush()
            session.add(
                LabwareEventRow(
                    labware_id=instance.id,
                    event_type="registered",
                    detail={
                        "template_name": instance.template_name,
                        "labware_type": instance.labware_type,
                        "barcode": instance.barcode,
                        "execution_id": execution_id,
                    },
                )
            )
            await session.commit()
            return
        except IntegrityError:
            await session.rollback()
            row = await session.get(LabwareInstanceRow, instance.id)
            if row is None:
                await _raise_if_barcode_held_elsewhere(session, instance)
                raise
    row.template_name = instance.template_name
    row.labware_type = instance.labware_type
    row.barcode = instance.barcode
    row.execution_id = execution_id
    row.carry_override = _carry_override_row(instance)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        await _raise_if_barcode_held_elsewhere(session, instance)
        raise


async def get_by_id(session: AsyncSession, labware_id: str) -> LabwareInstance | None:
    row = await session.get(LabwareInstanceRow, labware_id)
    return None if row is None else _rehydrate(row)


async def get_by_barcode(session: AsyncSession, barcode: str) -> LabwareInstance | None:
    row = (await session.execute(
        select(LabwareInstanceRow).where(LabwareInstanceRow.barcode == barcode)
    )).scalar_one_or_none()
    return None if row is None else _rehydrate(row)


async def get_by_position(session: AsyncSession, position_id: str) -> LabwareInstance | None:
    """The most recently registered labware persisted at ``position_id``, or None."""
    row = (await session.execute(
        select(LabwareInstanceRow)
        .where(LabwareInstanceRow.position_id == position_id)
        .order_by(LabwareInstanceRow.created_at.desc())
    )).scalars().first()
    return None if row is None else _rehydrate(row)


async def list_active_locations(session: AsyncSession) -> list[tuple[str, str]]:
    """(labware_id, position_id) for every persisted labware at a known location."""
    result = await session.execute(
        select(LabwareInstanceRow.id, LabwareInstanceRow.position_id)
        .where(LabwareInstanceRow.position_id.is_not(None))
    )
    return [
        (labware_id, position_id)
        for labware_id, position_id in result.all()
        if position_id is not None
    ]


async def list_departed_locations(session: AsyncSession) -> list[tuple[str, str]]:
    """(labware_id, last position_id) for every persisted labware whose position was cleared."""
    result = await session.execute(
        select(LabwareEventRow.labware_id, LabwareEventRow.detail)
        .join(LabwareInstanceRow, LabwareInstanceRow.id == LabwareEventRow.labware_id)
        .where(LabwareInstanceRow.position_id.is_(None), LabwareEventRow.event_type == "departed")
        .order_by(LabwareEventRow.id.asc())
    )
    last: dict[str, str] = {}
    for labware_id, detail in result.all():
        origin = detail.get("from") if isinstance(detail, dict) else None
        if isinstance(origin, str):
            last[labware_id] = origin
    return list(last.items())


async def update_location(session: AsyncSession, labware_id: str, position_id: str) -> None:
    row = await session.get(LabwareInstanceRow, labware_id)
    if row is None:
        raise LabwareNotFoundError(labware_id)
    previous = row.position_id
    row.position_id = position_id
    session.add(
        LabwareEventRow(
            labware_id=labware_id,
            event_type="relocated",
            detail={"from": previous, "to": position_id},
        )
    )
    await session.commit()


async def clear_location(session: AsyncSession, labware_id: str) -> None:
    """Drop the labware's active position, keeping its row and history.

    Labware leaving the world is not a retraction: identity, events and the ops
    trail stay queryable, and ``list_active_locations`` stops returning it so a
    reboot does not bring it back. An unknown id or a clear position is a no-op.
    """
    row = await session.get(LabwareInstanceRow, labware_id)
    if row is None or row.position_id is None:
        return
    previous = row.position_id
    row.position_id = None
    session.add(
        LabwareEventRow(
            labware_id=labware_id,
            event_type="departed",
            detail={"from": previous, "to": None},
        )
    )
    await session.commit()


async def get_location_history(
    session: AsyncSession, labware_id: str,
) -> list[tuple[str, str | None, float]]:
    """(position_id, previous_position_id, timestamp) for each move, oldest first.

    ``ts`` holds UTC in a naive ``datetime``, so it goes through ``epoch_seconds``:
    ``datetime.timestamp()`` would read it as local time.
    """
    rows = (await session.execute(
        select(LabwareEventRow)
        .where(
            LabwareEventRow.labware_id == labware_id,
            LabwareEventRow.event_type == "relocated",
        )
        .order_by(LabwareEventRow.id.asc())
    )).scalars().all()
    events: list[tuple[str, str | None, float]] = []
    for row in rows:
        detail = row.detail or {}
        to_name = detail.get("to") if isinstance(detail, dict) else None
        from_name = detail.get("from") if isinstance(detail, dict) else None
        if isinstance(to_name, str):
            events.append((
                to_name,
                from_name if isinstance(from_name, str) else None,
                epoch_seconds(row.ts),
            ))
    return events

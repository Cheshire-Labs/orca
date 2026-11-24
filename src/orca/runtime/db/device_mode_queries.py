"""The device-mode store's reads and writes, for any database. A device with no row is sim."""

from sqlalchemy.ext.asyncio import AsyncSession

from orca.modes.device_modes import DeviceMode
from orca.runtime.db.base import utc_now
from orca.runtime.db.models import DeviceModeRow


async def mode_of(session: AsyncSession, device_name: str) -> DeviceMode:
    row = await session.get(DeviceModeRow, device_name)
    return DeviceMode.SIM if row is None else DeviceMode(row.mode)


async def set_mode(session: AsyncSession, device_name: str, mode: DeviceMode) -> None:
    await session.merge(DeviceModeRow(device_name=device_name, mode=mode, changed_at=utc_now()))
    await session.commit()

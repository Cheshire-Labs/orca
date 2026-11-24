"""A barcode edit the store refuses leaves the labware's barcode as it was."""

import pytest

from orca.resource_models.labware import LabwareInstance
from orca.runtime.labware_service import LabwareService
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.errors import BarcodeInUseError

from tests.test_system_runtime import _build_simple_system
from tests.memory_activity import memory_runtime

TAKEN = "BC-TAKEN"


class _StoreWhereOneBarcodeIsTaken(InMemoryLabwareStore):
    async def register(
        self, instance: LabwareInstance, execution_id: str | None = None,
    ) -> None:
        if instance.barcode == TAKEN:
            raise BarcodeInUseError(TAKEN, "lw-holder")
        await super().register(instance, execution_id)


@pytest.mark.asyncio
async def test_a_refused_edit_leaves_the_old_barcode_in_place() -> None:
    system, _ = await _build_simple_system(labware_service=LabwareService(_StoreWhereOneBarcodeIsTaken()))
    runtime = await memory_runtime(system)
    await runtime.start()
    try:
        snap = await runtime.labware.register(
            "plate_96", barcode="BC-OLD", location="pad1", confirm=True,
        )

        with pytest.raises(BarcodeInUseError):
            await runtime.labware.edit_barcode(snap.id, TAKEN, confirm=True)

        assert (await runtime.labware.get_by_id(snap.id)).barcode == "BC-OLD"
    finally:
        await runtime.shutdown(confirm=True)

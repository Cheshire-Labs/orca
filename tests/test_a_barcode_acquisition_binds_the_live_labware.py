"""A submission that names a plate by barcode gets the labware the running system holds, not the store's record of it.

A durable store keeps identity only (id, template name, barcode), so its copy has no template and no deck site can be
chosen for it. A barcode only the store still holds belongs to labware that has left the world, and is refused.
"""

import pytest

from orca.resource_models.labware import LabwareInstance
from orca.workflow_models.labware_group import AcquisitionValidationError, BarcodeAcquisition, LabwareGroup, LabwareGroupMember
from orca.runtime.labware_service import LabwareService
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.modes.run_modes import WorkflowRunMode
from tests.memory_activity import memory_runtime
from tests.test_system_runtime import _build_simple_system


class IdentityOnlyStore(InMemoryLabwareStore):
    """Hands back a fresh identity-only copy, as a database-backed store does."""

    async def get_by_barcode(self, barcode: str) -> LabwareInstance | None:
        stored = await super().get_by_barcode(barcode)
        if stored is None:
            return None
        return LabwareInstance(
            stored.template_name, stored.labware_type, barcode=stored.barcode,
            instance_id=stored.id, name=stored.name,
        )


def _by_barcode(barcode: str) -> LabwareGroup:
    member = LabwareGroupMember(thread_template_name="plate_96", acquisition=BarcodeAcquisition(barcode=barcode))
    return LabwareGroup(id="grp", members=(member,))


async def test_the_thread_gets_the_live_instance_not_the_stores_copy() -> None:
    store = IdentityOnlyStore()
    system, workflow = await _build_simple_system(labware_service=LabwareService(store))
    live = await system.get_labware_template("plate_96").create_instance()
    live.barcode = "BC-7"
    system.add_labware(live)
    await store.register(live)
    runtime = await memory_runtime(system)
    await runtime.start()

    submission = await runtime.submit(workflow, groups=[_by_barcode("BC-7")], mode=WorkflowRunMode.CHECK_RUN)

    resolved = submission.resolved_acquisitions[("grp", "plate_96")]
    assert resolved.labware_instance is live
    await runtime.shutdown()


async def test_a_barcode_only_the_store_holds_is_refused() -> None:
    store = IdentityOnlyStore()
    system, workflow = await _build_simple_system(labware_service=LabwareService(store))
    gone = LabwareInstance("plate_96", "96_well", barcode="BC-8")
    await store.register(gone)
    runtime = await memory_runtime(system)
    await runtime.start()

    with pytest.raises(AcquisitionValidationError, match="BC-8.*not in the running system"):
        await runtime.submit(workflow, groups=[_by_barcode("BC-8")], mode=WorkflowRunMode.CHECK_RUN)
    await runtime.shutdown()

"""Placing onto an occupied slot reaches the operator as a typed 409.

Before this, the refusal was a bare ``DeviceBusyError``. Nothing between
the resource and the wire caught it, so an operator confirming a manual
place onto a full slot got a stripped ``internal_error`` 500 that named
neither the slot nor its occupant.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from orca.operations._protocol import OperationError, OperationErrorCode
from orca.operations.labware import (
    _slot_occupied,
    EditLabwareLocationOperation,
    RegisterLabwareOperation,
    ResetLabwareLocationOperation,
)
from orca.operations.labware_models import (
    EditLabwareLocationRequest,
    RegisterLabwareRequest,
    ResetLabwareLocationRequest,
)
from orca.resource_models.deck_site import DeckSite
from orca.resource_models.device_error import SlotOccupiedError
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_staging_bridge import LabwareStagingBridge
from orca.resource_models.plate_pad import PlatePad
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.system_runtime import SystemRuntime
from orca.resource_models.labware_location_service import (
    InMemoryLabwareLocationService,
)
from orca.workflow_models import spawn_actions

from tests.mock import EXTERNAL_MOVER, UniversalMockDevice
from tests.test_system_runtime import _build_simple_system


def _occupant() -> LabwareInstance:
    return LabwareInstance("plate_1", "corning_96_wellplate_360ul_flat", name="Plate 1")


def _refusal() -> SlotOccupiedError:
    return SlotOccupiedError(
        position_id="pad1",
        existing_labware_name="Plate 1",
        existing_template_name="plate_1",
    )


def _runtime_refusing(method_name: str) -> MagicMock:
    runtime = MagicMock()
    runtime.labware = MagicMock()
    setattr(runtime.labware, method_name, AsyncMock(side_effect=_refusal()))
    return runtime


def _assert_typed_409(raised: OperationError) -> None:
    assert raised.code is OperationErrorCode.CONFLICT
    assert raised.wire_code == "slot_occupied"
    assert raised.status_code == 409
    assert raised.extras == {
        "position_id": "pad1",
        "existing_labware_name": "Plate 1",
        "existing_template_name": "plate_1",
    }


def test_plate_pad_refusal_names_the_occupant() -> None:
    pad = PlatePad("pad1")
    pad.initialize_labware(_occupant())

    with pytest.raises(SlotOccupiedError) as exc_info:
        pad.initialize_labware(LabwareInstance("plate_2", "corning_96_wellplate_360ul_flat"))

    raised = exc_info.value
    assert raised.position_id == "pad1"
    assert raised.existing_labware_name == "Plate 1"
    assert raised.existing_template_name == "plate_1"


def test_deck_site_refusal_names_the_occupant() -> None:
    site = DeckSite("flex:C1")
    site.initialize_labware(_occupant())

    with pytest.raises(SlotOccupiedError) as exc_info:
        site.initialize_labware(LabwareInstance("plate_2", "corning_96_wellplate_360ul_flat"))

    assert exc_info.value.position_id == "flex:C1"


@pytest.mark.asyncio
async def test_spawn_placement_still_retries_the_richer_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spawn placement retries a busy target rather than failing it. The
    refusal grew a subclass, and if that stopped landing in the retry branch a
    slot that clears a moment later would become a hard failure instead."""
    monkeypatch.setattr(spawn_actions, "_RETRY_BACKOFF_S", 0)
    attempts: list[LabwareInstance] = []
    arriving = LabwareInstance("plate_2", "corning_96_wellplate_360ul_flat")

    async def _place(labware: LabwareInstance) -> None:
        attempts.append(labware)
        if len(attempts) == 1:
            raise _refusal()

    location = MagicMock()
    location.name = "pad1"
    location.labware = _occupant()
    location.place_labware = _place

    await spawn_actions._place_retrying_on_busy(
        location, arriving, "test", InMemoryLabwareLocationService(),
    )

    assert attempts == [arriving, arriving]


@pytest.mark.asyncio
async def test_register_onto_occupied_slot_surfaces_typed_409() -> None:
    op = RegisterLabwareOperation(runtime=_runtime_refusing("register"))

    with pytest.raises(OperationError) as exc_info:
        await op.run(RegisterLabwareRequest(template_name="plate_2", location="pad1"))

    _assert_typed_409(exc_info.value)


@pytest.mark.asyncio
async def test_edit_location_onto_occupied_slot_surfaces_typed_409() -> None:
    op = EditLabwareLocationOperation(runtime=_runtime_refusing("edit_location"))

    with pytest.raises(OperationError) as exc_info:
        await op.run(EditLabwareLocationRequest(
            labware_id="lw1", location="pad1", reason="operator moved it",
        ))

    _assert_typed_409(exc_info.value)


@pytest.mark.asyncio
async def test_reset_location_onto_occupied_slot_surfaces_typed_409() -> None:
    op = ResetLabwareLocationOperation(runtime=_runtime_refusing("reset_location"))

    with pytest.raises(OperationError) as exc_info:
        await op.run(ResetLabwareLocationRequest(
            labware_id="lw1", location="pad1", reason="operator moved it",
        ))

    _assert_typed_409(exc_info.value)


@pytest.mark.asyncio
async def test_device_site_refuses_an_assert_onto_a_loaded_plate() -> None:
    """A single-slot device holds one plate, and loading it empties the stage
    slot without emptying the site. The transporter path already refused a
    second plate here; the assert-placement path used to stage one on top."""
    site = LabwareStagingBridge("shaker1", UniversalMockDevice("shaker1"))
    resident = _occupant()
    await site.notify_placed(resident, EXTERNAL_MOVER)
    assert site.labware is resident

    with pytest.raises(SlotOccupiedError) as exc_info:
        site.initialize_labware(
            LabwareInstance("plate_2", "corning_96_wellplate_360ul_flat"),
        )

    raised = exc_info.value
    assert raised.position_id == "shaker1"
    assert raised.existing_labware_name == "Plate 1"


@pytest.mark.asyncio
async def test_device_site_takes_an_assert_of_the_plate_it_already_holds() -> None:
    """Re-asserting the resident instance is a no-op on a pad and a deck site,
    and must be one here too. A reset-location onto the slot the plate is
    already on goes down this path, and the stage write underneath refuses
    even the same instance."""
    site = LabwareStagingBridge("shaker1", UniversalMockDevice("shaker1"))
    resident = _occupant()
    site.initialize_labware(resident)
    assert site.labware is resident

    site.initialize_labware(resident)

    assert site.labware is resident


@pytest.mark.asyncio
async def test_device_site_refuses_a_transit_place_with_the_same_typed_refusal() -> None:
    """The transporter path enforces the same one-plate-per-site rule as the
    assert path, so a transit collision must name its occupant too instead of
    raising something no surface can read."""
    site = LabwareStagingBridge("shaker1", UniversalMockDevice("shaker1"))
    await site.notify_placed(_occupant(), EXTERNAL_MOVER)

    with pytest.raises(SlotOccupiedError) as exc_info:
        await site.prepare_for_place(
            LabwareInstance("plate_2", "corning_96_wellplate_360ul_flat"),
            EXTERNAL_MOVER,
        )

    raised = exc_info.value
    assert raised.position_id == "shaker1"
    assert raised.existing_labware_name == "Plate 1"
    assert raised.existing_template_name == "plate_1"


@pytest.mark.asyncio
async def test_register_onto_a_genuinely_occupied_pad_answers_the_typed_409() -> None:
    """The stubbed-facade tests above pin the mapping from a hand-built
    refusal. This one pins the join: a real pad holding a real plate raises,
    and the object it raises is the one the operation converts, occupant and
    all. Nothing else covers that the two halves meet."""
    system, _ = await _build_simple_system()
    runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
    await runtime.start()
    try:
        occupant = await runtime.labware.register(
            "plate_96", location="pad1", confirm=True,
        )

        with pytest.raises(SlotOccupiedError) as exc_info:
            await runtime.labware.register(
                "plate_96", location="pad1", confirm=True,
            )

        envelope = _slot_occupied(exc_info.value)
        assert envelope.code is OperationErrorCode.CONFLICT
        assert envelope.wire_code == "slot_occupied"
        assert envelope.status_code == 409
        assert envelope.extras == {
            "position_id": "pad1",
            "existing_labware_name": occupant.name,
            "existing_template_name": "plate_96",
        }
    finally:
        await runtime.shutdown()

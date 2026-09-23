"""A position another thread reserved reaches the operator as a typed 409.

The refusal is only useful if it survives the trip to the wire with the claim
attached: cancelling that reservation, aborting the thread holding it, or
choosing a different position is the whole of what the operator can do about
it, and none of those is a choice without knowing which claim it is.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from orca.operations._protocol import OperationError, OperationErrorCode
from orca.operations.labware import (
    EditLabwareLocationOperation,
    RegisterLabwareOperation,
    ResetLabwareLocationOperation,
)
from orca.operations.labware_models import (
    EditLabwareLocationRequest,
    RegisterLabwareRequest,
    ResetLabwareLocationRequest,
)
from orca.runtime.runtime_interface import LocationReservedError


def _runtime_refusing(method: str, error: LocationReservedError) -> MagicMock:
    runtime = MagicMock()
    runtime.labware = MagicMock()
    setattr(runtime.labware, method, AsyncMock(side_effect=error))
    return runtime


async def _edit(error: LocationReservedError) -> OperationError:
    operation = EditLabwareLocationOperation(
        runtime=_runtime_refusing("edit_location", error),
    )
    with pytest.raises(OperationError) as exc_info:
        await operation.run(EditLabwareLocationRequest(
            labware_id="lw1", location="shaker1/slot", reason="operator moved it",
        ))
    return exc_info.value


async def test_the_refusal_carries_the_claim_to_the_wire() -> None:
    raised = await _edit(LocationReservedError(
        "shaker1/slot", "rsv-1", "thread-7", "plate_96-a1b2c3d4",
    ))

    assert raised.code is OperationErrorCode.CONFLICT
    assert raised.wire_code == "location_reserved"
    assert raised.status_code == 409
    assert raised.extras == {
        "position_id": "shaker1/slot",
        "reservation_id": "rsv-1",
        "holder_thread_id": "thread-7",
        "holder_thread_name": "plate_96-a1b2c3d4",
        "inbound_labware": None,
        "inbound_from": None,
        "awaiting_operator": False,
    }


async def test_a_hold_no_thread_owns_still_refuses_and_says_so() -> None:
    """Not every reservation belongs to a workflow thread. The operator gets
    the same 409 and the same claim to cancel, with nothing invented about who
    to go and look at."""
    raised = await _edit(LocationReservedError("pad2", "rsv-2", None))

    assert raised.wire_code == "location_reserved"
    assert raised.extras == {
        "position_id": "pad2",
        "reservation_id": "rsv-2",
        "holder_thread_id": None,
        "holder_thread_name": None,
        "inbound_labware": None,
        "inbound_from": None,
        "awaiting_operator": False,
    }
    assert "no thread" in raised.message
    assert "rsv-2" in raised.message, "name the claim, since nothing else is"


async def test_the_sibling_verb_refuses_through_the_same_envelope() -> None:
    """``reset_location`` writes the same holders, so a client must not have
    to learn a second shape to find out the position was spoken for."""
    operation = ResetLabwareLocationOperation(
        runtime=_runtime_refusing(
            "reset_location",
            LocationReservedError("pad2", "rsv-3", "thread-9", "plate_96-dd"),
        ),
    )

    with pytest.raises(OperationError) as exc_info:
        await operation.run(ResetLabwareLocationRequest(
            labware_id="lw1", location="pad2", reason="operator moved it",
        ))

    assert exc_info.value.wire_code == "location_reserved"
    assert exc_info.value.status_code == 409


async def test_register_refuses_through_the_same_envelope() -> None:
    """The third verb that states a position. A refusal that reaches the wire
    as a 500 tells the operator nothing they can act on, and every article
    documenting this says 409 with the holder named."""
    operation = RegisterLabwareOperation(
        runtime=_runtime_refusing(
            "register",
            LocationReservedError("pad2", "rsv-4", "thread-3", "plate_96-ee"),
        ),
    )

    with pytest.raises(OperationError) as exc_info:
        await operation.run(RegisterLabwareRequest(
            template_name="plate_96", location="pad2",
        ))

    assert exc_info.value.wire_code == "location_reserved"
    assert exc_info.value.status_code == 409
    assert exc_info.value.extras is not None
    assert exc_info.value.extras["holder_thread_name"] == "plate_96-ee"


async def test_a_plate_on_its_way_reaches_the_wire_named() -> None:
    """The operator has to know the difference between "a plate is coming, wait
    for it" and "somebody else was asked to fill this". Both are 409 on the same
    code, so the difference has to be in the extras, not only in the prose."""
    raised = await _edit(LocationReservedError(
        "pad1", "rsv-5", "thread-7", "r6_source-56e0",
        inbound_labware="r6_source-56e0", inbound_from="flex_1/C4-slot",
    ))

    assert raised.extras is not None
    assert raised.extras["inbound_labware"] == "r6_source-56e0"
    assert raised.extras["inbound_from"] == "flex_1/C4-slot"
    assert raised.extras["awaiting_operator"] is False
    assert "remove it" in raised.message
    assert "take your labware back off" in raised.message.lower()


async def test_a_claim_held_for_another_placement_says_that_instead() -> None:
    """Nothing is on its way, so there is nothing to wait for: what frees the
    position is somebody doing the placement it was claimed for."""
    raised = await _edit(LocationReservedError(
        "pad1", "rsv-6", "thread-9", "plate_96-b2",
        inbound_labware="plate_96-b2", awaiting_operator=True,
    ))

    assert raised.extras is not None
    assert raised.extras["awaiting_operator"] is True
    assert "waiting for someone to place" in raised.message
    assert "rsv-6" in raised.message

"""A barcode another labware holds is refused with a typed 409.

The store refuses a second holder of one barcode. Neither the register nor the
edit-barcode Operation caught that refusal, so it reached the wire as a 500.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from orca.operations._protocol import OperationError, OperationErrorCode
from orca.operations.labware import EditLabwareBarcodeOperation, RegisterLabwareOperation
from orca.operations.labware_models import EditLabwareBarcodeRequest, RegisterLabwareRequest
from orca.runtime.runtime_interface import BarcodeInUseError


def _runtime_refusing(method_name: str) -> MagicMock:
    runtime = MagicMock()
    setattr(runtime.labware, method_name, AsyncMock(side_effect=BarcodeInUseError(
        barcode="BC-1", existing_labware_id="lw-old",
    )))
    return runtime


def _assert_typed_409(raised: OperationError) -> None:
    assert raised.code is OperationErrorCode.CONFLICT
    assert raised.wire_code == "barcode_in_use"
    assert raised.status_code == 409
    assert raised.extras == {"barcode": "BC-1", "existing_labware_id": "lw-old"}


@pytest.mark.asyncio
async def test_register_with_a_barcode_in_use_answers_409_naming_the_holder() -> None:
    with pytest.raises(OperationError) as exc_info:
        await RegisterLabwareOperation(_runtime_refusing("register")).run(
            RegisterLabwareRequest(template_name="plate_1", barcode="BC-1"),
        )
    _assert_typed_409(exc_info.value)


@pytest.mark.asyncio
async def test_edit_barcode_to_one_in_use_answers_409_naming_the_holder() -> None:
    with pytest.raises(OperationError) as exc_info:
        await EditLabwareBarcodeOperation(_runtime_refusing("edit_barcode")).run(
            EditLabwareBarcodeRequest(labware_id="lw-new", new_barcode="BC-1"),
        )
    _assert_typed_409(exc_info.value)

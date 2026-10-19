"""Registering the wrong template at a slot a thread waits on is a typed 409.

The facade refuses with SpawnIncompatibleError, a RuntimeError. Nothing in the
register Operation caught it, so the operator got a 500 internal_error that
named neither the slot nor the template the thread expects.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from orca.operations._protocol import OperationError, OperationErrorCode
from orca.operations.labware import RegisterLabwareOperation
from orca.operations.labware_models import RegisterLabwareRequest
from orca.runtime.runtime_interface import SpawnIncompatibleError


@pytest.mark.asyncio
async def test_register_of_the_wrong_template_answers_409_naming_both_templates() -> None:
    runtime = MagicMock()
    runtime.labware.register = AsyncMock(side_effect=SpawnIncompatibleError(
        location="pad_1", expected_template="plate_a", actual_template="plate_b",
    ))

    with pytest.raises(OperationError) as exc_info:
        await RegisterLabwareOperation(runtime).run(
            RegisterLabwareRequest(template_name="plate_b", location="pad_1"),
        )

    raised = exc_info.value
    assert raised.code is OperationErrorCode.CONFLICT
    assert raised.wire_code == "spawn_incompatible"
    assert raised.status_code == 409
    assert "expects labware of template 'plate_a', but got 'plate_b'" in raised.message
    assert raised.extras == {
        "location": "pad_1",
        "expected_template": "plate_a",
        "actual_template": "plate_b",
    }

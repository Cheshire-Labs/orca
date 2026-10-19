"""Each reason a confirm is refused has its own wire code."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from orca.operations._protocol import OperationError, OperationErrorCode
from orca.operations.device import ConfirmMountedTipsOperation
from orca.operations.device_models import ConfirmMountedTipsRequest
from orca.operations.labware import ConfirmTipStateOperation, ConfirmWellVolumesOperation
from orca.operations.labware_models import ConfirmTipStateRequest, ConfirmWellVolumesRequest
from orca.state.confirm_refusals import (
    ConfirmRefusedError,
    HeldByUnfinishedActionError,
    NothingToConfirmError,
    WorkLostError,
)

REFUSALS: list[tuple[ConfirmRefusedError, str, dict[str, str]]] = [
    (NothingToConfirmError("no record", settle_with="set-well-volumes"),
     "nothing_to_confirm", {"settle_with": "set-well-volumes"}),
    (HeldByUnfinishedActionError("behind"), "held_by_unfinished_action", {}),
    (WorkLostError("lost", settle_with="set-well-volumes"),
     "work_lost", {"settle_with": "set-well-volumes"}),
]


def _assert_typed_422(
    raised: OperationError, wire_code: str, extras: dict[str, str],
) -> None:
    assert raised.code is OperationErrorCode.INVALID_INPUT
    assert raised.wire_code == wire_code
    assert raised.status_code == 422
    assert raised.extras == extras


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal,wire_code,extras", REFUSALS)
async def test_confirm_well_volumes_refusal_carries_its_own_code(
    refusal: ConfirmRefusedError, wire_code: str, extras: dict[str, str],
) -> None:
    runtime = MagicMock()
    runtime.labware.confirm_well_volumes = AsyncMock(side_effect=refusal)
    with pytest.raises(OperationError) as exc_info:
        await ConfirmWellVolumesOperation(runtime).run(
            ConfirmWellVolumesRequest(labware_id="lw-1"),
        )
    _assert_typed_422(exc_info.value, wire_code, {**extras, "labware_id": "lw-1"})


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal,wire_code,extras", REFUSALS)
async def test_confirm_tip_state_refusal_carries_its_own_code(
    refusal: ConfirmRefusedError, wire_code: str, extras: dict[str, str],
) -> None:
    runtime = MagicMock()
    runtime.labware.confirm_tip_state = AsyncMock(side_effect=refusal)
    with pytest.raises(OperationError) as exc_info:
        await ConfirmTipStateOperation(runtime).run(
            ConfirmTipStateRequest(labware_id="lw-1"),
        )
    _assert_typed_422(exc_info.value, wire_code, {**extras, "labware_id": "lw-1"})


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal,wire_code,extras", REFUSALS)
async def test_confirm_mounted_tips_refusal_carries_its_own_code(
    refusal: ConfirmRefusedError, wire_code: str, extras: dict[str, str],
) -> None:
    runtime = MagicMock()
    runtime.devices.confirm_mounted_tips = AsyncMock(side_effect=refusal)
    with pytest.raises(OperationError) as exc_info:
        await ConfirmMountedTipsOperation(runtime).run(
            ConfirmMountedTipsRequest(device_name="flex_1"),
        )
    _assert_typed_422(exc_info.value, wire_code, {**extras, "device_name": "flex_1"})


@pytest.mark.asyncio
async def test_any_other_value_error_is_still_invalid_input() -> None:
    runtime = MagicMock()
    runtime.labware.confirm_tip_state = AsyncMock(side_effect=ValueError("not a rack"))
    with pytest.raises(OperationError) as exc_info:
        await ConfirmTipStateOperation(runtime).run(ConfirmTipStateRequest(labware_id="lw-1"))
    assert exc_info.value.code is OperationErrorCode.INVALID_INPUT
    assert exc_info.value.wire_code is None

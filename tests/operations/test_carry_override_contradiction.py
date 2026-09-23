"""What a caller is told when one field is named in both halves of an edit.

The facade owns the refusal itself and every surface shares it. What this layer
adds is the list of fields that clash, which is what lets a caller fix the
request in one go instead of bisecting it.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from cheshire_drivers.move_parameters import MoveParameterField, MoveParameterPatch

from orca.operations._protocol import OperationError, OperationErrorCode
from orca.operations.labware import SetLabwareCarryOverrideOperation
from orca.operations.labware_models import SetLabwareCarryOverrideRequest
from orca.runtime.move_parameters import reject_contradiction


class _LabwareApplyingTheRule:
    """The real rule, so this pins the translation and not a mock's opinion.

    It refuses an unconfirmed call the way the real facade does: that method is
    PHYSICAL-danger, so a stub that shrugged the flag off would leave every
    write failing on hardware while the suite stayed green.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def set_carry_override(
        self, labware_id: str, patch: MoveParameterPatch,
        clear: tuple[MoveParameterField, ...] = (),
        confirm: bool = False,
    ) -> MoveParameterPatch:
        self.calls += 1
        assert confirm, "the real facade is @dangerous and refuses without this"
        reject_contradiction(patch, clear)
        return patch


def _runtime(labware: _LabwareApplyingTheRule) -> MagicMock:
    runtime = MagicMock()
    runtime.labware = labware
    return runtime


async def _refused(request: SetLabwareCarryOverrideRequest) -> OperationError:
    operation = SetLabwareCarryOverrideOperation(
        runtime=_runtime(_LabwareApplyingTheRule()),
    )
    with pytest.raises(OperationError) as exc_info:
        await operation.run(request)
    return exc_info.value


async def test_a_field_cannot_be_given_a_value_and_handed_back_at_once() -> None:
    """Clearing used to win and the value was dropped without a word, so the
    caller was told the write succeeded and read back the opposite."""
    raised = await _refused(SetLabwareCarryOverrideRequest(
        labware_id="lw1",
        set=MoveParameterPatch(z_offset=3.0),
        clear=["z_offset"],
    ))

    assert raised.code is OperationErrorCode.INVALID_INPUT
    assert "z_offset" in raised.message


async def test_the_refusal_names_every_field_in_both_halves() -> None:
    """A message naming one of three sends the caller round again twice."""
    raised = await _refused(SetLabwareCarryOverrideRequest(
        labware_id="lw1",
        set=MoveParameterPatch(z_offset=3.0, speed=50.0, clearance=2.0),
        clear=["z_offset", "speed"],
    ))

    assert raised.extras is not None
    assert raised.extras["fields"] == ["speed", "z_offset"]
    assert "clearance" not in raised.message, "only the contested ones"


async def test_the_two_halves_still_travel_together_when_they_agree() -> None:
    """The refusal is about one field named twice, not about using both halves:
    setting some fields while handing others back is the normal write."""
    labware = _LabwareApplyingTheRule()
    operation = SetLabwareCarryOverrideOperation(runtime=_runtime(labware))

    await operation.run(SetLabwareCarryOverrideRequest(
        labware_id="lw1",
        set=MoveParameterPatch(z_offset=3.0),
        clear=["speed"],
    ))

    assert labware.calls == 1


async def test_an_unknown_plate_is_still_a_not_found_rather_than_bad_input() -> None:
    """Both land on the same call now, so the one that is the caller's fault
    has to stay distinguishable from the one that is not."""
    runtime = MagicMock()
    runtime.labware = MagicMock()
    runtime.labware.set_carry_override = AsyncMock(side_effect=KeyError("nope"))
    operation = SetLabwareCarryOverrideOperation(runtime=runtime)

    with pytest.raises(OperationError) as exc_info:
        await operation.run(SetLabwareCarryOverrideRequest(
            labware_id="nope", set=MoveParameterPatch(z_offset=1.0), clear=[],
        ))

    assert exc_info.value.code is OperationErrorCode.NOT_FOUND

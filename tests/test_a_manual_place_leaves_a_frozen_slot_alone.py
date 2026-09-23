"""A manual place never produces a second instance, so a frozen slot is never
asked to hold a different one.

The bench failure this replaces: a rack thread parked waiting for the operator
while the action that wanted it resolved its location and FROZE its slots onto
the instance the thread was holding. Registering the operator's labware used to
mint a second instance, and the frozen slot then refused the swap:

    input slot 'r4_tips' already bound to r4_tips-34cd58ec; refusing to
    overwrite with r4_tips-c5a7d1b9 after assignment was frozen

`labware_register` now adopts the expectation the thread already holds, so the
instance in the frozen slot is the instance that arrives. The freeze needs no
exemption, which is the point: the guard is back to catching only genuinely
different labware.
"""

import pytest

pytestmark = pytest.mark.asyncio

from orca.resource_models.labware import LabwareTemplate
from orca.runtime.sim_labware import SimPlateTemplate
from orca.workflow_models.actions.util import (
    AssignedLabwareManager,
    DoubleAssignmentError,
)

from tests.test_helpers import create_test_labware_instance


def _slot_and_manager() -> tuple[LabwareTemplate, AssignedLabwareManager]:
    slot = SimPlateTemplate("r4_tips")
    return slot, AssignedLabwareManager([slot], [slot])


async def test_rebinding_a_frozen_slot_to_the_same_instance_is_not_a_conflict() -> None:
    """What the manual-place path now does: the labware that arrives IS the
    labware the slot froze onto, so a later bind is a no-op, not a refusal."""
    labware = await create_test_labware_instance("r4_tips")
    slot, manager = _slot_and_manager()
    manager.assign_input(slot, labware)
    manager.freeze()

    manager.assign_input(slot, labware)

    assert manager.expected_inputs == [labware]
    assert manager.expected_outputs == [labware]


async def test_an_ordinary_double_assignment_is_still_refused() -> None:
    """The guard has no exemption left to slip through."""
    first = await create_test_labware_instance("r4_tips")
    second = await create_test_labware_instance("r4_tips")
    slot, manager = _slot_and_manager()
    manager.assign_input(slot, first)
    manager.freeze()

    with pytest.raises(DoubleAssignmentError):
        manager.assign_input(slot, second)


async def test_a_frozen_output_slot_refuses_a_different_instance_too() -> None:
    """Outputs froze alongside inputs and had the same exemption; they must
    refuse for the same reason now that neither does."""
    first = await create_test_labware_instance("r4_tips")
    second = await create_test_labware_instance("r4_tips")
    slot, manager = _slot_and_manager()
    manager.assign_output(slot, first)
    manager.freeze()

    with pytest.raises(DoubleAssignmentError):
        manager.assign_output(slot, second)

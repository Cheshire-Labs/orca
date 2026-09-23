"""ThreadTemplate end-side leave-in-place flag.

`@orca.thread(end=("loc", LEAVE_IN_PLACE))` marks a thread whose
labware should NOT be auto-disposed when the thread terminates. Used
for deck-resident reagent troughs and calibration plates that must
outlive a single execution so the next reuse-bind binding can find
them.

Default behavior (bare string / Location form) is unchanged: dispose
at thread completion.
"""

from collections.abc import AsyncGenerator

import pytest

from orca.spawn import LEAVE_IN_PLACE
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.thread_template import ThreadFunc, ThreadTemplate
from tests.test_helpers import create_test_plate_template


def _trivial_func() -> ThreadFunc:
    async def fn(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        del ctx
        if False:
            yield
    return fn


class TestEndLeaveInPlaceFlag:

    def test_thread_template_default_end_leave_in_place_is_false(self) -> None:
        plate = create_test_plate_template("plate_x")
        t = ThreadTemplate(
            labware_template=plate, start="pad_1", end="pad_1",
            func=_trivial_func(),
        )
        assert t.end_leave_in_place is False

    def test_thread_template_tuple_form_sets_leave_in_place(self) -> None:
        plate = create_test_plate_template("plate_x")
        t = ThreadTemplate(
            labware_template=plate,
            start="pad_1",
            end=("pad_1", LEAVE_IN_PLACE),
            func=_trivial_func(),
        )
        assert t.end_leave_in_place is True
        assert t.end_position_ids == ["pad_1"]

    def test_thread_template_invalid_end_sentinel_raises(self) -> None:
        plate = create_test_plate_template("plate_x")
        with pytest.raises(ValueError, match="sentinel"):
            ThreadTemplate(
                labware_template=plate,
                start="pad_1",
                end=("pad_1", "not_a_real_sentinel"),
                func=_trivial_func(),
            )

    def test_thread_template_malformed_end_tuple_raises(self) -> None:
        plate = create_test_plate_template("plate_x")
        with pytest.raises(ValueError, match="must be"):
            ThreadTemplate(
                labware_template=plate,
                start="pad_1",
                end=("pad_1",),
                func=_trivial_func(),
            )

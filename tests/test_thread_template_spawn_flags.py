"""ThreadTemplate spawn-flag normalization.

`@orca.thread(start=...)` accepts three forms:
  - bare string  ("pad_1") -- defaults reuse_existing=False
  - Location object -- defaults reuse_existing=False
  - tuple (loc, REUSE_EXISTING) -- reuse_existing=True

Tuple form's second element must match a known sentinel; invalid values
raise ValueError at construction.

The `end=` tuple form is reserved for LEAVE_IN_PLACE and currently raises
ValueError to prevent silent acceptance.
"""

from collections.abc import AsyncGenerator

import pytest

from orca.spawn import REUSE_EXISTING, LEAVE_IN_PLACE
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.thread_template import ThreadFunc, ThreadTemplate
from tests.test_helpers import create_test_plate_template


def _trivial_func() -> ThreadFunc:
    async def fn(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        del ctx
        return
        yield
    return fn


class TestThreadTemplateSpawnFlags:

    def test_thread_template_default_flag_is_false(self) -> None:
        plate = create_test_plate_template("plate_x")
        t = ThreadTemplate(
            labware_template=plate, start="pad_1", end="pad_1",
            func=_trivial_func(),
        )
        assert t.start_reuse_existing is False

    def test_thread_template_tuple_form_sets_reuse_existing(self) -> None:
        plate = create_test_plate_template("plate_x")
        t = ThreadTemplate(
            labware_template=plate,
            start=("pad_1", REUSE_EXISTING),
            end="pad_1",
            func=_trivial_func(),
        )
        assert t.start_reuse_existing is True
        # Location string is unwrapped from the tuple.
        assert t.start_position_id == "pad_1"

    def test_thread_template_bare_string_backward_compat(self) -> None:
        plate = create_test_plate_template("plate_x")
        t = ThreadTemplate(
            labware_template=plate, start="pad_1", end="pad_2",
            func=_trivial_func(),
        )
        assert t.start_position_id == "pad_1"
        assert t.end_position_ids == ["pad_2"]
        assert t.start_reuse_existing is False

    def test_thread_template_invalid_start_sentinel_raises(self) -> None:
        plate = create_test_plate_template("plate_x")
        with pytest.raises(ValueError, match="sentinel"):
            ThreadTemplate(
                labware_template=plate,
                start=("pad_1", "not_a_real_sentinel"),
                end="pad_1",
                func=_trivial_func(),
            )

    def test_thread_template_malformed_start_tuple_raises(self) -> None:
        plate = create_test_plate_template("plate_x")
        with pytest.raises(ValueError, match="must be"):
            ThreadTemplate(
                labware_template=plate,
                start=("pad_1",),  # missing sentinel
                end="pad_1",
                func=_trivial_func(),
            )

    def test_thread_template_end_tuple_form_accepted(self) -> None:
        """The `end=` tuple form normalizes to (location, leave_in_place=True)."""
        plate = create_test_plate_template("plate_x")
        t = ThreadTemplate(
            labware_template=plate,
            start="pad_1",
            end=("pad_1", LEAVE_IN_PLACE),
            func=_trivial_func(),
        )
        assert t.end_leave_in_place is True

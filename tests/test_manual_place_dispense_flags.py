"""ThreadTemplate spawn-mode flag normalization.

Three new sentinels make `@orca.thread(start=...)` and `end=...` explicit:

- `DISPENSE` (start-side) -- author intent: stacker / hotel / source dispense
  at workflow entry; routes to `DispenseSpawn` (renamed `FromSourceSpawn`).
- `MANUAL_PLACE` (start-side) -- author intent: operator places labware at
  the slot; routes to `ManualPlaceSpawn`. Bare-string default.
- `MANUAL_REMOVE` (end-side) -- author intent: operator removes labware
  from the slot at thread completion; routes to `ManualRemoveSpawn`.
  Bare-string default.

`REUSE_EXISTING` and `LEAVE_IN_PLACE` keep their existing semantics; the
inline reuse-bind path and the leave-in-place skip in
`_handle_thread_completion` remain unchanged.

The bare-string defaults shift semantics in v3: today's bare-string maps
to the auto-dispatch `select_spawn_action(location)` (IPlateSource-vs-
PlatePad). Under v3, bare-string maps to ManualPlace/ManualRemove and
the engine dispatches by template flag, not by resource shape. Sim
modes (PURE_SIM/DEVICE_SIM) auto-fulfill so the test corpus stays
green; LIVE mode parks the thread at AWAITING_MANUAL_* until the
operator calls `labware_register` / `labware_discharge`.
"""

from collections.abc import AsyncGenerator

import pytest

from orca.spawn import (
    DISPENSE,
    LEAVE_IN_PLACE,
    MANUAL_PLACE,
    MANUAL_REMOVE,
    REUSE_EXISTING,
)
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


class TestSentinelConstants:

    def test_dispense_sentinel_is_distinct_string(self) -> None:
        assert isinstance(DISPENSE, str)
        assert DISPENSE != REUSE_EXISTING
        assert DISPENSE != MANUAL_PLACE

    def test_manual_place_sentinel_is_distinct_string(self) -> None:
        assert isinstance(MANUAL_PLACE, str)
        assert MANUAL_PLACE != REUSE_EXISTING
        assert MANUAL_PLACE != DISPENSE

    def test_manual_remove_sentinel_is_distinct_string(self) -> None:
        assert isinstance(MANUAL_REMOVE, str)
        assert MANUAL_REMOVE != LEAVE_IN_PLACE


class TestThreadTemplateStartFlags:

    def test_bare_string_start_sets_no_start_flag_at_all(self) -> None:
        """A hand-placed start is the absence of the other two, not a flag.

        `select_spawn_action` reads `start_dispense`, `_resolve_reuse_bind`
        reads `start_reuse_existing`, and everything else falls through to
        `ManualPlaceSpawn`. So "this is a manual place" is exactly "neither of
        those is set".
        """
        plate = create_test_plate_template("plate_x")
        t = ThreadTemplate(
            labware_template=plate, start="pad_1", end="pad_1",
            func=_trivial_func(),
        )
        assert t.start_dispense is False
        assert t.start_reuse_existing is False

    def test_tuple_form_dispense_sets_dispense_flag_only(self) -> None:
        plate = create_test_plate_template("plate_x")
        t = ThreadTemplate(
            labware_template=plate,
            start=("stacker_1", DISPENSE),
            end="pad_1",
            func=_trivial_func(),
        )
        assert t.start_dispense is True
        assert t.start_reuse_existing is False
        assert t.start_position_id == "stacker_1"

    def test_tuple_form_explicit_manual_place_sets_manual_place_only(self) -> None:
        plate = create_test_plate_template("plate_x")
        t = ThreadTemplate(
            labware_template=plate,
            start=("pad_1", MANUAL_PLACE),
            end="pad_1",
            func=_trivial_func(),
        )
        assert t.start_dispense is False
        assert t.start_reuse_existing is False

    def test_explicit_sentinel_and_bare_string_produce_the_same_flags(self) -> None:
        """Naming MANUAL_PLACE is a spelling, not a different thread: nothing
        downstream can tell the two starts apart."""
        plate = create_test_plate_template("plate_x")
        explicit = ThreadTemplate(
            labware_template=plate, start=("pad_1", MANUAL_PLACE), end="pad_1",
            func=_trivial_func(),
        )
        bare = ThreadTemplate(
            labware_template=plate, start="pad_1", end="pad_1",
            func=_trivial_func(),
        )
        assert explicit.start_dispense == bare.start_dispense
        assert explicit.start_reuse_existing == bare.start_reuse_existing
        assert explicit.start_position_id == bare.start_position_id

    def test_tuple_form_reuse_existing_sets_reuse_only(self) -> None:
        plate = create_test_plate_template("plate_x")
        t = ThreadTemplate(
            labware_template=plate,
            start=("trough", REUSE_EXISTING),
            end=("trough", LEAVE_IN_PLACE),
            func=_trivial_func(),
        )
        assert t.start_reuse_existing is True
        assert t.start_dispense is False


class TestThreadTemplateEndFlags:

    def test_bare_string_end_defaults_to_manual_remove(self) -> None:
        plate = create_test_plate_template("plate_x")
        t = ThreadTemplate(
            labware_template=plate, start="pad_1", end="pad_2",
            func=_trivial_func(),
        )
        assert t.end_manual_remove is True
        assert t.end_leave_in_place is False

    def test_tuple_form_explicit_manual_remove_sets_manual_remove_only(self) -> None:
        plate = create_test_plate_template("plate_x")
        t = ThreadTemplate(
            labware_template=plate,
            start="pad_1",
            end=("pad_2", MANUAL_REMOVE),
            func=_trivial_func(),
        )
        assert t.end_manual_remove is True
        assert t.end_leave_in_place is False

    def test_tuple_form_leave_in_place_clears_manual_remove(self) -> None:
        plate = create_test_plate_template("plate_x")
        t = ThreadTemplate(
            labware_template=plate,
            start=("trough", REUSE_EXISTING),
            end=("trough", LEAVE_IN_PLACE),
            func=_trivial_func(),
        )
        assert t.end_leave_in_place is True
        assert t.end_manual_remove is False


class TestSentinelValidation:

    def test_dispense_in_end_position_raises(self) -> None:
        plate = create_test_plate_template("plate_x")
        with pytest.raises(ValueError, match="sentinel"):
            ThreadTemplate(
                labware_template=plate,
                start="pad_1",
                end=("pad_2", DISPENSE),
                func=_trivial_func(),
            )

    def test_manual_remove_in_start_position_raises(self) -> None:
        plate = create_test_plate_template("plate_x")
        with pytest.raises(ValueError, match="sentinel"):
            ThreadTemplate(
                labware_template=plate,
                start=("pad_1", MANUAL_REMOVE),
                end="pad_2",
                func=_trivial_func(),
            )

    def test_leave_in_place_in_start_position_raises(self) -> None:
        plate = create_test_plate_template("plate_x")
        with pytest.raises(ValueError, match="sentinel"):
            ThreadTemplate(
                labware_template=plate,
                start=("pad_1", LEAVE_IN_PLACE),
                end="pad_2",
                func=_trivial_func(),
            )

    def test_reuse_existing_in_end_position_raises(self) -> None:
        plate = create_test_plate_template("plate_x")
        with pytest.raises(ValueError, match="sentinel"):
            ThreadTemplate(
                labware_template=plate,
                start="pad_1",
                end=("pad_2", REUSE_EXISTING),
                func=_trivial_func(),
            )

    def test_manual_place_in_end_position_raises(self) -> None:
        plate = create_test_plate_template("plate_x")
        with pytest.raises(ValueError, match="sentinel"):
            ThreadTemplate(
                labware_template=plate,
                start="pad_1",
                end=("pad_2", MANUAL_PLACE),
                func=_trivial_func(),
            )

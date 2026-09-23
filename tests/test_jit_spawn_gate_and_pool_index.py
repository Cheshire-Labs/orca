"""Guards for the JIT spawn (spawn-after-reserve): contributors now spawn after
the owner reserves the device, so the presence gate must tolerate not-yet-assigned
inputs, and the contribution-index dict must be observed by reference (the spawn
sets it after the action resolves, not before)."""

import asyncio

import pytest

from orca.variables.variable_store import NullVariableResolver
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.actions.util import AssignedLabwareManager
from orca.workflow_models.device_handle import ActionRequest

from tests.test_helpers import create_test_labware_instance, create_test_plate_template


class TestAssignedLabwareManagerReadiness:
    """The presence gate reads assignment through these non-raising accessors so a
    contributor reaching the gate mid-spawn (siblings unbound) waits, not crashes."""

    @pytest.mark.asyncio
    async def test_readiness_tracks_progressive_binding(self) -> None:
        slot_a = create_test_plate_template("plate_a")
        slot_b = create_test_plate_template("plate_b")
        # Each input passes through as an output too, so ``assign_input`` (which
        # mirrors to the output slot) needs both templates declared on both sides.
        manager = AssignedLabwareManager([slot_a, slot_b], [slot_a, slot_b])

        assert not manager.all_inputs_assigned
        assert manager.assigned_inputs == []
        assert set(manager.unassigned_input_slot_names) == {"plate_a", "plate_b"}

        first = await create_test_labware_instance("plate_a")
        manager.assign_input(slot_a, first)
        assert not manager.all_inputs_assigned
        assert manager.assigned_inputs == [first]
        assert manager.unassigned_input_slot_names == ["plate_b"]

        second = await create_test_labware_instance("plate_b")
        manager.assign_input(slot_b, second)
        assert manager.all_inputs_assigned
        assert manager.unassigned_input_slot_names == []


class TestContributionIndexByReference:
    """The spawn sets the index AFTER the owner resolves the action, so the
    context must share the method's dict by reference. ``or {}`` forked an empty
    copy at resolve time and lost the later write, crashing the pooling action."""

    def _make_ctx(self, indices: dict[str, int] | None) -> ActionContext:
        queue: asyncio.Queue[ActionRequest | None] = asyncio.Queue()
        return ActionContext(
            device_name="mlstar_2",
            action_queue=queue,
            assigned_labware={},
            variable_store=NullVariableResolver(),
            execution_id="test-exec-ref",
            pool_indices=indices,
        )

    def test_index_set_after_construction_is_visible(self) -> None:
        indices: dict[str, int] = {}
        ctx = self._make_ctx(indices)  # built empty, as at resolve time
        indices["final_plate"] = 2  # the spawn sets it post-resolve
        assert ctx.pool_index("final_plate") == 2

    def test_unknown_receiver_still_raises(self) -> None:
        ctx = self._make_ctx({})
        with pytest.raises(ValueError, match="does not contribute"):
            ctx.pool_index("final_plate")

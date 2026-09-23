"""2D lifecycle stages: Unresolved -> Assigned -> Executable + double-assign guard.

Pins the explicit action lifecycle:
- ``UnresolvedLocationAction.assign()`` is idempotent (one AssignedLocationAction
  per unresolved lifetime) and freezes the labware manager onto the action.
- ``AssignedLocationAction.executable(...)`` mints a FRESH ExecutableLocationAction
  per call -- the retry seam (one assigned, N executables).
- ``AssignedLabwareManager.assign_input`` / ``assign_output`` raise
  ``DoubleAssignmentError`` on a conflicting overwrite (W1/F27) instead of
  silently clobbering, while tolerating idempotent same-instance re-assign.
- ``MoveAction.executable(...)`` factory parallels the location lifecycle (W2):
  the move path no longer ``new``-constructs ExecutableMoveAction inline.
"""

from unittest.mock import Mock

import pytest

from orca.events.execution_context import MethodExecutionContext
from orca.resource_models.labware import LabwareInstance
from orca.variables.variable_store import NullVariableResolver
from orca.workflow_models.actions.assigned_location_action import AssignedLocationAction
from orca.workflow_models.actions.dynamic_resource_action import UnresolvedLocationAction
from orca.workflow_models.actions.executable_location_action import ExecutableLocationAction
from orca.workflow_models.actions.location_action import ActionBodyLocationAction
from orca.workflow_models.actions.move_action import ExecutableMoveAction, MoveAction
from orca.workflow_models.actions.util import (
    AssignedLabwareManager,
    DoubleAssignmentError,
)
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method import MethodInstance

from tests.test_helpers import (
    no_source_hold,
    create_test_device,
    create_test_labware_instance,
    create_test_plate_template,
    create_test_transporter,
)


async def _noop_action_body(ctx: ActionContext) -> None:
    return None


def _make_unresolved() -> UnresolvedLocationAction:
    device = create_test_device("shaker1")
    body = ActionBodyLocationAction(func=_noop_action_body, command="shake")
    return UnresolvedLocationAction(
        resource=device,
        location_action=body,
        expected_input_templates=[],
        expected_output_templates=[],
    )


def _make_context() -> MethodExecutionContext:
    return MethodExecutionContext(
        execution_id="exec-1",
        workflow_name="wf",
        method_id="m1",
        method_name="method",
        thread_id="t1",
        thread_name="thread1",
        participating_thread_ids=("t1",),
    )


class TestDoubleAssignmentError:
    async def test_conflicting_input_reassign_after_freeze_raises(self) -> None:
        template = create_test_plate_template("plate")
        manager = AssignedLabwareManager([template], [template])
        first = await create_test_labware_instance("plate")
        second = await create_test_labware_instance("plate")
        manager.assign_input(template, first)
        manager.freeze()

        with pytest.raises(DoubleAssignmentError):
            manager.assign_input(template, second)

    async def test_prefreeze_conflicting_overwrite_is_allowed(self) -> None:
        # The freeze is what makes a re-assignment a conflict. Before it, a slot
        # is still being worked out and pointing it at different labware is
        # ordinary -- an acquisition resolving, a spawn binding what it found.
        template = create_test_plate_template("plate")
        manager = AssignedLabwareManager([template], [template])
        first = await create_test_labware_instance("plate")
        second = await create_test_labware_instance("plate")
        manager.assign_input(template, first)
        manager.assign_input(template, second)  # pre-freeze, allowed
        assert manager.expected_inputs == [second]

    async def test_idempotent_same_instance_reassign_allowed_after_freeze(self) -> None:
        template = create_test_plate_template("plate")
        manager = AssignedLabwareManager([template], [template])
        instance = await create_test_labware_instance("plate")
        manager.assign_input(template, instance)
        manager.freeze()
        # Same instance again: harmless, must NOT raise even when frozen.
        manager.assign_input(template, instance)
        assert manager.expected_inputs == [instance]

    async def test_conflicting_output_reassign_after_freeze_raises(self) -> None:
        template = create_test_plate_template("plate")
        manager = AssignedLabwareManager([template], [template])
        first = await create_test_labware_instance("plate")
        second = await create_test_labware_instance("plate")
        manager.assign_output(template, first)
        manager.freeze()

        with pytest.raises(DoubleAssignmentError):
            manager.assign_output(template, second)


class _FakeLabwareThread:
    """Minimal IHasLabware for assign_thread: just carries a labware instance."""
    def __init__(self, labware: LabwareInstance) -> None:
        self._labware = labware

    @property
    def labware(self) -> LabwareInstance:
        return self._labware


class TestAssignThreadOverwritesBeforeFreeze:
    """A blanket double-assign guard would refuse a second ``assign_thread``
    walk that lands before the action resolves, and threads legitimately bind
    twice while they are still working out what labware they carry. Exercises
    the whole chain: MethodInstance.assign_thread -> try_assign_labware ->
    AssignedLabwareManager.assign_input."""

    async def test_a_second_walk_overwrites_the_slot_before_freeze(self) -> None:
        template = create_test_plate_template("plate")
        device = create_test_device("shaker1")
        body = ActionBodyLocationAction(func=_noop_action_body, command="shake")
        unresolved = UnresolvedLocationAction(
            resource=device,
            location_action=body,
            expected_input_templates=[template],
            expected_output_templates=[template],
        )
        method = MethodInstance(name="m")
        method.append_action(unresolved)

        first = await create_test_labware_instance("plate")
        second = await create_test_labware_instance("plate")
        assert first is not second

        method.assign_thread(template, _FakeLabwareThread(first))
        # Allowed because resolution has not frozen the manager yet.
        method.assign_thread(template, _FakeLabwareThread(second))

        assert unresolved.expected_inputs == [second]


class TestAssignFactory:
    def test_assign_is_idempotent(self) -> None:
        unresolved = _make_unresolved()
        first = unresolved.assign()
        second = unresolved.assign()
        assert first is second
        assert isinstance(first, AssignedLocationAction)

    def test_assign_freezes_manager_onto_action(self) -> None:
        unresolved = _make_unresolved()
        assigned = unresolved.assign()
        # The frozen handoff exposes the same underlying action, now carrying
        # the manager the unresolved owned.
        assert isinstance(assigned.location_action, ActionBodyLocationAction)
        assert assigned.location_action.assigned_labware_manager is not None


class TestExecutableFactory:
    def test_executable_is_fresh_per_call(self) -> None:
        unresolved = _make_unresolved()
        assigned = unresolved.assign()
        status_manager = Mock()

        first = assigned.executable(
            status_manager, _make_context(), NullVariableResolver(), None, None,
        )
        second = assigned.executable(
            status_manager, _make_context(), NullVariableResolver(), None, None,
        )

        assert isinstance(first, ExecutableLocationAction)
        assert first is not second  # fresh per attempt (retry seam)
        assert first.action is second.action  # same underlying action


class TestMoveExecutableFactory:
    async def test_move_executable_wraps_the_move(self) -> None:
        labware = await create_test_labware_instance("plate_1")
        transporter = create_test_transporter("robot1", ["a", "b"])
        from orca.resource_models.location import Location
        from orca.resource_models.plate_pad import PlatePad
        source = Location("a", resource=PlatePad("pad_a"))
        target = Location("b", resource=PlatePad("pad_b"))
        move = MoveAction(labware, source, target, transporter)

        context = Mock()
        context.execution_id = "exec-1"
        context.workflow_name = "wf"
        context.thread_id = "t1"
        context.thread_name = "thread1"
        context.template_name = "tmpl"

        executable = move.executable(
            Mock(), context, Mock(), Mock(), no_source_hold(),
        )

        assert isinstance(executable, ExecutableMoveAction)
        assert executable.id == move.id

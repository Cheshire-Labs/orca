"""Every operation record an action writes names the labware group the action serves."""

from unittest.mock import MagicMock

import pytest

from orca.events.execution_context import MethodExecutionContext, WorkflowExecutionContext
from orca.plugins.declared_tracking_observer import DeclaredTrackingObserver
from orca.resource_models.tracking_context import TrackingContext
from orca.state.ops_history import OpsHistory
from orca.state.records import DeclaredTracking, DeclaredVolumeTransfer, OperationRecord
from orca.workflow_models.actions.executable_location_action import ExecutableLocationAction
from orca.workflow_models.method import ExecutingMethod, group_served_by
from tests.memory_activity import memory_ops_store


def _action(declares: DeclaredTracking | None) -> MagicMock:
    action = MagicMock()
    action.id = "a1"
    action.command = "transfer"
    action.device.name = "lh"
    action.declares = declares
    action.build_template_to_instance_map.return_value = {}
    return action


async def _ops_written(group_id: str | None, declares: DeclaredTracking) -> list[OperationRecord]:
    history = OpsHistory(store=memory_ops_store(), execution_id="e1")
    context = MethodExecutionContext(
        execution_id="e1", workflow_name="wf", method_id="m1", method_name="m",
        thread_id="t1", thread_name="plate_1", participating_thread_ids=("t1",),
    )
    executable = ExecutableLocationAction(
        MagicMock(), _action(declares), context,
        tracking_context=TrackingContext(observer=DeclaredTrackingObserver(), ops_history=history),
        group_served=lambda: group_id,
    )
    await executable._save_declared_tracking()
    await executable.record_operator_continued()
    return [op for record in await history.records() for op in record.operations]


@pytest.mark.asyncio
@pytest.mark.parametrize("group_id", ["g1", None])
async def test_declared_and_operator_records_carry_the_actions_group(group_id: str | None) -> None:
    declares = DeclaredTracking(volume_transferred=[DeclaredVolumeTransfer(
        source="reagent", target="sample", volume_ul=5.0, source_wells=["A1"], target_wells=["A1"],
    )], wells_used={"sample": ["A1"]})

    ops = await _ops_written(group_id, declares)

    assert len(ops) == 4
    assert [op.group_id for op in ops] == [group_id] * 4


@pytest.mark.parametrize(
    ("groups", "served"),
    [
        (["g1"], "g1"),
        (["g1", None, "g1"], "g1"),
        ([None], None),
        (["g1", "g2"], None),
    ],
)
def test_an_action_serves_the_one_group_among_its_threads(groups: list[str | None], served: str | None) -> None:
    assert group_served_by(groups) == served


def test_a_thread_that_joins_after_the_action_binds_counts_toward_its_group() -> None:
    template = MagicMock(id="m1")
    template.name = "add_method"
    method = ExecutingMethod(template, MagicMock(), MagicMock(), WorkflowExecutionContext(execution_id="e1", workflow_name="wf"))
    assigned = MagicMock()
    method.set_current_thread("final", "final_1", group_id=None)
    method._create_executable_action(assigned)
    group_served = assigned.executable.call_args.args[7]
    assert group_served() is None

    method.set_current_thread("sample", "sample_1", group_id="g1")
    assert group_served() == "g1"

    method.set_current_thread("other", "sample_2", group_id="g2")
    assert group_served() is None

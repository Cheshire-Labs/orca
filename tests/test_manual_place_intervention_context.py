"""AWAITING_MANUAL_PLACE / AWAITING_MANUAL_REMOVE status events carry
the labware identity and target slot so an operator-notification waiter
can act without a second lookup.

The status transition already reached the SystemEventBus; what was missing
was a payload. These tests pin that the published context for those two
statuses is a ManualInterventionContext with labware_id / labware_name /
target_location, and that ordinary statuses stay a plain
ThreadExecutionContext (no enrichment, no behavior change).
"""

from unittest.mock import MagicMock

from orca.events.execution_context import (
    ExecutionContext,
    ManualInterventionContext,
    ThreadExecutionContext,
)
from orca.events.intervention import InterventionKind, classify_intervention
from orca.events.runtime_event import RuntimeEvent
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.runtime.run_modes import WorkflowRunMode
from orca.workflow_models.labware_threads.executing_labware_thread import (
    ExecutingLabwareThread,
)
from orca.workflow_models.labware_threads.labware_thread import (
    LabwareThreadInstance,
)
from orca.workflow_models.status_enums import LabwareThreadStatus

Captured = list[tuple[str, str, ExecutionContext]]


def _build_thread() -> tuple[ExecutingLabwareThread, Captured]:
    start = Location("start_pad", resource=PlatePad("start_pad"))
    end = Location("end_pad", resource=PlatePad("end_pad"))
    labware = LabwareInstance("plate_96", "96_well")
    thread = LabwareThreadInstance(
        labware=labware,
        start_location=start,
        end_locations=[end],
        run_mode=WorkflowRunMode.LIVE,
    )

    captured: Captured = []
    status_manager = MagicMock()
    status_manager.get_status = MagicMock(side_effect=KeyError)

    def _set_status(kind: str, _id: str, name: str, ctx: ExecutionContext) -> None:
        captured.append((kind, name, ctx))

    status_manager.set_status = MagicMock(side_effect=_set_status)

    context = MagicMock()
    context.execution_id = "exec-1"
    context.workflow_name = "wf"

    loc_service = MagicMock()
    loc_service.get_history.return_value = MagicMock()
    # AWAITING_MANUAL_REMOVE reports the slot the labware rests at.
    loc_service.get.return_value = end

    et = ExecutingLabwareThread(
        thread=thread,
        event_bus=MagicMock(),
        move_handler=MagicMock(),
        status_manager=status_manager,
        actions_resolver=MagicMock(),
        context=context,
        labware_location_service=loc_service,
    )
    return et, captured


def test_manual_place_status_publishes_enriched_context() -> None:
    et, captured = _build_thread()
    et._publish_status_to_status_manager(LabwareThreadStatus.AWAITING_MANUAL_PLACE)

    _kind, name, ctx = captured[-1]
    assert name == "AWAITING_MANUAL_PLACE"
    assert isinstance(ctx, ManualInterventionContext)
    assert ctx.labware_id == et.labware.id
    assert ctx.labware_name == et.labware.name
    assert ctx.labware_name is not None and ctx.labware_name.startswith("plate_96")
    expected_template = (
        et._thread.labware_template.name
        if et._thread.labware_template is not None
        else None
    )
    assert ctx.labware_template_name == expected_template
    assert ctx.target_location == "start_pad"

    event = RuntimeEvent.from_event_bus(
        f"THREAD.{et.labware.id}.AWAITING_MANUAL_PLACE", "exec-1", ctx,
    )
    assert classify_intervention(event) is InterventionKind.MANUAL_PLACE


def test_manual_remove_status_targets_end_location() -> None:
    et, captured = _build_thread()
    et._publish_status_to_status_manager(LabwareThreadStatus.AWAITING_MANUAL_REMOVE)

    _kind, name, ctx = captured[-1]
    assert name == "AWAITING_MANUAL_REMOVE"
    assert isinstance(ctx, ManualInterventionContext)
    assert ctx.target_location == "end_pad"


def test_ordinary_status_publishes_plain_thread_context() -> None:
    et, captured = _build_thread()
    et._publish_status_to_status_manager(LabwareThreadStatus.MOVING)

    _kind, name, ctx = captured[-1]
    assert name == "MOVING"
    assert isinstance(ctx, ThreadExecutionContext)
    assert not isinstance(ctx, ManualInterventionContext)

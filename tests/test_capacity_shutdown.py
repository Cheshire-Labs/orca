"""Tests for ExecutingWorkflow.cleanup_parked_threads() and shutdown signals.

When a workflow shuts down with items still stranded in slot.pending or
slot.queue (under-fill: declared N contributors, only M<N arrived), the
runtime must:
1. Emit SLOT.{slot_key}.ABANDONED for observability.
2. Log a WARNING with the slot key + pending/queue counts for CLI diagnosis.

Covers:
- T9: ABANDONED event emitted for non-empty slots
- T10: WARNING log includes slot key and item counts
- Bonus: empty slots are NOT abandoned (no event, no log)
"""
import logging
from unittest.mock import MagicMock

import pytest

from orca.events.event_bus import EventBus
from orca.events.execution_context import ExecutionContext
from orca.resource_models.labware_state import InMemoryLabwareRegistry
from orca.workflow_models.method import ExecutingMethod
from orca.workflow_models.workflows.executing_workflow import ExecutingWorkflow
from orca.workflow_models.workflows.workflow import WorkflowInstance
from orca.workflow_models.workflow_templates import WorkflowTemplate


def _build_workflow_with_registry() -> tuple[ExecutingWorkflow, InMemoryLabwareRegistry, EventBus]:
    template = WorkflowTemplate(name="shutdown_test_wf")
    workflow_instance = WorkflowInstance(name="shutdown_test_wf", template=template)
    registry = InMemoryLabwareRegistry()
    event_bus = EventBus()

    workflow = ExecutingWorkflow(
        workflow=workflow_instance,
        thread_reservation_coordinator=MagicMock(),
        system_thread_manager=MagicMock(),
        event_bus=event_bus,
        move_handler=MagicMock(),
        status_manager=MagicMock(),
        system_map=MagicMock(),
        create_thread_fn=MagicMock(),
        labware_registry=registry,
    )
    return workflow, registry, event_bus


def _make_method(name: str) -> ExecutingMethod:
    method = MagicMock(spec=ExecutingMethod)
    method.name = name
    return method


class TestCleanupAbandonedEvent:

    def test_emits_slot_abandoned_when_pending_nonempty(self) -> None:
        """T9 (pending case): items in slot.pending trigger ABANDONED + log."""
        workflow, registry, event_bus = _build_workflow_with_registry()
        slot = registry.get_or_create_slot("plate_x", "plate_x")
        slot.pending.append(_make_method("m1"))
        slot.pending.append(_make_method("m2"))

        captured: list[tuple[str, ExecutionContext]] = []
        event_bus.subscribe("SLOT.plate_x.ABANDONED",
                            lambda name, ctx: captured.append((name, ctx)))

        workflow.cleanup_parked_threads()

        assert len(captured) == 1
        event_name, context = captured[0]
        assert event_name == "SLOT.plate_x.ABANDONED"
        assert context.execution_id == workflow.id

    def test_emits_slot_abandoned_when_queue_nonempty(self) -> None:
        """T9 (queue case): items in slot.queue (not just pending) also trigger ABANDONED."""
        workflow, registry, event_bus = _build_workflow_with_registry()
        slot = registry.get_or_create_slot("plate_y", "plate_y")
        slot.queue.put_nowait(_make_method("q1"))

        captured: list[tuple[str, ExecutionContext]] = []
        event_bus.subscribe("SLOT.plate_y.ABANDONED",
                            lambda name, ctx: captured.append((name, ctx)))

        workflow.cleanup_parked_threads()

        assert len(captured) == 1

    def test_does_not_emit_for_empty_slot(self) -> None:
        """A slot with no pending and no queued items must not raise ABANDONED."""
        workflow, registry, event_bus = _build_workflow_with_registry()
        registry.get_or_create_slot("empty_slot", "empty_slot")

        captured: list[tuple[str, ExecutionContext]] = []
        event_bus.subscribe_all(lambda name, ctx: captured.append((name, ctx)))

        workflow.cleanup_parked_threads()

        abandoned = [n for n, _ in captured if n.endswith(".ABANDONED")]
        assert len(abandoned) == 0


class TestCleanupWarningLog:

    def test_log_includes_slot_key_and_counts(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """T10: WARNING log message contains slot key + pending count + queue count."""
        workflow, registry, _ = _build_workflow_with_registry()
        slot = registry.get_or_create_slot("plate_z", "plate_z")
        slot.pending.append(_make_method("p1"))
        slot.pending.append(_make_method("p2"))
        slot.queue.put_nowait(_make_method("q1"))

        with caplog.at_level(logging.WARNING, logger="orca"):
            workflow.cleanup_parked_threads()

        warning_records = [
            r for r in caplog.records
            if r.name == "orca" and r.levelno == logging.WARNING
        ]
        assert len(warning_records) == 1
        message = warning_records[0].getMessage()
        assert "plate_z" in message
        assert "pending=2" in message
        assert "queue=1" in message

    def test_no_warning_for_empty_slot(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        workflow, registry, _ = _build_workflow_with_registry()
        registry.get_or_create_slot("empty_slot", "empty_slot")

        with caplog.at_level(logging.WARNING, logger="orca"):
            workflow.cleanup_parked_threads()

        warning_records = [
            r for r in caplog.records
            if r.name == "orca" and r.levelno == logging.WARNING
            and "empty_slot" in r.getMessage()
        ]
        assert len(warning_records) == 0

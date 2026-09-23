"""Characterization tests for RuntimeEvent.to_dict() wire-shape preservation.

Pin the exact dict produced by RuntimeEvent.to_dict() for every concrete
ExecutionContext subclass. The characterization is run on the pre-refactor
dataclass form, then again after the Pydantic conversion; both runs MUST
produce byte-identical JSON.

The "wire" is the JSON serialization that flows through LogSink (logger),
daemon SSE/poll routes (FastAPI JSON), and a hosted deployment's WsFanoutSink. Test
asserts JSON-byte-identity rather than in-memory dict identity because
the dict carries a tuple in participating_thread_ids that json.dumps
collapses to a JSON array; the wire never sees the tuple distinction.
"""

import json

from orca.events.execution_context import (
    ExecutionContext,
    ExecutionLifecycleContext,
    GroupLifecycleContext,
    LocationActionExecutionContext,
    MethodExecutionContext,
    MoveActionExecutionContext,
    SubmissionExecutionContext,
    ThreadExecutionContext,
    WorkflowExecutionContext,
)
from orca.events.runtime_event import RuntimeEvent


def _event(context: ExecutionContext) -> RuntimeEvent:
    return RuntimeEvent(
        event_name="ENTITY.id.STATUS",
        execution_id="exec-1",
        timestamp=12345.6789,
        entity_type="ENTITY",
        entity_id="id",
        status="STATUS",
        context=context,
    )


def _json(event: RuntimeEvent) -> str:
    return json.dumps(event.to_dict(), sort_keys=True)


def test_workflow_context_wire_shape() -> None:
    ctx = WorkflowExecutionContext(execution_id="exec-1", workflow_name="wf")
    got = _json(_event(ctx))
    expected = json.dumps(
        {
            "context": {"execution_id": "exec-1", "workflow_name": "wf"},
            "entity_id": "id",
            "entity_type": "ENTITY",
            "event_name": "ENTITY.id.STATUS",
            "execution_id": "exec-1",
            "status": "STATUS",
            "timestamp": 12345.6789,
        },
        sort_keys=True,
    )
    assert got == expected


def test_thread_context_wire_shape() -> None:
    ctx = ThreadExecutionContext(
        execution_id="exec-1", workflow_name="wf",
        thread_id="t1", thread_name="tn", template_name="tt",
    )
    got = _json(_event(ctx))
    expected = json.dumps(
        {
            "context": {
                "execution_id": "exec-1", "workflow_name": "wf",
                "thread_id": "t1", "thread_name": "tn", "template_name": "tt",
                "pause_reason": None, "last_error": None,
            },
            "entity_id": "id",
            "entity_type": "ENTITY",
            "event_name": "ENTITY.id.STATUS",
            "execution_id": "exec-1",
            "status": "STATUS",
            "timestamp": 12345.6789,
        },
        sort_keys=True,
    )
    assert got == expected


def test_method_context_full_wire_shape() -> None:
    ctx = MethodExecutionContext(
        execution_id="exec-1", workflow_name="wf",
        method_id="m1", method_name="mn",
        thread_id="t1", thread_name="tn",
        participating_thread_ids=("t1", "t2"),
    )
    got = _json(_event(ctx))
    expected = json.dumps(
        {
            "context": {
                "execution_id": "exec-1", "workflow_name": "wf",
                "method_id": "m1", "method_name": "mn",
                "thread_id": "t1", "thread_name": "tn",
                "participating_thread_ids": ["t1", "t2"],
            },
            "entity_id": "id",
            "entity_type": "ENTITY",
            "event_name": "ENTITY.id.STATUS",
            "execution_id": "exec-1",
            "status": "STATUS",
            "timestamp": 12345.6789,
        },
        sort_keys=True,
    )
    assert got == expected


def test_method_context_defaults_wire_shape() -> None:
    ctx = MethodExecutionContext(
        execution_id="exec-1", workflow_name="wf",
        method_id=None, method_name=None,
    )
    got = _json(_event(ctx))
    expected = json.dumps(
        {
            "context": {
                "execution_id": "exec-1", "workflow_name": "wf",
                "method_id": None, "method_name": None,
                "thread_id": None, "thread_name": None,
                "participating_thread_ids": [],
            },
            "entity_id": "id",
            "entity_type": "ENTITY",
            "event_name": "ENTITY.id.STATUS",
            "execution_id": "exec-1",
            "status": "STATUS",
            "timestamp": 12345.6789,
        },
        sort_keys=True,
    )
    assert got == expected


def test_location_action_context_wire_shape() -> None:
    ctx = LocationActionExecutionContext(
        execution_id="exec-1", workflow_name="wf",
        method_id="m1", method_name="mn",
        action_id="a1", action_status="RUNNING",
        thread_id="t1", thread_name="tn",
        participating_thread_ids=("t1",),
        action_name="dispense",
    )
    got = _json(_event(ctx))
    expected = json.dumps(
        {
            "context": {
                "execution_id": "exec-1", "workflow_name": "wf",
                "method_id": "m1", "method_name": "mn",
                "thread_id": "t1", "thread_name": "tn",
                "participating_thread_ids": ["t1"],
                "action_id": "a1", "action_status": "RUNNING",
                "action_name": "dispense",
            },
            "entity_id": "id",
            "entity_type": "ENTITY",
            "event_name": "ENTITY.id.STATUS",
            "execution_id": "exec-1",
            "status": "STATUS",
            "timestamp": 12345.6789,
        },
        sort_keys=True,
    )
    assert got == expected


def test_move_action_context_wire_shape() -> None:
    ctx = MoveActionExecutionContext(
        execution_id="exec-1", workflow_name="wf",
        thread_id="t1", thread_name="tn", template_name="tt",
        action_id="a1", action_status="RUNNING",
    )
    got = _json(_event(ctx))
    expected = json.dumps(
        {
            "context": {
                "execution_id": "exec-1", "workflow_name": "wf",
                "thread_id": "t1", "thread_name": "tn", "template_name": "tt",
                "pause_reason": None, "last_error": None,
                "action_id": "a1", "action_status": "RUNNING",
                "action_name": None,
            },
            "entity_id": "id",
            "entity_type": "ENTITY",
            "event_name": "ENTITY.id.STATUS",
            "execution_id": "exec-1",
            "status": "STATUS",
            "timestamp": 12345.6789,
        },
        sort_keys=True,
    )
    assert got == expected


def test_submission_context_wire_shape() -> None:
    ctx = SubmissionExecutionContext(
        execution_id="exec-1", workflow_name="wf",
        submission_id="s1", group_count=2, reason="manual",
    )
    got = _json(_event(ctx))
    expected = json.dumps(
        {
            "context": {
                "execution_id": "exec-1", "workflow_name": "wf",
                "submission_id": "s1", "group_count": 2, "reason": "manual",
            },
            "entity_id": "id",
            "entity_type": "ENTITY",
            "event_name": "ENTITY.id.STATUS",
            "execution_id": "exec-1",
            "status": "STATUS",
            "timestamp": 12345.6789,
        },
        sort_keys=True,
    )
    assert got == expected


def test_group_lifecycle_context_wire_shape() -> None:
    ctx = GroupLifecycleContext(
        execution_id="exec-1", workflow_name="wf",
        submission_id="s1", group_id="g1",
    )
    got = _json(_event(ctx))
    expected = json.dumps(
        {
            "context": {
                "execution_id": "exec-1", "workflow_name": "wf",
                "submission_id": "s1", "group_id": "g1",
            },
            "entity_id": "id",
            "entity_type": "ENTITY",
            "event_name": "ENTITY.id.STATUS",
            "execution_id": "exec-1",
            "status": "STATUS",
            "timestamp": 12345.6789,
        },
        sort_keys=True,
    )
    assert got == expected


def test_execution_lifecycle_context_wire_shape() -> None:
    ctx = ExecutionLifecycleContext(
        execution_id="exec-1", workflow_name="wf", reason="completed",
    )
    got = _json(_event(ctx))
    expected = json.dumps(
        {
            "context": {
                "execution_id": "exec-1", "workflow_name": "wf",
                "reason": "completed",
            },
            "entity_id": "id",
            "entity_type": "ENTITY",
            "event_name": "ENTITY.id.STATUS",
            "execution_id": "exec-1",
            "status": "STATUS",
            "timestamp": 12345.6789,
        },
        sort_keys=True,
    )
    assert got == expected

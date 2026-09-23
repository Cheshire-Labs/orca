"""Regression: slot closure must be scoped to the slot's own group/submission.

The bug (pre-fix): ``ExecutingWorkflow._evaluate_slot_closures`` judged "is any
feeder still live?" by thread-TEMPLATE NAME across the whole execution, ignoring
which submission each feeder belonged to. So a submission-scoped receiver
(a STANDALONE / isolated BATCHABLE instance keyed ``reservoir:*:sub1``) was held
open forever by a SIBLING submission's live feeder of the same template. It
parked on its home pad and deadlocked a sibling receiver that needed that pad.

The fix decodes each slot's scope from its key (``*`` -> match any) and only
counts feeders in the slot's own scope. A shared/pooled receiver (``reservoir:*:*``)
still counts every submission's feeders, so JOIN_EXISTING pooling is unchanged.

All tests here are deterministic: they exercise the pure scope decode, the
in-scope predicate, and one hand-built ``_evaluate_slot_closures`` call. No sim,
no timing.
"""

from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import cast

import pytest

from orca.resource_models.labware import PlateTemplate
from orca.runtime.group_aware_labware_registry import GroupAwareLabwareRegistry
from orca.runtime.group_execution_context import GroupExecutionContext
from orca.runtime.labware_group import LabwareGroup, LabwareGroupMember
from orca.runtime.submission import BatchMode
from orca.sdk.workflow import ThreadTemplate, WorkflowTemplate
from orca.workflow_models.labware_threads.executing_labware_thread import (
    ExecutingLabwareThread,
)
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflows.executing_workflow import ExecutingWorkflow
from orca.workflow_models.workflows.workflow import WorkflowInstance


def _stub_thread(template_name: str, group_id: str | None,
                 submission_id: str | None, completed: bool) -> ExecutingLabwareThread:
    """A minimal stand-in for an ExecutingLabwareThread as read by the closure
    logic: thread_instance.{thread_template.name, group_id, submission_id},
    has_completed() and has_finished_its_work()."""
    instance = SimpleNamespace(
        thread_template=SimpleNamespace(name=template_name),
        group_id=group_id,
        submission_id=submission_id,
    )
    stub = SimpleNamespace(
        thread_instance=instance,
        has_completed=lambda: completed,
        has_finished_its_work=lambda: completed,
    )
    return cast(ExecutingLabwareThread, stub)


def _dummy_thread(name: str, contributes_to: list[str]) -> ThreadTemplate:
    async def _fn(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        if False:
            yield  # pragma: no cover - never runs; satisfies async-generator typing
    return ThreadTemplate(
        labware_template=PlateTemplate(
            name, labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black"),
        start=f"{name}_pad",
        end=f"{name}_pad",
        func=_fn,
        contributes_to=contributes_to,
    )


def _feeder_workflow() -> WorkflowTemplate:
    """sample -> reservoir: a single declared feeder for the receiver."""
    wt = WorkflowTemplate("scoped_closure_test")
    wt.add_thread(_dummy_thread("sample", ["reservoir"]))
    wt.add_thread(_dummy_thread("reservoir", []))
    return wt


def _executing_workflow(registry: GroupAwareLabwareRegistry,
                        template: WorkflowTemplate,
                        threads: list[ExecutingLabwareThread]) -> ExecutingWorkflow:
    # Pokes the private attrs _evaluate_slot_closures reads; the runtime-driven
    # close-race tests are the backstop if that internal surface changes.
    wf = ExecutingWorkflow.__new__(ExecutingWorkflow)
    wf._labware_registry = registry
    wf._workflow = cast(WorkflowInstance, SimpleNamespace(template=template))
    wf._entry_threads = list(threads)
    wf._spawned_threads = []
    wf._pending_injections = 0
    return wf


def test_slot_scope_decode() -> None:
    r = GroupAwareLabwareRegistry()
    assert r.slot_scope("final_plate:*:sub1") == (None, "sub1")
    assert r.slot_scope("final_plate:*:*") == (None, None)
    assert r.slot_scope("final_plate:g1:sub1") == ("g1", "sub1")
    # Bare (deck-resident / non-group) keys carry no scope.
    assert r.slot_scope("reservoir") == (None, None)


def test_thread_in_scope_truth_table() -> None:
    t = _stub_thread("sample", group_id="g1", submission_id="sub1", completed=False)
    # Wildcard (shared slot) matches any thread.
    assert ExecutingWorkflow._thread_in_scope(t, None, None)
    # Concrete submission scope matches only that submission.
    assert ExecutingWorkflow._thread_in_scope(t, None, "sub1")
    assert not ExecutingWorkflow._thread_in_scope(t, None, "sub2")
    # Concrete group scope matches only that group.
    assert ExecutingWorkflow._thread_in_scope(t, "g1", None)
    assert not ExecutingWorkflow._thread_in_scope(t, "g2", None)
    # Both components must match.
    assert ExecutingWorkflow._thread_in_scope(t, "g1", "sub1")
    assert not ExecutingWorkflow._thread_in_scope(t, "g1", "sub2")


def test_scoped_slot_closes_ignoring_sibling_submission_feeder() -> None:
    """A STANDALONE receiver (reservoir:*:sub1) closes when ITS OWN feeder is
    done, even while a sibling submission's feeder of the same template is live.
    Fails pre-fix (the sibling's feeder held it open forever)."""
    registry = GroupAwareLabwareRegistry()
    slot = registry.get_or_create_slot("reservoir:*:sub1", "reservoir")
    threads = [
        _stub_thread("sample", group_id=None, submission_id="sub1", completed=True),
        _stub_thread("sample", group_id=None, submission_id="sub2", completed=False),
    ]
    wf = _executing_workflow(registry, _feeder_workflow(), threads)

    wf._evaluate_slot_closures()

    assert slot.is_closed, (
        "a submission-scoped receiver must close when its own feeder is done; "
        "a sibling submission's live feeder must not hold it open"
    )


def test_shared_slot_stays_open_while_any_submission_feeder_live() -> None:
    """A shared/pooled receiver (reservoir:*:*) stays open while ANY submission's
    feeder is live -- the JOIN_EXISTING pooling north star is unchanged."""
    registry = GroupAwareLabwareRegistry()
    slot = registry.get_or_create_slot("reservoir:*:*", "reservoir")
    threads = [
        _stub_thread("sample", group_id=None, submission_id="sub1", completed=True),
        _stub_thread("sample", group_id=None, submission_id="sub2", completed=False),
    ]
    wf = _executing_workflow(registry, _feeder_workflow(), threads)

    wf._evaluate_slot_closures()

    assert not slot.is_closed, (
        "a shared receiver must stay open while any submission's feeder is live"
    )


def test_labware_group_rejects_colon_in_id() -> None:
    """A ':' in a group id is refused at submit (ingress), not discovered when a
    receiver spawns: it delimits the slot key and would silently break scoping."""
    with pytest.raises(ValueError, match="must not contain ':'"):
        LabwareGroup(id="grp:1", members=(LabwareGroupMember(thread_template_name="t"),))


def test_slot_key_composition_rejects_colon_component() -> None:
    """Defense-in-depth: composing a key from a ':'-bearing component raises
    rather than degrading the decoded scope to a wildcard."""
    registry = GroupAwareLabwareRegistry()
    template = _dummy_thread("final_plate", [])
    ctx = GroupExecutionContext(
        group_id="g:1", submission_id="sub-1", batch_mode=BatchMode.STANDALONE,
    )
    with pytest.raises(ValueError, match="must not contain ':'"):
        registry.slot_key_for(template, ctx)

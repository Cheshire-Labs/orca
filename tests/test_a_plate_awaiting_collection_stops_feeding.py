"""A thread waiting for an operator to collect its labware stops feeding.

Found on the bench 2026-08-31. A source plate reached its end location and
parked at AWAITING_MANUAL_REMOVE. That park is not terminal, so the closure
sweep counted the thread as a live feeder: the receiver slot stayed open, the
shared final plate and the tip rack joined to it kept looping on their join,
and both stayed pinned to the liquid handler's deck until the operator walked
over and picked the source plate up.

The park sits after the last action and the end move, so the thread has made
every contribution it will ever make. Only the physical pickup is outstanding.
"""

import asyncio
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

from orca.resource_models.labware import PlateTemplate
from orca.runtime.group_aware_labware_registry import GroupAwareLabwareRegistry
from orca.sdk.workflow import ThreadTemplate, WorkflowTemplate
from orca.workflow_models.labware_threads.executing_labware_thread import (
    ExecutingLabwareThread,
)
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.status_enums import LabwareThreadStatus
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflows.executing_workflow import ExecutingWorkflow
from orca.workflow_models.workflows.workflow import WorkflowInstance


def _thread_at(template_name: str, status: LabwareThreadStatus) -> ExecutingLabwareThread:
    """A stand-in carrying the real predicates, so the closure sweep reads the
    same answers ExecutingLabwareThread would give at that status."""
    instance = SimpleNamespace(
        thread_template=SimpleNamespace(name=template_name),
        group_id=None,
        submission_id=None,
    )
    stub = SimpleNamespace(
        thread_instance=instance,
        status=status,
        has_completed=lambda: ExecutingLabwareThread.has_completed(
            cast(ExecutingLabwareThread, stub)),
        has_finished_its_work=lambda: ExecutingLabwareThread.has_finished_its_work(
            cast(ExecutingLabwareThread, stub)),
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
    wt = WorkflowTemplate("collection_park_test")
    wt.add_thread(_dummy_thread("sample", ["reservoir"]))
    wt.add_thread(_dummy_thread("reservoir", []))
    return wt


def _executing_workflow(registry: GroupAwareLabwareRegistry,
                        threads: list[ExecutingLabwareThread]) -> ExecutingWorkflow:
    wf = ExecutingWorkflow.__new__(ExecutingWorkflow)
    wf._labware_registry = registry
    wf._workflow = cast(WorkflowInstance, SimpleNamespace(template=_feeder_workflow()))
    wf._entry_threads = list(threads)
    wf._spawned_threads = []
    wf._pending_injections = 0
    return wf


class TestTheCollectionParkIsNotAContribution:
    def test_a_thread_waiting_to_be_collected_has_finished_its_work(self) -> None:
        thread = _thread_at("sample", LabwareThreadStatus.AWAITING_MANUAL_REMOVE)

        assert thread.has_finished_its_work()
        assert not thread.has_completed(), (
            "the labware is still physically on the deck, so the thread must "
            "not read as terminal"
        )

    def test_a_thread_waiting_to_be_placed_has_not(self) -> None:
        """The other manual park is at the START of a thread's life: its work
        is all still ahead of it."""
        thread = _thread_at("sample", LabwareThreadStatus.AWAITING_MANUAL_PLACE)

        assert not thread.has_finished_its_work()

    def test_the_receiver_closes_while_the_plate_waits_to_be_collected(self) -> None:
        registry = GroupAwareLabwareRegistry()
        slot = registry.get_or_create_slot("reservoir", "reservoir")
        threads = [_thread_at("sample", LabwareThreadStatus.AWAITING_MANUAL_REMOVE)]
        wf = _executing_workflow(registry, threads)

        wf._evaluate_slot_closures()

        assert slot.is_closed, (
            "a feeder parked for collection has made every contribution it "
            "will make; the receiver it feeds must not wait for the operator"
        )

    def test_a_still_running_feeder_holds_the_receiver_open(self) -> None:
        registry = GroupAwareLabwareRegistry()
        slot = registry.get_or_create_slot("reservoir", "reservoir")
        threads = [_thread_at("sample", LabwareThreadStatus.EXECUTING_ACTION)]
        wf = _executing_workflow(registry, threads)

        wf._evaluate_slot_closures()

        assert not slot.is_closed


def _thread_publishing() -> ExecutingLabwareThread:
    """A thread whose only working parts are the ones the status publish
    touches, so the real hook block runs."""
    et = ExecutingLabwareThread.__new__(ExecutingLabwareThread)
    published: dict[str, str] = {}

    def _set_status(_kind: str, _id: str, name: str, _ctx: object) -> None:
        published["status"] = name

    # `MagicMock(name=...)` names the mock itself, so every `.name` this
    # publish reads has to be set after construction.
    inner = MagicMock(id="t1", template_name="sample")
    inner.name = "sample_1"
    inner.labware = MagicMock(id="lw1")
    inner.labware.name = "plate_1"
    inner.labware_template = MagicMock()
    inner.labware_template.name = "sample"
    inner.start_location = MagicMock()
    inner.start_location.name = "sample_pad"

    here = MagicMock()
    here.name = "sample_pad"
    location_service = MagicMock()
    location_service.get.return_value = here

    for attr, value in (
        ("_thread", inner),
        ("_context", MagicMock(execution_id="e1", workflow_name="wf")),
        ("_last_error", None),
        ("_pause_reason", None),
        ("completed", asyncio.Event()),
        ("_labware_location_service", location_service),
        ("_status_manager", MagicMock(
            set_status=_set_status, get_status=lambda _id: published["status"])),
    ):
        setattr(et, attr, value)
    return et


class TestTheHookFiresWhenTheCloseBecomesDue:
    """The sweep reading the park is only half of it. Nothing re-evaluates the
    slots on its own, so a hook that stays quiet at the park leaves the close
    waiting for some later thread's terminal, which is the bench symptom."""

    def test_parking_for_collection_fires_the_work_finished_hook(self) -> None:
        thread = _thread_publishing()
        fired: list[ExecutingLabwareThread] = []
        thread.set_work_finished_hook(fired.append)

        thread._publish_status_to_status_manager(
            LabwareThreadStatus.AWAITING_MANUAL_REMOVE)

        assert fired == [thread]

    def test_pausing_does_not(self) -> None:
        thread = _thread_publishing()
        fired: list[ExecutingLabwareThread] = []
        thread.set_work_finished_hook(fired.append)

        thread._publish_status_to_status_manager(LabwareThreadStatus.PAUSED)

        assert fired == []

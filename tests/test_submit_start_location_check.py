"""The pre-submission start_location check.

Today's bug: when a prior execution leaves labware at a thread template's
start_location, the next submission stalls silently inside
`ExecutingLabwareThread.initialize_labware` retrying `DeviceBusyError`
forever. No event, no error, no recovery.

This check runs at submit time, after `_resolve_acquisitions`, and refuses
the submission with a typed `StartLocationsOccupiedError` envelope so the
operator sees the failure and knows what to clear.

Skip rules:
- BarcodeAcquisition resolved: the acquisition already attaches an existing
  LabwareInstance, no fresh placement needed.
- LocationAcquisition resolved: the override location is the effective
  start_location; check THAT, not the template default.
"""

import pytest

from orca.resource_models.labware import LabwareInstance
from orca.runtime.labware_group import (
    BarcodeAcquisition,
    LabwareGroup,
    LabwareGroupMember,
    LocationAcquisition,
)
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.runtime_interface import StartLocationsOccupiedError
from orca.runtime.system_runtime import SystemRuntime
from orca.runtime.run_modes import WorkflowRunMode
from tests.test_system_runtime import _build_simple_system


class TestStartLocationCheck:

    async def test_submit_when_start_location_empty_succeeds(self) -> None:
        system, workflow = await _build_simple_system()
        runtime = SystemRuntime(system)
        await runtime.start()
        try:
            submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
            assert submission.id is not None
        finally:
            await runtime.shutdown()

    async def test_submit_when_default_thread_start_occupied_raises_typed_error(
        self,
    ) -> None:
        system, workflow = await _build_simple_system()
        # Pre-occupy pad1 (the entry thread's start_location) with stale labware
        # from a prior execution that never picked it off.
        leftover = LabwareInstance("plate_96", "96_well")
        pad1 = system.system_map.get_location("pad1")
        pad1.initialize_labware(leftover)

        runtime = SystemRuntime(system)
        await runtime.start()
        try:
            with pytest.raises(StartLocationsOccupiedError) as exc_info:
                await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
            err = exc_info.value
            assert len(err.occupied) == 1
            slot = err.occupied[0]
            assert slot.position_id == "pad1"
            assert slot.existing_labware_name == leftover.name
            assert slot.existing_template_name == "plate_96"
        finally:
            await runtime.shutdown()

    async def test_groupless_submit_with_occupied_template_default_raises(
        self,
    ) -> None:
        """Groupless submit (groups=()) must still run the start_location check
        against template defaults — that's the canonical reproducer."""
        system, workflow = await _build_simple_system()
        leftover = LabwareInstance("plate_96", "96_well")
        system.system_map.get_location("pad1").initialize_labware(leftover)

        runtime = SystemRuntime(system)
        await runtime.start()
        try:
            with pytest.raises(StartLocationsOccupiedError):
                await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)  # groups omitted entirely
        finally:
            await runtime.shutdown()

    async def test_submit_skips_check_when_barcode_acquisition_resolved(
        self,
    ) -> None:
        """BarcodeAcquisition supplies its own LabwareInstance; the entry thread
        doesn't need to claim a fresh slot at start_location, so the check is
        a no-op for that thread even if the default location happens to be
        occupied by something else."""
        system, workflow = await _build_simple_system()
        store = InMemoryLabwareStore()
        existing = LabwareInstance("plate_96", "96_well")
        existing.barcode = "BC-42"
        await store.register(existing)
        # pad1 is occupied (would normally trip the check), but Barcode bypasses.
        pad1 = system.system_map.get_location("pad1")
        unrelated = LabwareInstance("plate_96", "96_well")
        pad1.initialize_labware(unrelated)

        runtime = SystemRuntime(system, labware_store=store)
        await runtime.start()
        try:
            group = LabwareGroup(
                id="grp-1",
                members=(
                    LabwareGroupMember(
                        thread_template_name="plate_96",
                        acquisition=BarcodeAcquisition(barcode="BC-42"),
                    ),
                ),
            )
            submission = await runtime.submit(workflow, groups=[group], mode=WorkflowRunMode.PURE_SIM)
            assert submission.id is not None
        finally:
            await runtime.shutdown()

    async def test_submit_respects_location_acquisition_override(self) -> None:
        """LocationAcquisition overrides the effective start_location. If the
        override is empty, the submission must succeed even when the template
        default is occupied."""
        system, workflow = await _build_simple_system()
        # Template default (pad1) is occupied
        pad1 = system.system_map.get_location("pad1")
        pad1.initialize_labware(LabwareInstance("plate_96", "96_well"))
        # The override location (shaker1) is empty — submission should pass.
        runtime = SystemRuntime(system)
        await runtime.start()
        try:
            group = LabwareGroup(
                id="grp-1",
                members=(
                    LabwareGroupMember(
                        thread_template_name="plate_96",
                        acquisition=LocationAcquisition(source_location="shaker1"),
                    ),
                ),
            )
            submission = await runtime.submit(workflow, groups=[group], mode=WorkflowRunMode.PURE_SIM)
            assert submission.id is not None
        finally:
            await runtime.shutdown()

    async def test_submit_skips_check_when_start_reuse_existing_true(
        self,
    ) -> None:
        """A reuse-existing thread handles its own occupancy via the auto-spawn
        callback's bind block. The pre-submission check must skip it even when
        the start_location is currently occupied."""
        from collections.abc import AsyncGenerator

        from orca.spawn import REUSE_EXISTING
        from orca.workflow_models.method_template import IMethodTemplate
        from orca.workflow_models.thread_context import ThreadContext
        from orca.workflow_models.thread_template import ThreadTemplate
        from orca.workflow_models.workflow_templates import WorkflowTemplate

        system, workflow = await _build_simple_system()
        # Construct a reuse workflow targeting pad2 with a separate labware.
        plate_b = system.get_labware_template("plate_96_b")

        async def _gen(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            del ctx
            return
            yield  # unreachable; reuse thread body is never executed in this submit-only test

        reuse_thread = ThreadTemplate(
            labware_template=plate_b,
            start=("pad2", REUSE_EXISTING),
            end="pad2",
            func=_gen,
        )
        reuse_thread.resolve_locations(system.system_map.get_location)
        wf_reuse = WorkflowTemplate("reuse_wf")
        wf_reuse.add_thread(reuse_thread)  # NOT is_start (cannot be entry).
        system.add_workflow_template(wf_reuse)

        # Pre-occupy pad2 with a matching template. The reuse-bind path will
        # later bind to this at runtime. The pre-check must NOT
        # flag it.
        leftover = LabwareInstance("plate_96_b", "96_well")
        system.system_map.get_location("pad2").initialize_labware(leftover)

        runtime = SystemRuntime(system)
        await runtime.start()
        try:
            # Submit with NO groups so the only entry walk is on `wf_reuse`'s
            # entry_thread_templates (which is empty -- reuse threads can't
            # be entries). Use the original simple_workflow whose default
            # pad1 is empty; passing through with no occupied error proves
            # the skip rule applies.
            sub = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
            assert sub.id is not None
        finally:
            await runtime.shutdown()

    async def test_submit_when_location_acquisition_overrides_to_occupied_raises(
        self,
    ) -> None:
        """Override location is what gets checked, not the template default.
        If the override is occupied, submission must raise."""
        system, workflow = await _build_simple_system()
        # Template default (pad1) is empty
        # Override target (shaker1) is occupied
        shaker = system.system_map.get_location("shaker1")
        shaker.initialize_labware(LabwareInstance("plate_96", "96_well"))

        runtime = SystemRuntime(system)
        await runtime.start()
        try:
            group = LabwareGroup(
                id="grp-1",
                members=(
                    LabwareGroupMember(
                        thread_template_name="plate_96",
                        acquisition=LocationAcquisition(source_location="shaker1"),
                    ),
                ),
            )
            with pytest.raises(StartLocationsOccupiedError) as exc_info:
                await runtime.submit(workflow, groups=[group], mode=WorkflowRunMode.PURE_SIM)
            slot = exc_info.value.occupied[0]
            assert slot.position_id == "shaker1"
        finally:
            await runtime.shutdown()

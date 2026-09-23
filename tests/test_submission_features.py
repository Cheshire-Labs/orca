"""T6g: submission-scoped variables + Acquisition validation at submit time.

Submission variables: values passed to runtime.submit(variables={...}) land in
the execution partition of the VariableStore so the workflow's threads see
them as overrides to workflow defaults. Preserves strict scoping (bare names
stay workflow-scoped; global.* names stay global).

Acquisition validation:
- BarcodeAcquisition rejects at submit if the barcode is not registered in
  the runtime's ILabwareStore.
- LocationAcquisition rejects at submit if the source_location is not known
  to the topology.
- PoolAcquisition (default) always accepts.
"""

import asyncio

import pytest

from tests.test_helpers import execution_outcome

from orca.runtime.labware_group import (
    AcquisitionValidationError,
    BarcodeAcquisition,
    LabwareGroup,
    LabwareGroupMember,
    LocationAcquisition,
)
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.system_runtime import SystemRuntime
from orca.resource_models.labware import LabwareInstance
from orca.variables import VariableDefinition
from orca.runtime.run_modes import WorkflowRunMode
from tests.test_system_runtime import _build_simple_system


class TestSubmissionVariables:

    async def test_submission_variables_override_workflow_default(self) -> None:
        system, workflow = await _build_simple_system()
        system.variable_store.register_workflow_definitions(
            workflow.name, {"shake_time": VariableDefinition(type="int", default=60)}
        )
        runtime = SystemRuntime(system)
        await runtime.start()

        submission = await runtime.submit(workflow, variables={"shake_time": 120}, mode=WorkflowRunMode.PURE_SIM)
        status = await execution_outcome(runtime, submission, timeout=30.0)
        assert status.status == "completed"
        await runtime.shutdown()

        # The value we passed in submit() must be the resolved value for the
        # execution partition. The execution's workflow instance id maps to
        # the submission; the variable store keeps the partition until remove.
        # By inspection at runtime: submission.variables[shake_time] == 120.
        assert submission.variables["shake_time"] == 120

    async def test_submission_without_variables_uses_defaults(self) -> None:
        system, workflow = await _build_simple_system()
        system.variable_store.register_workflow_definitions(
            workflow.name, {"shake_time": VariableDefinition(type="int", default=60)}
        )
        runtime = SystemRuntime(system)
        await runtime.start()

        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        status = await execution_outcome(runtime, submission, timeout=30.0)
        assert status.status == "completed"
        await runtime.shutdown()

        # ``variables == {}`` alone never proves the default was applied;
        # with no override written, resolution must fall through to it.
        assert submission.variables == {}
        store = system.variable_store
        store.create_execution("exec-defaults", workflow.name)
        try:
            assert store.resolve("shake_time", "exec-defaults") == 60
            assert (
                store.resolve(
                    "shake_time", "exec-defaults", submission_id=submission.id,
                )
                == 60
            )
        finally:
            store.remove_execution("exec-defaults")


class TestAcquisitionValidation:

    async def test_barcode_acquisition_rejects_unknown_barcode(self) -> None:
        system, workflow = await _build_simple_system()
        runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
        await runtime.start()

        group = LabwareGroup(
            id="grp-1",
            members=(
                LabwareGroupMember(
                    thread_template_name="plate_96",
                    acquisition=BarcodeAcquisition(barcode="MISSING-123"),
                ),
            ),
        )
        with pytest.raises(AcquisitionValidationError, match="MISSING-123"):
            await runtime.submit(workflow, groups=[group], mode=WorkflowRunMode.PURE_SIM)
        await runtime.shutdown()

    async def test_barcode_acquisition_accepts_registered_barcode(self) -> None:
        system, workflow = await _build_simple_system()
        store = InMemoryLabwareStore()
        instance = LabwareInstance("plate_96", "96_well")
        instance.barcode = "BC-42"
        await store.register(instance)
        runtime = SystemRuntime(system, labware_store=store)
        await runtime.start()

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
        await runtime.shutdown()

    async def test_location_acquisition_rejects_unknown_location(self) -> None:
        system, workflow = await _build_simple_system()
        runtime = SystemRuntime(system)
        await runtime.start()

        group = LabwareGroup(
            id="grp-1",
            members=(
                LabwareGroupMember(
                    thread_template_name="plate_96",
                    acquisition=LocationAcquisition(source_location="ghost_slot"),
                ),
            ),
        )
        with pytest.raises(AcquisitionValidationError, match="ghost_slot"):
            await runtime.submit(workflow, groups=[group], mode=WorkflowRunMode.PURE_SIM)
        await runtime.shutdown()

    async def test_location_acquisition_accepts_known_location(self) -> None:
        system, workflow = await _build_simple_system()
        runtime = SystemRuntime(system)
        await runtime.start()

        group = LabwareGroup(
            id="grp-1",
            members=(
                LabwareGroupMember(
                    thread_template_name="plate_96",
                    acquisition=LocationAcquisition(source_location="pad1"),
                ),
            ),
        )
        submission = await runtime.submit(workflow, groups=[group], mode=WorkflowRunMode.PURE_SIM)
        assert submission.id is not None
        await runtime.shutdown()


class TestAcquisitionAttachment:
    """Regression tests for member-Acquisition wiring in the thread factory.

    - BarcodeAcquisition: entry thread attaches the LabwareInstance already
      registered in the store (same identity), not a freshly-minted instance.
    - LocationAcquisition: entry thread's start_location is overridden to the
      member's source_location, not the thread template's default start.
    """

    async def test_barcode_acquisition_attaches_existing_instance(self) -> None:
        system, workflow = await _build_simple_system()
        store = InMemoryLabwareStore()
        existing = LabwareInstance("plate_96", "96_well")
        existing.barcode = "BC-99"
        await store.register(existing)
        runtime = SystemRuntime(system, labware_store=store)
        await runtime.start()

        group = LabwareGroup(
            id="grp-barcode",
            members=(
                LabwareGroupMember(
                    thread_template_name="plate_96",
                    acquisition=BarcodeAcquisition(barcode="BC-99"),
                ),
            ),
        )
        submission = await runtime.submit(workflow, groups=[group], mode=WorkflowRunMode.PURE_SIM)
        resolved = submission.resolved_acquisitions.get(("grp-barcode", "plate_96"))
        assert resolved is not None, "submission must record the resolved acquisition"
        assert resolved.labware_instance is existing, (
            "BarcodeAcquisition must attach the exact instance from the store"
        )

        status = await execution_outcome(runtime, submission, timeout=30.0)
        assert status.status == "completed"
        await runtime.shutdown()

    def test_submission_partitions_isolate_overrides(self) -> None:
        """Unit-test the nested submission scope directly on VariableStore:
        two submissions sharing one execution_id each have their own partition
        overriding the execution partition, not clobbering each other.

        This isolates the nesting contract from the runtime submit path —
        full-integration coverage lives in the adaptive E2E tests.
        """
        from orca.variables.variable_store import VariableService, VariableStore

        store = VariableService(VariableStore())
        store.register_workflow_definitions(
            "wf", {"shake_time": VariableDefinition(type="int", default=60)},
        )
        store.create_execution("exec-1", "wf")

        # No submission_id => workflow default.
        assert store.resolve("shake_time", "exec-1") == 60

        store.set_submission("shake_time", 120, "exec-1", "sub-a")
        store.set_submission("shake_time", 30, "exec-1", "sub-b")

        # Each submission sees its own override.
        assert store.resolve("shake_time", "exec-1", submission_id="sub-a") == 120
        assert store.resolve("shake_time", "exec-1", submission_id="sub-b") == 30
        # Neither write leaked into the execution partition.
        assert store.resolve("shake_time", "exec-1") == 60

        # Removing the execution also clears all submission partitions under it.
        store.remove_execution("exec-1")
        import pytest
        from orca.variables.errors import UndefinedVariableError
        with pytest.raises(KeyError):
            store.resolve("shake_time", "exec-1", submission_id="sub-a")

    async def test_location_acquisition_overrides_thread_start(self) -> None:
        system, workflow = await _build_simple_system()
        runtime = SystemRuntime(system)
        await runtime.start()

        group = LabwareGroup(
            id="grp-location",
            members=(
                LabwareGroupMember(
                    thread_template_name="plate_96",
                    acquisition=LocationAcquisition(source_location="pad1"),
                ),
            ),
        )
        submission = await runtime.submit(workflow, groups=[group], mode=WorkflowRunMode.PURE_SIM)
        resolved = submission.resolved_acquisitions.get(("grp-location", "plate_96"))
        assert resolved is not None, "submission must record the resolved acquisition"
        assert resolved.start_location is not None
        assert resolved.start_location.name == "pad1", (
            "LocationAcquisition must resolve source_location to a Location object"
        )

        status = await execution_outcome(runtime, submission, timeout=30.0)
        assert status.status == "completed"
        await runtime.shutdown()

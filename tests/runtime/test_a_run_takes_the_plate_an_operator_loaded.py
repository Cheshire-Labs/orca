"""An entry thread runs on the unused plate an operator put on its start location; a used or foreign plate is refused."""

from collections.abc import Sequence

import pytest

from orca.workflow_models.labware_group import LabwareGroup, LabwareGroupMember
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.execution import Execution
from orca.runtime.errors import RunModeMismatchError, StartLocationsOccupiedError
from orca.workflow_models.submission import ResolvedAcquisition
from orca.runtime.system_runtime import SystemRuntime
from orca.workflow_models.workflow_templates import WorkflowTemplate
from tests.memory_activity import memory_runtime
from tests.no_devices import NullGatewayRegistry
from tests.runtime.manual_place_fixtures import _build_manual_place_system, _live_connection_source
from tests.test_helpers import run_to_quiescence

WORKFLOW = "wf_loaded"
TEMPLATE = f"plate_{WORKFLOW}"
OTHER_TYPE = "Cor_96_wellplate_360ul_Fb"


async def _runtime(leave_in_place: bool = False) -> SystemRuntime:
    system = await _build_manual_place_system(WORKFLOW, leave_in_place=leave_in_place)
    runtime = await memory_runtime(
        system, gateway_registry=NullGatewayRegistry(), connection_source=_live_connection_source(),
    )
    await runtime.start()
    return runtime


async def test_a_submission_with_no_groups_runs_on_the_plate_the_operator_loaded() -> None:
    runtime = await _runtime()
    try:
        loaded = await runtime.labware.register(TEMPLATE, location="pad1", confirm=True)

        record = await runtime.submit_workflow(WORKFLOW, mode=WorkflowRunMode.CHECK_RUN)

        await run_to_quiescence(runtime, record.id)
        assert [t.labware_id for t in runtime.list_threads(record.id)] == [loaded.id]
    finally:
        await runtime.shutdown()


async def test_a_plate_a_run_has_used_is_still_refused() -> None:
    runtime = await _runtime()
    try:
        await runtime.labware.register(TEMPLATE, location="pad1", confirm=True)
        await runtime.submit_workflow(WORKFLOW, mode=WorkflowRunMode.LIVE)

        with pytest.raises(StartLocationsOccupiedError):
            await runtime.submit_workflow(WORKFLOW, mode=WorkflowRunMode.LIVE)
    finally:
        await runtime.shutdown()


async def test_a_plate_of_another_template_on_the_start_location_is_still_refused() -> None:
    runtime = await _runtime()
    try:
        await runtime.labware.register(labware_type=OTHER_TYPE, location="pad1", confirm=True)

        with pytest.raises(StartLocationsOccupiedError):
            await runtime.submit_workflow(WORKFLOW, mode=WorkflowRunMode.LIVE)
    finally:
        await runtime.shutdown()


async def test_a_plate_a_removed_run_made_and_used_is_still_refused() -> None:
    runtime = await _runtime(leave_in_place=True)
    try:
        record = await runtime.submit_workflow(WORKFLOW, mode=WorkflowRunMode.CHECK_RUN)
        await run_to_quiescence(runtime, record.id)
        await runtime.wait(record.id)
        [made_id] = [t.labware_id for t in runtime.list_threads(record.id)]
        [plate] = [lw for lw in runtime.system.labwares if lw.id == made_id]
        runtime.submissions.remove_execution(record.id, reason="done", confirm=True)
        pad = runtime.system.system_map.get_location("pad1")
        if pad.labware is None:
            pad.initialize_labware(plate)

        with pytest.raises(StartLocationsOccupiedError):
            await runtime.submit_workflow(WORKFLOW, mode=WorkflowRunMode.CHECK_RUN)
    finally:
        await runtime.shutdown()


async def test_a_refused_submission_leaves_the_loaded_plate_free_for_the_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = await _runtime()
    try:
        loaded = await runtime.labware.register(TEMPLATE, location="pad1", confirm=True)
        real = runtime._validate_start_locations
        calls = 0

        def refuse_once(
            workflow: WorkflowTemplate,
            groups: Sequence[LabwareGroup],
            resolved: dict[tuple[str, str], ResolvedAcquisition],
            join_target: Execution | None = None,
        ) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise StartLocationsOccupiedError([])
            real(workflow, groups, resolved, join_target)

        monkeypatch.setattr(runtime, "_validate_start_locations", refuse_once)
        with pytest.raises(StartLocationsOccupiedError):
            await runtime.submit_workflow(WORKFLOW, mode=WorkflowRunMode.CHECK_RUN)

        record = await runtime.submit_workflow(WORKFLOW, mode=WorkflowRunMode.CHECK_RUN)
        await run_to_quiescence(runtime, record.id)
        assert [t.labware_id for t in runtime.list_threads(record.id)] == [loaded.id]
    finally:
        await runtime.shutdown()


async def test_a_group_that_names_the_thread_by_its_template_name_still_runs_on_the_loaded_plate() -> None:
    runtime = await _runtime()
    try:
        loaded = await runtime.labware.register(TEMPLATE, location="pad1", confirm=True)
        workflow = runtime.system.get_workflow_template(WORKFLOW)
        [entry] = workflow.entry_thread_templates
        group = LabwareGroup(id="g1", members=(LabwareGroupMember(entry.name),))

        submission = await runtime.submit(workflow, groups=[group], mode=WorkflowRunMode.CHECK_RUN)

        await run_to_quiescence(runtime, submission.execution_id)
        assert [t.labware_id for t in runtime.list_threads(submission.execution_id)] == [loaded.id]
    finally:
        await runtime.shutdown()


async def test_a_submission_refused_after_its_start_locations_pass_leaves_the_loaded_plate_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = await _runtime()
    try:
        loaded = await runtime.labware.register(TEMPLATE, location="pad1", confirm=True)
        real = runtime._build_fresh_execution
        calls = 0

        def refuse_once(
            workflow: WorkflowTemplate, groups: Sequence[LabwareGroup], *, mode: WorkflowRunMode, sim_speedup: float | None,
        ) -> tuple[Execution, bool]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RunModeMismatchError(
                    blocking_execution_id="e0", blocking_workflow_name=WORKFLOW,
                    existing_run_mode=WorkflowRunMode.LIVE, submitted_run_mode=mode,
                )
            return real(workflow, groups, mode=mode, sim_speedup=sim_speedup)

        monkeypatch.setattr(runtime, "_build_fresh_execution", refuse_once)
        with pytest.raises(RunModeMismatchError):
            await runtime.submit_workflow(WORKFLOW, mode=WorkflowRunMode.CHECK_RUN)

        record = await runtime.submit_workflow(WORKFLOW, mode=WorkflowRunMode.CHECK_RUN)
        await run_to_quiescence(runtime, record.id)
        assert [t.labware_id for t in runtime.list_threads(record.id)] == [loaded.id]
    finally:
        await runtime.shutdown()


async def test_a_groupless_run_on_a_loaded_plate_is_not_one_a_later_submission_can_join() -> None:
    runtime = await _runtime()
    try:
        await runtime.labware.register(TEMPLATE, location="pad1", confirm=True)

        await runtime.submit_workflow(WORKFLOW, mode=WorkflowRunMode.LIVE)

        assert WORKFLOW not in runtime._active_executions
    finally:
        await runtime.shutdown()

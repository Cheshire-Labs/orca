"""End-to-end: a standalone method submitted via runtime.submit_method runs
to completion in sim, and the run-mode gate is enforced.

Distinct from test_standalone_executor, which drives StandaloneMethodExecutor
directly and never touches the submission API. This exercises the public
submit_method path: resolving the method from its parent workflow's bundle,
the run-mode gate, the synthetic one-method workflow, and real execution
through the runtime to a terminal state. That path is what the daemon REST /
CLI and the hosted MCP and REST mirror call, so the gate (mode is required) and the
run-to-completion must hold here, not just at the executor.

The workflow is built through the @orca.workflow decorator (the real
authoring path) so its bundled_methods is populated; submit_method resolves
the method by (workflow_name, method_name) from that bundle.
"""

import asyncio
from collections.abc import AsyncGenerator

import pytest

import orca.orca as orca
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.resource_pool import ResourcePool
from orca.state.ops_store import SYSTEM_ID
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.runtime_interface import (
    RunModeRequiredError,
    StartLocationsOccupiedError,
)
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import Topology, build_system
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext

from tests.daemon.daemon_test_fixture_topology import build_topology, build_workflow
from tests.test_helpers import create_test_device, create_test_plate_template, create_test_transporter


_TERMINAL = {"completed", "failed", "aborted", "stopped"}


async def _start_runtime() -> SystemRuntime:
    stores = InMemoryRuntimeStoreFactory()
    topology = build_topology(stores)
    workflow = build_workflow(topology)
    build = await build_system(
        "standalone_method_e2e", topology, stores,
        workflow=workflow, configure_logging=False,
    )
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    await runtime.start()
    return runtime


async def _start_converge_runtime() -> SystemRuntime:
    """Workflow whose method's action takes two plates as inputs, authored as
    one owner thread + one co-labware thread (the convergence shape)."""
    device = create_test_device("shaker1", site_names=["site-1", "site-2"])
    transporter = create_test_transporter("robot1", ["shaker1", "pad_a", "pad_b"])
    topology = Topology(
        locations={
            "shaker1": device,
            "pad_a": PlatePad("pad_a"),
            "pad_b": PlatePad("pad_b"),
        },
        transporters=[transporter],
        pools=[ResourcePool("shaker1", [device])],
    )
    plate_a = create_test_plate_template("plate_a")
    plate_b = create_test_plate_template("plate_b")
    pool = topology.pool("shaker1")

    @orca.action(device=pool, inputs=[plate_a, plate_b])
    async def shared_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def converge(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        del ctx
        yield shared_action

    @orca.thread(labware=plate_a, start="pad_a", end="pad_a")
    async def owner(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        del ctx
        yield converge

    @orca.thread(labware=plate_b, start="pad_b", end="pad_b")
    async def contributor(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        del ctx
        yield orca.join()

    @orca.workflow(name="converge_wf")
    def converge_wf(wf: WorkflowContext) -> None:
        wf.start(owner)
        wf.thread(contributor)

    stores = InMemoryRuntimeStoreFactory()
    build = await build_system(
        "converge_e2e", topology, stores,
        workflow=converge_wf, labwares=[plate_a, plate_b],
        configure_logging=False,
    )
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    await runtime.start()
    return runtime


async def _wait_terminal(
    runtime: SystemRuntime, execution_id: str, timeout: float = 30.0,
) -> str:
    async def _poll() -> str:
        while True:
            status = runtime.get_execution_status(execution_id)
            if status.status in _TERMINAL:
                return status.status
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(_poll(), timeout=timeout)


@pytest.mark.asyncio
async def test_submit_method_runs_to_completion_in_pure_sim() -> None:
    """submit_method synthesizes a one-method workflow and runs it to
    completion in PURE_SIM; ops_history records the method's action and the
    labware visits the device."""
    runtime = await _start_runtime()
    system = runtime.system

    record = await runtime.submit_method(
        workflow_name="simple_workflow",
        method_name="shake_method",
        labware_start={"plate_96": "pad1"},
        labware_end={"plate_96": "pad1"},
        mode=WorkflowRunMode.PURE_SIM,
    )

    final = await _wait_terminal(runtime, record.id)
    await runtime.shutdown(confirm=True)

    assert final == "completed", f"standalone method did not complete: {final}"
    assert record.workflow_name.startswith(
        "standalone-method:simple_workflow.shake_method"
    )

    records = await system.ops_history.for_execution(record.id).records()
    action_records = [r for r in records if r.thread_id != SYSTEM_ID]
    assert action_records, (
        "standalone method run emitted no action ops_history records; "
        "the synthesized workflow never executed the method's action"
    )

    plate = next(lw for lw in system.labwares if lw.template_name == "plate_96")
    journey = [
        loc.name
        for loc in system.labware_location_service.get_history(plate).get_history()
    ]
    assert journey[0] == "pad1", journey
    assert "shaker1/slot" in journey, f"plate never visited the device: {journey}"
    assert journey[-1] == "pad1", journey


@pytest.mark.asyncio
async def test_submit_method_requires_run_mode() -> None:
    """submit_method with mode=None raises RunModeRequiredError. This is the
    gate the hosted MCP and REST mirror skipped by never passing a mode, which made
    every standalone method call fail until run_mode was threaded through."""
    runtime = await _start_runtime()
    try:
        with pytest.raises(RunModeRequiredError):
            await runtime.submit_method(
                workflow_name="simple_workflow",
                method_name="shake_method",
                labware_start={"plate_96": "pad1"},
                labware_end={"plate_96": "pad1"},
                mode=None,
            )
    finally:
        await runtime.shutdown(confirm=True)


@pytest.mark.asyncio
async def test_submit_method_converges_multiple_labware() -> None:
    """Two plates converge on one shared method execution. The first labware
    owns the method; the rest register as co-labware threads the method's slot
    pulls in. Without that auto-spawn registration the contributors yield
    orca.join() with no method to resolve and hang."""
    runtime = await _start_converge_runtime()
    system = runtime.system

    record = await runtime.submit_method(
        workflow_name="converge_wf",
        method_name="converge",
        labware_start={"plate_a": "pad_a", "plate_b": "pad_b"},
        labware_end={"plate_a": "pad_a", "plate_b": "pad_b"},
        mode=WorkflowRunMode.PURE_SIM,
    )

    final = await _wait_terminal(runtime, record.id)
    await runtime.shutdown(confirm=True)

    assert final == "completed", f"convergence did not complete: {final}"
    for template_name, pad in (("plate_a", "pad_a"), ("plate_b", "pad_b")):
        plate = next(
            lw for lw in system.labwares if lw.template_name == template_name
        )
        journey = [
            loc.name
            for loc in system.labware_location_service.get_history(plate).get_history()
        ]
        assert any(
            loc.startswith("shaker1/") for loc in journey
        ), f"{template_name} never converged: {journey}"
        assert journey[-1] == pad, journey


@pytest.mark.asyncio
async def test_submit_method_refuses_an_occupied_start_location() -> None:
    """A standalone run brings its own labware, so it meets the same
    start-location check a workflow submission does. The refusal names the
    slot and what is standing on it, which is the whole of what the operator
    needs to clear it."""
    runtime = await _start_runtime()
    try:
        pad1 = runtime.system.system_map.get_location("pad1")
        leftover = LabwareInstance("plate_96", "96_well")
        runtime.system.add_labware(leftover)
        pad1.initialize_labware(leftover)

        with pytest.raises(StartLocationsOccupiedError) as exc_info:
            await runtime.submit_method(
                workflow_name="simple_workflow",
                method_name="shake_method",
                labware_start={"plate_96": "pad1"},
                labware_end={"plate_96": "pad1"},
                mode=WorkflowRunMode.PURE_SIM,
            )

        occupied = exc_info.value.occupied
        assert [slot.position_id for slot in occupied] == ["pad1"]
        assert occupied[0].existing_labware_name == leftover.name
        assert occupied[0].existing_template_name == "plate_96"
    finally:
        await runtime.shutdown(confirm=True)


@pytest.mark.asyncio
async def test_every_start_location_is_checked_not_just_the_first() -> None:
    """A method routes its own labware to every start location it names, so an
    occupied slot under any of them is refused at submit.

    Only the first thread of a synthesized method workflow is an entry thread;
    the rest are auto-spawned. Checking entry threads alone let an occupied slot
    under thread 2..N through, and it surfaced later as a run that never moved,
    because the spawn retries DeviceBusyError until the stall detector fires.
    """
    runtime = await _start_converge_runtime()
    try:
        pad_b = runtime.system.system_map.get_location("pad_b")
        leftover = LabwareInstance("plate_b", "96_well")
        runtime.system.add_labware(leftover)
        pad_b.initialize_labware(leftover)
        assert pad_b.labware is leftover, "the slot under thread 2 is not occupied"

        with pytest.raises(StartLocationsOccupiedError) as caught:
            await runtime.submit_method(
                workflow_name="converge_wf",
                method_name="converge",
                labware_start={"plate_a": "pad_a", "plate_b": "pad_b"},
                labware_end={"plate_a": "pad_a", "plate_b": "pad_b"},
                mode=WorkflowRunMode.PURE_SIM,
            )

        refused = {slot.position_id for slot in caught.value.occupied}
        assert "pad_b" in refused, (
            "the refusal did not name the slot that is actually occupied"
        )
    finally:
        await runtime.shutdown(confirm=True)

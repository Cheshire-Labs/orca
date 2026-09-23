"""A device that fails to come up leaves a record an operator can find.

On the 2026-08-21 bench campaign an execution went straight to `failed` with an
empty incidents table. The lazy first-execution bring-up raised the driver's own
error and nothing declared an incident, so `orca incident list` had nothing to
show and the operator was left reading an execution error string that did not
even name the device. `IncidentCategory.DEVICE_INIT_FAILED` and
`DeviceInitFailedDetail` already existed and were DB-mapped; nothing ever
recorded one.
"""

import asyncio
import contextlib
from collections.abc import AsyncGenerator

import orca.orca as orca
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.incident_store import (
    DeviceInitFailedDetail,
    IncidentCategory,
    RecoveryAction,
)
from orca.runtime.registries import NullGatewayRegistry
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.errors import DeviceInitializationError
from orca.system.system_interface import ISystem
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext

from tests.mock import UniversalMockDevice, UniversalSimDriver
from tests.runtime.test_lazy_init_and_resolver import CountingInitDriver
from tests.runtime.manual_place_fixtures import (
    _live_connection_source,
)
from tests.test_helpers import (
    create_test_plate_template,
    create_test_transporter,
    wire_system_map,
)

# Deliberately does not name the device: what the runtime adds is the point.
INIT_ERROR = "controller did not answer"


class AlwaysFailsInitDriver(UniversalSimDriver):
    """Bring-up never succeeds, the way a powered-down device behaves."""

    async def initialize(self) -> None:
        raise RuntimeError(INIT_ERROR)


async def _system_with(workflow_name: str, shaker: UniversalMockDevice) -> ISystem:
    """One shaker on a pad, one thread that shakes and comes back."""
    plate = create_test_plate_template(f"plate_{workflow_name}")
    registry = ResourceRegistry()
    registry.add_resource(shaker)
    registry.add_resource(create_test_transporter("robot1", ["shaker1", "pad1"]))
    registry.add_resource_pool(ResourcePool("shaker1", [shaker]))
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": shaker}, pads=["pad1"])

    @orca.action(device=registry.get_resource_pool("shaker1"), inputs=[plate])
    async def shake_action(ctx: ActionContext) -> None:
        del ctx

    @orca.method
    async def shake_method(
        ctx: MethodContext,
    ) -> AsyncGenerator[ActionTemplate, None]:
        del ctx
        yield shake_action

    pad = system_map.get_location("pad1")

    @orca.thread(labware=plate, start=pad, end=pad)
    async def plate_thread(
        ctx: ThreadContext,
    ) -> AsyncGenerator[MethodTemplate, None]:
        del ctx
        yield shake_method

    workflow = WorkflowTemplate(workflow_name)
    workflow.add_thread(plate_thread, is_start=True)

    builder = SdkToSystemBuilder(
        name=f"{workflow_name}_system", description="",
        labwares=[plate], resources_registry=registry,
        system_map=system_map, workflows=[workflow], event_bus=EventBus(),
    )
    await builder.bind_labwares()
    return builder.get_system()


def _runtime_over(system: ISystem) -> SystemRuntime:
    return SystemRuntime(
        system,
        gateway_registry=NullGatewayRegistry(),
        connection_source=_live_connection_source(),
    )


async def _wait_for_bringup(driver: CountingInitDriver) -> None:
    """Wait until the device has actually been brought up."""
    for _ in range(200):
        if driver.init_calls:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the device was never initialized")


async def _run_to_completion(runtime: SystemRuntime, execution_id: str) -> None:
    """Wait out the execution however it ends. A bring-up failure raises out of
    the execution task, and these tests are about what it leaves behind."""
    with contextlib.suppress(Exception):
        await runtime._executions[execution_id].task


class TestTheTypedError:
    def test_stays_a_runtime_error(self) -> None:
        """Bring-up failures were RuntimeErrors before they were typed, and
        callers catch them that way; narrowing the base would drop them."""
        cause = ValueError("no answer")
        error = DeviceInitializationError("shaker1", cause)
        assert isinstance(error, RuntimeError)
        assert error.device_name == "shaker1"
        assert error.cause is cause
        assert "shaker1" in str(error)
        assert "no answer" in str(error)


class TestBringUpFailureIsDeclared:
    async def test_a_device_that_will_not_come_up_records_an_incident(self) -> None:
        """The bench failure, as a test: the incidents table was empty."""
        shaker = UniversalMockDevice("shaker1", driver=AlwaysFailsInitDriver("shaker1"))
        runtime = _runtime_over(await _system_with("wf_init_fail", shaker))
        await runtime.start()
        try:
            record = await runtime.submit_workflow(
                "wf_init_fail", mode=WorkflowRunMode.LIVE,
            )
            await _run_to_completion(runtime, record.id)
            incidents = await runtime.incidents.list(
                category=IncidentCategory.DEVICE_INIT_FAILED,
            )
            assert len(incidents) == 1, (
                "a failed bring-up must leave one queryable record; "
                f"found {len(incidents)}"
            )
            incident = incidents[0]
            assert isinstance(incident.detail, DeviceInitFailedDetail)
            assert incident.detail.device_name == "shaker1", (
                "the incident has to name the device that failed, or the "
                "operator cannot tell which one to go look at"
            )
            assert INIT_ERROR in incident.detail.driver_error
            assert incident.execution_id == record.id, (
                "the incident must link to the execution it killed"
            )
            assert incident.recovery_action is RecoveryAction.RESTART_EXECUTION
        finally:
            await runtime.shutdown()

    async def test_the_execution_still_fails(self) -> None:
        """Recording the incident must not swallow the failure."""
        shaker = UniversalMockDevice("shaker1", driver=AlwaysFailsInitDriver("shaker1"))
        runtime = _runtime_over(await _system_with("wf_init_fail_phase", shaker))
        await runtime.start()
        try:
            record = await runtime.submit_workflow(
                "wf_init_fail_phase", mode=WorkflowRunMode.LIVE,
            )
            await _run_to_completion(runtime, record.id)
            execution = runtime._executions[record.id]
            assert execution.phase.name == "FAILED"
            assert "shaker1" in str(execution.error), (
                "the execution error has to name the device too, since that is "
                f"what a client reads first; got {execution.error!r}"
            )
        finally:
            await runtime.shutdown()

    async def test_a_healthy_bring_up_records_nothing(self) -> None:
        """Negative control: a device that comes up leaves no incident.

        This one waits for the bring-up rather than for the run, because a LIVE
        thread starting on a pad parks for an operator to place the labware and
        would never finish on its own.
        """
        driver = CountingInitDriver("shaker1")
        shaker = UniversalMockDevice("shaker1", driver=driver)
        runtime = _runtime_over(await _system_with("wf_init_ok", shaker))
        await runtime.start()
        try:
            await runtime.submit_workflow("wf_init_ok", mode=WorkflowRunMode.LIVE)
            await _wait_for_bringup(driver)
            incidents = await runtime.incidents.list(
                category=IncidentCategory.DEVICE_INIT_FAILED,
            )
            assert incidents == []
        finally:
            await runtime.shutdown()

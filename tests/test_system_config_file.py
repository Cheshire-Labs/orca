"""`system_config.json` in the deployment package sets the system's settings; a missing file or key keeps the default."""
import asyncio
from collections.abc import AsyncGenerator

import pytest
from cheshire_drivers import CartesianCoordinates as C
from cheshire_drivers import Teachpoint

import orca.orca as orca
from orca.config import OrcaConfig, ReservationConfig
from orca.runtime.setup_files import SetupFileError
from orca.devices.shaker import Shaker
from orca.events.event_bus import EventBus
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.resource_pool import ResourcePool
from orca.resource_models.transporter import Transporter
from orca.runtime.startup import PackageSetupStoreFactory
from orca.sdk.build import Topology
from orca.sdk.labware import PlateTemplate
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import ActionTemplate, IMethodTemplate, MethodTemplate
from orca.runtime.system_runtime import SystemRuntime
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.memory_activity import memory_ops_store, memory_runtime
from tests.setup_package import package_stores, with_teachpoints, write_teachpoints
from tests.test_helpers import wait_until

PLATE_TYPE = "Cor_Falcon_96_wellplate_340ul_Fb_Black"


def _write_system_config(stores: PackageSetupStoreFactory, text: str) -> None:
    (stores.root / "system_config.json").write_text(text, encoding="utf-8")


def test_a_package_with_no_system_config_gets_the_defaults() -> None:
    assert package_stores().system_config() == OrcaConfig()


def test_a_setting_in_the_file_is_used_and_the_rest_keep_their_defaults() -> None:
    stores = package_stores()
    _write_system_config(stores, '{"reservation": {"action_reservation_timeout": 5.0}}')

    config = stores.system_config()

    assert config.reservation.action_reservation_timeout == 5.0
    assert config.reservation.move_reservation_timeout == ReservationConfig().move_reservation_timeout
    assert config.coordination == OrcaConfig().coordination


def test_a_misspelled_setting_is_refused_and_names_the_file() -> None:
    stores = package_stores()
    _write_system_config(stores, '{"reservation": {"action_reservation_timout": 5.0}}')

    with pytest.raises(SetupFileError, match="system_config.json"):
        stores.system_config()


async def test_a_pool_wait_times_out_into_a_pause_when_the_file_sets_a_limit() -> None:
    stores = package_stores()
    _write_system_config(stores, '{"reservation": {"action_reservation_timeout": 1.0}}')
    shaker = Shaker("shaker_1")
    pool = ResourcePool("shakers", [shaker])
    arm = with_teachpoints(Transporter("arm"), write_teachpoints(stores, "arm", [
        Teachpoint(position, C(100.0 * i, 200, 300, 0, 90, 180), orientation="right")
        for i, position in enumerate(["pad_1", "pad_2", "shaker_1"])
    ]))
    topology = Topology(
        locations={"shaker_1": shaker, "pad_1": PlatePad("pad_1"), "pad_2": PlatePad("pad_2")},
        transporters=[arm], pools=[pool],
    )
    first, second = PlateTemplate("first", labware_type=PLATE_TYPE), PlateTemplate("second", labware_type=PLATE_TYPE)
    released = asyncio.Event()

    @orca.action(device=pool, inputs=[first])
    async def hold_the_shaker(ctx: ActionContext) -> None:
        await released.wait()

    @orca.action(device=pool, inputs=[second])
    async def shake(ctx: ActionContext) -> None:
        await released.wait()

    @orca.method
    async def first_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield hold_the_shaker

    @orca.method
    async def second_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake

    @orca.thread(labware=first, start="pad_1", end="pad_1")
    async def first_thread(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        yield first_method

    @orca.thread(labware=second, start="pad_2", end="pad_2")
    async def second_thread(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        yield second_method

    @orca.workflow(name="one_shaker")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(first_thread)
        wf.start(second_thread)

    build = await orca.build_system(
        name="one shaker", workflow=workflow, topology=topology, stores=stores,
        event_bus=EventBus(), ops_history_store=memory_ops_store(),
    )
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        assert build.workflow is not None
        submission = await runtime.submit(build.workflow, mode=WorkflowRunMode.SIM)

        def the_waiting_plate_paused() -> bool:
            return "PAUSED" in [t.status for t in runtime.list_threads(submission.execution_id)]

        await wait_until(the_waiting_plate_paused, timeout=30.0, message="the pool wait never timed out")
        assert [t.status for t in runtime.list_threads(submission.execution_id)].count("PAUSED") == 1
    finally:
        released.set()
        await runtime.shutdown(confirm=True)


async def _a_run_waiting_for_an_event(stores: PackageSetupStoreFactory, step_timeout: float | None) -> tuple[SystemRuntime, str]:
    """A plate that shakes, then waits for an event nothing emits, with `step_timeout` as its own timeout."""
    shaker = Shaker("shaker_1")
    pool = ResourcePool("shakers", [shaker])
    arm = with_teachpoints(Transporter("arm"), write_teachpoints(stores, "arm", [
        Teachpoint(position, C(100.0 * i, 200, 300, 0, 90, 180), orientation="right")
        for i, position in enumerate(["pad_1", "shaker_1"])
    ]))
    topology = Topology(locations={"shaker_1": shaker, "pad_1": PlatePad("pad_1")}, transporters=[arm], pools=[pool])
    plate = PlateTemplate("plate", labware_type=PLATE_TYPE)

    @orca.action(device=pool, inputs=[plate])
    async def shake(ctx: ActionContext) -> None:
        return None

    @orca.method
    async def shake_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake

    @orca.thread(labware=plate, start="pad_1", end="pad_1")
    async def waiting(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield shake_method
        yield orca.on("never_emitted", timeout=step_timeout)

    @orca.workflow(name="waits")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(waiting)

    build = await orca.build_system(
        name="waits", workflow=workflow, topology=topology, stores=stores,
        event_bus=EventBus(), ops_history_store=memory_ops_store(),
    )
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    await runtime.start()
    assert build.workflow is not None
    submission = await runtime.submit(build.workflow, mode=WorkflowRunMode.SIM)
    return runtime, submission.execution_id


def _paused(runtime: SystemRuntime, execution_id: str) -> bool:
    return [t.status for t in runtime.list_threads(execution_id)] == ["PAUSED"]


async def test_an_event_step_with_no_timeout_of_its_own_pauses_after_the_files_event_timeout() -> None:
    stores = package_stores()
    _write_system_config(stores, '{"coordination": {"event_timeout": 0.5}}')
    runtime, execution_id = await _a_run_waiting_for_an_event(stores, step_timeout=None)
    try:
        await wait_until(lambda: _paused(runtime, execution_id), timeout=30.0, message="the event wait never timed out")
    finally:
        await runtime.shutdown(confirm=True)


async def test_an_event_step_s_own_timeout_wins_over_the_files_event_timeout() -> None:
    stores = package_stores()
    _write_system_config(stores, '{"coordination": {"event_timeout": 3600}}')
    runtime, execution_id = await _a_run_waiting_for_an_event(stores, step_timeout=0.5)
    try:
        await wait_until(lambda: _paused(runtime, execution_id), timeout=30.0, message="the step's own timeout was ignored")
    finally:
        await runtime.shutdown(confirm=True)

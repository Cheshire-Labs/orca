"""A run's bring-up covers every device and every transporter (owner, 2026-11-13), and moves nothing.

It used to bring up only the movers on a route the workflow declares, so an arm with no orca-client failed at its
first pick instead of refusing the run at its start. Bring-up asks for no motion, so bringing
up an arm the run never uses does not drive it.
"""

from orca.config import OrcaConfig

import pytest

from orca.events.event_bus import EventBus
from orca.resource_models.devices import Device
from orca.resource_models.labware import PlateTemplate
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import SystemRuntime
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.resource_models.resource_registry import ResourceRegistry
from orca.system.system_map import SystemMap

from tests.test_helpers import (
    create_test_plate_template,
    create_test_transporter,
    seeded,
    wire_system_map,
)
from tests.runtime.test_lazy_init_and_resolver import (
    CountingInitDriver,
    UniversalMockDevice,
)
from tests.memory_activity import memory_ops_store, memory_runtime, memory_variable_store
from tests.memory_activity import memory_unsaved_positions
from tests.labware_catalog import bundled_labware_catalog
from tests.memory_activity import memory_labware_service

pytestmark = pytest.mark.asyncio


class CountingMoves:
    """Records the motion a transporter bring-up actually performs."""

    def __init__(self) -> None:
        self.initialize_calls = 0
        self.move_to_safe_calls = 0


def _spy_on_bringup(transporter, spy: CountingMoves) -> None:
    """Wrap the driver's bring-up calls so a test can see the arm move."""
    driver = transporter.driver
    real_initialize = driver.initialize
    real_move_to_safe = driver.move_to_safe

    async def initialize(request):
        spy.initialize_calls += 1
        return await real_initialize(request)

    async def move_to_safe(request):
        spy.move_to_safe_calls += 1
        return await real_move_to_safe(request)

    driver.initialize = initialize
    driver.move_to_safe = move_to_safe


async def _one_device_one_arm(
    plate: PlateTemplate,
) -> tuple[SystemRuntime, CountingInitDriver, CountingMoves]:
    """A shaker on a pad, an arm that can reach both, nothing else."""
    shaker_driver = CountingInitDriver("shaker1")
    shaker: Device = UniversalMockDevice("shaker1", driver=shaker_driver)
    transporter = create_test_transporter("robot1", ["pad1", "shaker1"])
    arm_spy = CountingMoves()
    _spy_on_bringup(transporter, arm_spy)

    registry = ResourceRegistry()
    registry.add_resource(shaker)
    registry.add_resource(transporter)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": shaker}, pads=["pad1"])
    builder = SdkToSystemBuilder(
        name="scoped_sys", description="",
        labwares=[plate], resources_registry=registry,
        system_map=system_map, workflows=[], event_bus=EventBus(), config=OrcaConfig(), ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(),
        unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    runtime = await memory_runtime(builder.get_system(), event_bus=EventBus())
    return runtime, shaker_driver, arm_spy


class TestBringUpCoversEveryTransporter:
    async def test_a_run_that_never_moves_labware_brings_up_the_arm_without_moving_it(self) -> None:
        plate = create_test_plate_template("plate_96")
        runtime, shaker_driver, arm_spy = await _one_device_one_arm(plate)
        try:
            await runtime.start()
            with seeded(WorkflowRunMode.LIVE):
                await runtime.system.ensure_runtime_initialized()
            assert shaker_driver.init_calls == 1
            assert arm_spy.initialize_calls == 1, (
                "every transporter comes up at a run's start; "
                f"observed {arm_spy.initialize_calls} initialize dispatches"
            )
            assert arm_spy.move_to_safe_calls == 0, (
                "bring-up drove the arm; "
                f"observed {arm_spy.move_to_safe_calls} move_to_safe dispatches"
            )
        finally:
            await runtime.shutdown()

    async def test_a_second_entry_under_the_same_mode_brings_up_nothing_again(self) -> None:
        plate = create_test_plate_template("plate_96")
        runtime, shaker_driver, arm_spy = await _one_device_one_arm(plate)
        try:
            await runtime.start()
            with seeded(WorkflowRunMode.LIVE):
                await runtime.system.ensure_runtime_initialized()
                await runtime.system.ensure_runtime_initialized()
            assert (shaker_driver.init_calls, arm_spy.initialize_calls) == (1, 1)
        finally:
            await runtime.shutdown()

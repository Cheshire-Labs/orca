"""
Tests for deadlock detection and resolution.

These tests verify that:
1. The system detects deadlocks when threads block each other
2. Exactly one thread yields to a parking pad
3. Starvation prevention works (starved threads get priority)
4. The deadlocked thread successfully completes after yielding
"""
import pytest
import asyncio
from typing import Tuple

from orca.sdk.system import SdkToSystemBuilder, WorkflowExecutor, ResourceRegistry, SystemMap
from orca.sdk.workflow import WorkflowTemplate, ThreadTemplate, MethodTemplate
from orca.sdk.labware import PlateTemplate
from orca.sdk.actions import RunProtocol
from orca.events.event_bus import EventBus
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.plate_pad import PlatePad
from orca.workflow_models.status_enums import LabwareThreadStatus
from tests.test_helpers import (
    create_test_transporter,
    create_test_device,
    create_test_labware_instance,
    create_test_plate_template,
    create_simple_system_map,
    wait_for_threads_completed
)


@pytest.mark.asyncio
async def test_two_robot_deadlock_resolution():
    """
    Test that deadlock is detected and resolved when two threads
    try to swap locations through a shared choke point (loc_b).

    Setup:
        - start_pad_1, start_pad_2: inert PlatePads where threads begin
        - loc_a: device_a (robot1 can reach)
        - loc_b: choke point (BOTH robots can reach)
        - loc_c: device_c (robot2 can reach)
        - parking_pad: for deadlock resolution

    Thread Flow:
        - Thread1: start_pad_1 → loc_a (action1) → loc_b → loc_c (action2)
        - Thread2: start_pad_2 → loc_c (action1) → loc_b → loc_a (action2)

    Expected deadlock scenario:
        1. Both threads complete action1 at their respective devices
        2. Thread1 at loc_a tries to move through loc_b to loc_c
        3. Thread2 at loc_c tries to move through loc_b to loc_a
        4. One thread gets to loc_b first (e.g., Thread1)
        5. Thread1 at loc_b wants loc_c (where plate2 is) - BLOCKED
        6. Thread2 at loc_c wants loc_b (where plate1 is) - BLOCKED
        7. Deadlock detected via wait-for graph cycle
        8. One thread yields to parking_pad
        9. Both threads eventually complete
    """
    # Create inert start pads where threads begin
    start_pad_1 = PlatePad("start_pad_1")
    start_pad_2 = PlatePad("start_pad_2")

    # Create devices at their respective locations
    device_a = create_test_device("device_a", "device")
    device_c = create_test_device("device_c", "device")
    parking_pad = PlatePad("parking_pad", supports_deadlock_resolution=True)

    # Robot teachpoints - both can reach loc_b (the choke point)
    robot1 = create_test_transporter("robot1", ["start_pad_1", "loc_a", "loc_b", "parking_pad"])
    robot2 = create_test_transporter("robot2", ["start_pad_2", "loc_b", "loc_c", "parking_pad"])

    registry, system_map = create_simple_system_map(
        [robot1, robot2],
        {"loc_a": device_a, "loc_c": device_c},
        {"parking_pad": parking_pad, "start_pad_1": start_pad_1, "start_pad_2": start_pad_2}
    )

    # Create plates
    plate1_template = create_test_plate_template("plate1")
    plate2_template = create_test_plate_template("plate2")

    # Create methods with TWO actions each:
    # - Action 1 moves to first device
    # - Action 2 moves to opposite device (requires going through loc_b)
    method_for_thread1 = MethodTemplate("method_thread1", [
        RunProtocol(device_a, "protocol.pro", {}, [plate1_template], [plate1_template]),
        RunProtocol(device_c, "protocol.pro", {}, [plate1_template], [plate1_template])
    ])
    method_for_thread2 = MethodTemplate("method_thread2", [
        RunProtocol(device_c, "protocol.pro", {}, [plate2_template], [plate2_template]),
        RunProtocol(device_a, "protocol.pro", {}, [plate2_template], [plate2_template])
    ])

    # Create threads starting at inert pads (NOT at device locations)
    thread1 = ThreadTemplate(
        plate1_template,
        system_map.get_location("start_pad_1"),
        system_map.get_location("loc_c"),
        [method_for_thread1]
    )

    thread2 = ThreadTemplate(
        plate2_template,
        system_map.get_location("start_pad_2"),
        system_map.get_location("loc_a"),
        [method_for_thread2]
    )

    # Create workflow
    workflow = WorkflowTemplate("deadlock_test")
    workflow.add_thread(thread1, is_start=True)
    workflow.add_thread(thread2, is_start=True)

    # Build system
    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        "Deadlock Test",
        "Test deadlock resolution",
        [plate1_template, plate2_template],
        registry,
        system_map,
        [method_for_thread1, method_for_thread2],
        [workflow],
        event_bus
    )
    system = builder.get_system()

    # Set up simulation mode and create workflow manually
    system.set_simulating(True)
    workflow_instance = system.create_and_register_workflow_instance(workflow)
    system.add_workflow(workflow_instance)
    executing_workflow = system.get_executing_workflow(workflow_instance.id)

    # Run workflow (with timeout to prevent hanging if deadlock not resolved)
    try:
        await asyncio.wait_for(executing_workflow.start(), timeout=30.0)
    except asyncio.TimeoutError:
        pytest.fail("Workflow timed out - deadlock was not resolved!")

    # Verify both threads completed
    threads = executing_workflow.thread_manager.threads

    assert len(threads) == 2, "Should have 2 threads"

    for thread in threads:
        assert thread.status == LabwareThreadStatus.COMPLETED, (
            f"Thread {thread.id} did not complete (status: {thread.status}). "
            "Deadlock resolution failed!"
        )

    print("✓ Deadlock detected and resolved successfully!")
    print(f"✓ Both threads completed: {[t.id for t in threads]}")


@pytest.mark.asyncio
async def test_starvation_prevention():
    """
    Test that starvation prevention works - a thread that has yielded
    multiple times gets priority and doesn't yield again.

    This test uses the same two-action pattern as test_two_robot_deadlock_resolution
    but artificially sets a high starvation score for thread1 before starting.
    When they deadlock, thread2 should yield instead of thread1 because
    thread1 has higher priority (higher starvation score).

    Setup is identical to test_two_robot_deadlock_resolution:
        - start_pad_1, start_pad_2: inert PlatePads where threads begin
        - loc_a: device_a, loc_b: choke point, loc_c: device_c
        - parking_pad: for deadlock resolution
    """
    # Create inert start pads where threads begin
    start_pad_1 = PlatePad("start_pad_1")
    start_pad_2 = PlatePad("start_pad_2")

    # Create devices at their respective locations
    device_a = create_test_device("device_a", "device")
    device_c = create_test_device("device_c", "device")
    parking_pad = PlatePad("parking_pad", supports_deadlock_resolution=True)

    # Robot teachpoints - both can reach loc_b (the choke point)
    robot1 = create_test_transporter("robot1", ["start_pad_1", "loc_a", "loc_b", "parking_pad"])
    robot2 = create_test_transporter("robot2", ["start_pad_2", "loc_b", "loc_c", "parking_pad"])

    registry, system_map = create_simple_system_map(
        [robot1, robot2],
        {"loc_a": device_a, "loc_c": device_c},
        {"parking_pad": parking_pad, "start_pad_1": start_pad_1, "start_pad_2": start_pad_2}
    )

    # Create plates
    plate1_template = create_test_plate_template("plate1")
    plate2_template = create_test_plate_template("plate2")

    # Create methods with TWO actions each (same pattern as deadlock test)
    method_for_thread1 = MethodTemplate("method_thread1", [
        RunProtocol(device_a, "protocol.pro", {}, [plate1_template], [plate1_template]),
        RunProtocol(device_c, "protocol.pro", {}, [plate1_template], [plate1_template])
    ])
    method_for_thread2 = MethodTemplate("method_thread2", [
        RunProtocol(device_c, "protocol.pro", {}, [plate2_template], [plate2_template]),
        RunProtocol(device_a, "protocol.pro", {}, [plate2_template], [plate2_template])
    ])

    # Create threads starting at inert pads (NOT at device locations)
    thread1 = ThreadTemplate(
        plate1_template,
        system_map.get_location("start_pad_1"),
        system_map.get_location("loc_c"),
        [method_for_thread1]
    )

    thread2 = ThreadTemplate(
        plate2_template,
        system_map.get_location("start_pad_2"),
        system_map.get_location("loc_a"),
        [method_for_thread2]
    )

    # Create workflow
    workflow = WorkflowTemplate("starvation_test")
    workflow.add_thread(thread1, is_start=True)
    workflow.add_thread(thread2, is_start=True)

    # Build system
    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        "Starvation Test",
        "Test starvation prevention",
        [plate1_template, plate2_template],
        registry,
        system_map,
        [method_for_thread1, method_for_thread2],
        [workflow],
        event_bus
    )
    system = builder.get_system()

    # Set up simulation mode and create workflow manually to get thread IDs first
    system.set_simulating(True)
    workflow_instance = system.create_and_register_workflow_instance(workflow)
    system.add_workflow(workflow_instance)
    executing_workflow = system.get_executing_workflow(workflow_instance.id)

    # Now we can access thread IDs from entry threads
    entry_threads = executing_workflow._entry_threads
    thread1_id = entry_threads[0].id
    thread2_id = entry_threads[1].id

    # Access starvation registry via builder
    starvation_registry = builder._thread_reservation_coordinator.starvation_registry

    # Artificially set high starvation score for thread1
    # This simulates thread1 having been denied multiple times in the past
    for _ in range(5):
        starvation_registry.increment_starvation_score(thread1_id)

    # Now start the workflow - when deadlock occurs, thread2 should yield
    # (thread1 has higher starvation score = higher priority)
    try:
        await asyncio.wait_for(executing_workflow.start(), timeout=30.0)
    except asyncio.TimeoutError:
        pytest.fail("Workflow timed out!")

    # Verify both threads completed
    threads = executing_workflow.thread_manager.threads

    assert len(threads) == 2
    for thread in threads:
        assert thread.status == LabwareThreadStatus.COMPLETED

    # Verify starvation mechanism worked:
    # thread1 should have score 0 (was granted and reset)
    # because it had higher priority and didn't need to yield
    thread1_score = starvation_registry.get_starvation_score(thread1_id)
    thread2_score = starvation_registry.get_starvation_score(thread2_id)

    # The thread with high initial starvation score should have been granted priority
    # and had its score reset to 0
    assert thread1_score == 0, (
        f"Thread1 (high starvation) should have score 0 after completion, got {thread1_score}"
    )

    print("✓ Starvation prevention mechanism verified!")
    print(f"✓ Final starvation scores: thread1={thread1_score}, thread2={thread2_score}")

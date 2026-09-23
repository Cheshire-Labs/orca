"""A latched device fault must be observable, not just readable.

Before this the fault was state on the device row and nothing else: no event,
so no UI could show one arriving, the intervention long-poll could not return
one, and the archive had no record that a device stopped the lab. An operator
found out when the next thread refused.
"""

import pytest

from orca.events.execution_context import DeviceFaultContext
from orca.events.intervention import InterventionKind, classify_intervention
from orca.events.runtime_event import RuntimeEvent
from orca.gateway.controller.controller import DeviceController
from orca.runtime.system_runtime import SystemRuntime
from orca.gateway.device_fault import (
    DeviceFault,
    DeviceFaultOutcome,
    fault_from_error,
)


def a_fault(device_id: str = "pf400_1") -> DeviceFault:
    return fault_from_error(
        device_id=device_id,
        command="initialize",
        command_id="cmd-1",
        error=OSError("[WinError 1236] The network connection was aborted"),
        outcome=DeviceFaultOutcome.FAILED,
        execution_id="exec-1",
    )


def fault_event(*, cleared: bool, device_name: str = "pf400_1") -> RuntimeEvent:
    status = "FAULT_CLEARED" if cleared else "FAULTED"
    return RuntimeEvent(
        event_name=f"DEVICE.{device_name}.{status}",
        execution_id="exec-1",
        timestamp=0.0,
        entity_type="DEVICE",
        entity_id=device_name,
        status=status,
        context=DeviceFaultContext(device_name=device_name, cleared=cleared),
    )


@pytest.mark.asyncio
async def test_latching_a_fault_tells_the_listener():
    controller = DeviceController()
    seen: list[tuple[str, DeviceFault | None]] = []
    controller.set_fault_listener(lambda name, fault: seen.append((name, fault)))

    await controller._latch_fault(
        "pf400_1", "initialize", "cmd-1",
        OSError("dropped"), DeviceFaultOutcome.FAILED, "exec-1",
    )

    assert len(seen) == 1
    name, fault = seen[0]
    assert name == "pf400_1"
    assert fault is not None
    assert fault.command == "initialize"


@pytest.mark.asyncio
async def test_only_the_first_fault_is_announced():
    """The first fault is the one kept, so it is the only one worth saying."""
    controller = DeviceController()
    seen: list[tuple[str, DeviceFault | None]] = []
    controller.set_fault_listener(lambda name, fault: seen.append((name, fault)))

    for _ in range(3):
        await controller._latch_fault(
            "pf400_1", "initialize", "cmd-1",
            OSError("dropped"), DeviceFaultOutcome.FAILED, "exec-1",
        )

    assert len(seen) == 1


@pytest.mark.asyncio
async def test_clearing_a_fault_tells_the_listener_it_is_gone():
    """A surface that latched onto the fault can drop it without polling."""
    controller = DeviceController()
    await controller._latch_fault(
        "pf400_1", "initialize", "cmd-1",
        OSError("dropped"), DeviceFaultOutcome.FAILED, "exec-1",
    )
    seen: list[tuple[str, DeviceFault | None]] = []
    controller.set_fault_listener(lambda name, fault: seen.append((name, fault)))

    await controller.clear_fault("pf400_1")

    assert seen == [("pf400_1", None)]


@pytest.mark.asyncio
async def test_clearing_a_device_with_no_fault_announces_nothing():
    controller = DeviceController()
    seen: list[tuple[str, DeviceFault | None]] = []
    controller.set_fault_listener(lambda name, fault: seen.append((name, fault)))

    await controller.clear_fault("pf400_1")

    assert seen == []


@pytest.mark.asyncio
async def test_a_listener_that_raises_never_reaches_the_command_path():
    controller = DeviceController()

    def explode(name: str, fault: DeviceFault | None) -> None:
        raise RuntimeError("observer is broken")

    controller.set_fault_listener(explode)

    await controller._latch_fault(
        "pf400_1", "initialize", "cmd-1",
        OSError("dropped"), DeviceFaultOutcome.FAILED, "exec-1",
    )

    assert controller.fault("pf400_1") is not None


def test_a_new_fault_is_an_intervention():
    assert classify_intervention(fault_event(cleared=False)) is (
        InterventionKind.DEVICE_FAULT
    )


def test_a_cleared_fault_is_not_an_intervention():
    """Nothing is waiting on a person once the fault is gone."""
    assert classify_intervention(fault_event(cleared=True)) is None


class _CapturingBus:
    def __init__(self) -> None:
        self.events: list[RuntimeEvent] = []

    def emit(self, event: RuntimeEvent) -> None:
        self.events.append(event)


class _RuntimeWithJustTheEmitter:
    """Only the two attributes `_on_device_fault_changed` reads."""

    def __init__(self) -> None:
        self._system_event_bus = _CapturingBus()

    _on_device_fault_changed = SystemRuntime._on_device_fault_changed


def test_the_event_the_runtime_emits_is_the_one_the_classifier_answers_to():
    """The producer and the classifier are otherwise never checked together.

    Every other test in this file builds its own event by hand, so the two
    halves could disagree and the intervention long-poll would go quiet with
    every test still green.
    """
    runtime = _RuntimeWithJustTheEmitter()
    runtime._on_device_fault_changed("pf400_1", a_fault())

    (emitted,) = runtime._system_event_bus.events
    assert classify_intervention(emitted) is InterventionKind.DEVICE_FAULT
    assert emitted.event_name == "DEVICE.pf400_1.FAULTED"
    assert emitted.execution_id == "exec-1"


def test_a_cleared_fault_is_emitted_and_is_not_an_intervention():
    runtime = _RuntimeWithJustTheEmitter()
    runtime._on_device_fault_changed("pf400_1", None)

    (emitted,) = runtime._system_event_bus.events
    assert emitted.event_name == "DEVICE.pf400_1.FAULT_CLEARED"
    assert classify_intervention(emitted) is None

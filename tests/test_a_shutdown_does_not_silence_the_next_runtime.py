"""A rebuild can start the next runtime before the old one shuts down.

The fault listener is one slot on a module-level controller. An unconditional
clear on shutdown would silence the runtime that had just taken the slot, and
device faults would stop reaching the event stream with nothing to show why.

Every listener here is a BOUND METHOD, because that is what SystemRuntime
registers and it is the case that tells `==` from `is`: each attribute access
builds a new bound-method object, so an identity check would fail to recognise
a runtime's own listener and clear a slot it no longer owns.
"""

from orca.gateway.controller.controller import DeviceController
from orca.gateway.device_fault import DeviceFault


class FakeRuntime:
    def __init__(self, label: str, seen: list[str]) -> None:
        self._label = label
        self._seen = seen

    def on_fault_changed(self, device_id: str, fault: DeviceFault | None) -> None:
        self._seen.append(f"{self._label}:{device_id}")


def test_a_bound_method_is_recognised_as_its_own_listener():
    """`self.on_fault_changed` is a fresh object each time it is read."""
    seen: list[str] = []
    runtime = FakeRuntime("only", seen)
    assert runtime.on_fault_changed is not runtime.on_fault_changed
    assert runtime.on_fault_changed == runtime.on_fault_changed

    controller = DeviceController()
    controller.set_fault_listener(runtime.on_fault_changed)
    controller.clear_fault_listener(runtime.on_fault_changed)

    controller._tell_fault_listener("pf400_1", None)
    assert seen == []


def test_the_old_runtimes_shutdown_leaves_the_new_ones_listener_alone():
    seen: list[str] = []
    old = FakeRuntime("old", seen)
    new = FakeRuntime("new", seen)

    controller = DeviceController()
    controller.set_fault_listener(old.on_fault_changed)
    controller.set_fault_listener(new.on_fault_changed)
    controller.clear_fault_listener(old.on_fault_changed)

    controller._tell_fault_listener("pf400_1", None)
    assert seen == ["new:pf400_1"], "the runtime that had just started kept the slot"

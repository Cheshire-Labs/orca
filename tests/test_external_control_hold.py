"""An operator can say "I am driving this" for longer than one command.

The gateway takes external control around each ad-hoc command and gives it
straight back, which says nothing about the gap between two of them. A workflow
move can start in that gap, which is exactly what happens while somebody is
standing at the instrument sending one command at a time.
"""

import pytest

from cheshire_drivers import RecordingLiquidHandlerDriver
from cheshire_drivers.plr import ChatterboxLiquidHandlerDriver
from orca.operations.device import (
    ReleaseDeviceControlOperation,
    TakeDeviceControlOperation,
)
from orca.operations.device_models import (
    ReleaseDeviceControlRequest,
    TakeDeviceControlRequest,
)
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.system_runtime import SystemRuntime
from tests.test_helpers import create_test_device
from tests.test_labware_state_reconciliation import _build


def test_a_per_command_release_does_not_end_an_operators_hold() -> None:
    """The regression this whole thing turns on.

    The gateway releases after every ad-hoc command. If that release cleared
    the operator's standing claim, then sending one command DURING a hold would
    hand the device straight back -- the opposite of what was asked for, and
    silently.
    """
    device = create_test_device("shaker_1")
    device.hold_external_control("swapping the plate by hand")

    device.take_external_control()
    device.release_external_control()

    assert device.under_external_control, (
        "an ad-hoc command sent during a hold gave the device back on its way out"
    )
    assert device.external_control_hold == "swapping the plate by hand"


def test_releasing_the_hold_gives_the_device_back() -> None:
    device = create_test_device("shaker_1")
    device.hold_external_control("looking at it")

    device.release_external_control_hold()

    assert not device.under_external_control
    assert device.external_control_hold is None


def test_a_hold_with_no_reason_is_still_a_hold() -> None:
    """`None` means nobody is holding it; an empty reason is not the same thing."""
    device = create_test_device("shaker_1")

    device.hold_external_control()

    assert device.under_external_control
    assert device.external_control_hold == ""


@pytest.mark.timeout(30)
async def test_the_hold_and_its_reason_reach_the_operator_surface() -> None:
    """A hold nobody can see is a device that has silently stopped working.

    Goes through the operations layer, because that is what every surface
    calls, and reads the registry row, because that is what every surface
    answers from.
    """
    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    build, _ = await _build(recorder, wf_name="hold_surface")
    runtime = SystemRuntime(
        build.system, event_bus=build.event_bus, labware_store=InMemoryLabwareStore(),
    )
    await runtime.start()
    try:
        await TakeDeviceControlOperation(runtime=runtime).run(
            TakeDeviceControlRequest(device_name="arm", reason="jogging the arm"),
        )
        row = next(
            t for t in runtime.registry.list_transporters() if t.name == "arm"
        )
        assert row.under_external_control
        assert row.external_control_hold == "jogging the arm"

        await ReleaseDeviceControlOperation(runtime=runtime).run(
            ReleaseDeviceControlRequest(device_name="arm"),
        )
        row = next(
            t for t in runtime.registry.list_transporters() if t.name == "arm"
        )
        assert not row.under_external_control
        assert row.external_control_hold is None
    finally:
        await runtime.shutdown()

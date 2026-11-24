"""A device a sim or live switch could not bring up refuses operator commands until a clean initialize."""

import pytest

from orca.gateway.controller.controller import DeviceController
from orca.gateway.controller.exceptions import DeviceNotBroughtUpError
from orca.modes.run_modes import WorkflowRunMode
from tests.gateway.test_controller_device_fault import _run_and_succeed, _stub_preflight
from tests.gateway_parts import gateway_parts

_WHY = "flex_1 is set to LIVE but did not come up (ConnectionError: no answer)."


@pytest.fixture
def controller() -> DeviceController:
    return gateway_parts().controller


@pytest.mark.asyncio
async def test_an_operator_command_is_refused_with_the_bring_up_error(controller: DeviceController) -> None:
    controller.mark_not_brought_up("flex_1", _WHY)
    dispatched = _stub_preflight(controller)

    with pytest.raises(DeviceNotBroughtUpError, match="did not come up"):
        await controller.execute_command(
            device_id="flex_1", command="aspirate", params={},
            timeout_seconds=5.0, effective_mode=WorkflowRunMode.LIVE,
        )

    assert dispatched == {}
    assert controller.bring_up_error("flex_1") == _WHY


@pytest.mark.asyncio
async def test_a_clean_initialize_is_let_through_and_clears_it(controller: DeviceController) -> None:
    controller.mark_not_brought_up("flex_1", _WHY)

    await _run_and_succeed(controller, command="initialize")

    assert controller.bring_up_error("flex_1") is None

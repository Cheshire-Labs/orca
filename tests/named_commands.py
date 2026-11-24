"""Commands by name sent the way the execute and batch Operations send them, for tests of the runner beneath."""

from unittest.mock import MagicMock

from orca.gateway.device_command_runner import IDeviceCommandRunner
from orca.operations.operator_device import NamedCommands
from orca.modes.run_modes import WorkflowRunMode


def named_commands(runner: IDeviceCommandRunner, device_id: str) -> NamedCommands:
    """No operator hold, and every name sent as given."""
    runtime = MagicMock()
    runtime.devices.command_name = MagicMock(side_effect=lambda device, name: name)
    runtime.operator_base_mode = MagicMock(return_value=WorkflowRunMode.LIVE)
    return NamedCommands(runtime, runner, device_id)

"""One operator command through the runner's dispatch, answering the driver's raw answer and its interfaces."""

from pydantic import JsonValue

from orca.gateway.device_gateway import DeviceGateway
from orca.gateway.operator_commands import DriverAnswer, operator_control
from orca.runtime.runtime_interface import ISystemRuntime


async def run_operator_command(
    gateway: DeviceGateway,
    runtime: ISystemRuntime | None,
    device_id: str,
    command: str,
    params: dict[str, JsonValue] | None = None,
    *,
    timeout_seconds: float | None = None,
    confirm: bool = False,
) -> DriverAnswer:
    async with operator_control(gateway, runtime, device_id) as control:
        return await control.run(command, params, timeout_seconds=timeout_seconds, confirm=confirm)

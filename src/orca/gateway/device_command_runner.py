"""The operator runner's interface: what every operator route on every server sends its device commands through."""

from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from cheshire_drivers.command_table import DeviceCommand
from pydantic import BaseModel, ConfigDict, JsonValue
from typing_extensions import Self

from orca.gateway.registry import DeviceSnapshot


class NamedCommandResult(BaseModel):
    """What a command run by name returned, as it came off the driver, and how long it took."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: JsonValue = None
    duration_ms: float


class IDeviceCommandRunner(Protocol):
    """Runs an operator's device commands. Every server builds one and every operator route uses it."""

    async def device(self, device_id: str) -> DeviceSnapshot:
        """The device, refused when it is unknown or offline."""
        ...

    async def run(
        self, device_id: str, command: DeviceCommand, params: BaseModel, *, reason: str | None = None,
    ) -> BaseModel:
        """Run a command an interface declares and answer its typed response. The driver's declared limit times it."""
        ...

    async def run_named(
        self, device_id: str, command: str, params: Mapping[str, JsonValue],
        *, timeout_seconds: float | None = None, confirm: bool = False, reason: str | None = None,
    ) -> NamedCommandResult:
        """Run any advertised command, vendor extras included, and answer what the driver returned."""
        ...

    def hold(self, device_id: str) -> AbstractAsyncContextManager[Self]:
        """Hold the device's external control across every command run through the runner it yields."""
        ...

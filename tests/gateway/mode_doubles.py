"""Shared doubles for the run-mode resolution tests.

The suite-wide conftest seeds `current_run_mode`, which hides exactly the
question these tests ask, so `unseeded` runs a call past that seed. The device
double is backed by a real `DeviceRunMode` because the mode a command
carries IS the subject: canning it would replace the computation under test.
"""

import asyncio
import contextvars
from typing import Awaitable, Callable, TypeVar

from orca.gateway.remote_drivers import ModeResolver
from orca.resource_models.resources import EngineCommandLog, IModeAware, IResource
from orca.resource_models.device_run_mode import DeviceRunMode
from orca.modes.device_modes import DeviceMode, IDeviceModes
from tests.device_modes import InMemoryDeviceModes
from orca.modes.run_modes import WorkflowRunMode

_T = TypeVar("_T")


def unseeded(call: Callable[[], _T]) -> _T:
    """Run `call` with no submission mode in force."""
    return contextvars.Context().run(call)


async def unseeded_await(awaitable: Awaitable[_T]) -> _T:
    """Await `awaitable` with no submission mode in force.

    A task copies the context it is created in, so creating it inside a fresh
    `Context` drops the suite-wide seed for the whole call.
    """
    return await contextvars.Context().run(asyncio.ensure_future, awaitable)


def fixed_mode(mode: WorkflowRunMode) -> ModeResolver:
    """A wire-stamp resolver that answers `mode` for every device."""

    async def resolve(_name: str) -> WorkflowRunMode:
        return mode

    return resolve


class DeclaredDevice(IResource, IModeAware):
    """A device the topology declares, resolving through the real owner."""

    def __init__(self, setting: DeviceMode | None = None, name: str = "dev_1") -> None:
        self._name = name
        self._run_mode = DeviceRunMode(name)
        self._engine_commands = EngineCommandLog(name)
        if setting is not None:
            self._run_mode.use_device_modes(InMemoryDeviceModes({name: setting}))

    @property
    def name(self) -> str:
        return self._name

    async def mode_under(self, base: WorkflowRunMode) -> WorkflowRunMode:
        return await self._run_mode.mode_under(base)

    async def effective_mode(self) -> WorkflowRunMode:
        return await self._run_mode.effective_mode()

    @property
    def engine_commands(self) -> EngineCommandLog:
        return self._engine_commands

    def use_device_modes(self, modes: IDeviceModes) -> None:
        self._run_mode.use_device_modes(modes)

    async def setting(self) -> DeviceMode:
        return await self._run_mode.setting()

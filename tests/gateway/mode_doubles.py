"""Shared doubles for the run-mode resolution tests.

The suite-wide conftest seeds `current_run_mode`, which hides exactly the
question these tests ask, so `unseeded` runs a call past that seed. The device
double is backed by a real `SimulationManager` because the mode a command
carries IS the subject: canning it would replace the computation under test.
"""

import asyncio
import contextvars
from typing import Awaitable, Callable, TypeVar

from orca.resource_models.resources import IModeAware, IResource
from orca.resource_models.simulation_manager import SimulationManager
from orca.runtime.run_modes import WorkflowRunMode

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


class DeclaredDevice(IResource, IModeAware):
    """A device the topology declares, resolving through the real owner."""

    def __init__(
        self,
        sim_override: WorkflowRunMode | None = None,
        name: str = "dev_1",
    ) -> None:
        self._name = name
        self._sim_manager: SimulationManager[str] = SimulationManager(
            live_driver="live", sim_driver="sim", sim_override=sim_override,
        )

    @property
    def name(self) -> str:
        return self._name

    def mode_under(self, base: WorkflowRunMode) -> WorkflowRunMode:
        return self._sim_manager.mode_under(base)

    @property
    def effective_mode(self) -> WorkflowRunMode:
        return self._sim_manager.effective_mode

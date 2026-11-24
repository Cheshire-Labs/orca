"""The one answer to "what run mode is device X dispatching under".

Two inputs decide it: a BASE mode, and the device's own sim or live setting.
Combining them for dispatch is
`DeviceRunMode.mode_under`, and nothing else does it.

The base is the submission's run mode while an execution is in force. Outside
one it is `OUT_OF_RUN_BASE` (LIVE), or what the caller passes as `when_unseeded`:
an active run's mode for an operator command, CHECK_RUN inside a check run's runtime.

A device set to sim stays off the hardware whatever base the caller supplies,
and no setting turns a metadata read into a wire command.

With no live system there is no device to ask, so the question is refused
rather than answered from the base alone. A system that IS readable and simply
does not declare the device is a different case, and it does take the base.

Callers holding a device OBJECT go through `mode_of`. Callers holding only a
NAME (the gateway's remote drivers, stamping `effective_mode` onto every wire
command) go through `system_mode_resolver`, which resolves the name and lands
on the same computation, so both routes agree by construction. orca-client
picks its simulator or the real driver only from the `effective_mode` this
stamps, so orca-core is the one place the choice is made.
"""

from typing import Awaitable, Callable, NoReturn, Protocol

from orca.gateway.controller.exceptions import ModeUnresolvableError
from orca.resource_models.resources import IModeAware, IResource
from orca.modes.run_modes import (
    OUT_OF_RUN_BASE,
    BaseMode,
    WorkflowRunMode,
    current_run_mode,
)


class ResourceLookup(Protocol):
    """The slice of the system this needs: name in, resource out."""

    def has_resource(self, name: str) -> bool: ...

    def get_resource(self, name: str) -> IResource: ...


async def mode_of(
    resource: IResource | None,
    *,
    when_unseeded: WorkflowRunMode = OUT_OF_RUN_BASE,
) -> WorkflowRunMode:
    """The mode this resource dispatches under right now.

    `when_unseeded` is the BASE to resolve against when no execution is in
    force. A declared device still combines its own setting with it, so an
    operator command reaches a device set to live and is held back by one set
    to sim.
    """
    base = current_run_mode.get(when_unseeded)
    if isinstance(resource, IModeAware):
        return await resource.mode_under(base)
    return base


def require_system(
    system: ResourceLookup | None, device_name: str,
) -> ResourceLookup:
    """The live system, or a refusal to guess which world `device_name` is in.

    Raises `ModeUnresolvableError` when there is none: a runtime that is not
    built, or one a failed rebuild tore down.

    A system that IS readable but does not declare the device is a different
    answer, not a missing one: the caller's base stands.
    """
    if system is None:
        refuse_unbuilt(device_name)
    return system


def refuse_unbuilt(device_name: str) -> NoReturn:
    raise ModeUnresolvableError(
        f"Cannot tell which world device {device_name!r} is in: the "
        "runtime is not built, so its topology declaration cannot be "
        "read. Commands are refused until the runtime builds."
    )


async def resolve_device_mode(
    system: ResourceLookup | None,
    device_name: str,
    *,
    when_unseeded: WorkflowRunMode = OUT_OF_RUN_BASE,
) -> WorkflowRunMode:
    """`mode_of` for callers holding only a name."""
    live = require_system(system, device_name)
    if not live.has_resource(device_name):
        return await mode_of(None, when_unseeded=when_unseeded)
    return await mode_of(live.get_resource(device_name), when_unseeded=when_unseeded)


class HasSystem(Protocol):
    @property
    def system(self) -> ResourceLookup: ...


def system_mode_resolver(
    current_runtime: Callable[[], HasSystem | None],
    *,
    base_mode: BaseMode,
) -> Callable[[str], Awaitable[WorkflowRunMode]]:
    """A name-keyed resolver for `RemoteDeviceFactory`.

    Takes a provider rather than a runtime: a topology rebuild swaps the
    SystemRuntime instance, and a resolver holding the old one keeps reading
    the discarded topology's overrides until the process restarts.

    `base_mode` is read on each command, so an operator's command made while a run is active goes in the run's
    mode with no scope at the call site. It has no default: this stamp decides whether a command reaches an
    instrument, and no answer is right for every host.
    """

    async def resolve(device_name: str) -> WorkflowRunMode:
        runtime = current_runtime()
        return await resolve_device_mode(
            runtime.system if runtime is not None else None, device_name,
            when_unseeded=base_mode(),
        )

    return resolve

"""The one answer to "what run mode is device X dispatching under".

Two inputs decide it: a BASE mode, and the device's own topology
`sim_override`. Combining them for dispatch is
`SimulationManager.mode_under`, and nothing else does it. (The submit-time
gates in `orca.runtime` call `resolve_effective_mode_for_device` directly;
those decide whether a submission is accepted, not what goes on the wire.)

The base is the submission's run mode while an execution is in force. Outside
one there is no submission to read, so the caller states what it means: a
metadata read means PURE_SIM, because reading a device must not move it, while
an operator command means LIVE, because asking a device to move means the real
one. Both say so at the call site through `when_unseeded` rather than one of
them quietly winning.

An override only ever ratchets toward sim. Declaring DEVICE_SIM therefore keeps
a command off the hardware whatever base the caller supplies, and no override
turns a metadata read into a wire command.

That only holds while the declaration is readable. With no live system there is
nothing to combine the base with, so the question is refused rather than
answered from the base alone: guessing there is how a device that asked for sim
gets driven live. A system that IS readable and simply does not declare the
device is a different case, and it does take the base.

Callers holding a device OBJECT go through `mode_of`. Callers holding only a
NAME (the gateway's remote drivers, stamping `effective_mode` onto every wire
command) go through `system_mode_resolver`, which resolves the name and lands
on the same computation. Both routes therefore agree by construction. That
matters because the sim/live choice is made twice, in two processes: orca-core
picks sim-driver-vs-wire, and the device bridge picks Sim-backend-vs-real off
the `effective_mode` this stamps. Those two disagreeing is how a device
declared DEVICE_SIM ends up moving a real instrument.
"""

from typing import Callable, Protocol

from orca.gateway.controller.exceptions import ModeUnresolvableError
from orca.resource_models.resources import IModeAware, IResource
from orca.runtime.run_modes import (
    UNSEEDED_FALLBACK_MODE,
    WorkflowRunMode,
    current_run_mode,
)


class ResourceLookup(Protocol):
    """The slice of the system this needs: name in, resource out."""

    def has_resource(self, name: str) -> bool: ...

    def get_resource(self, name: str) -> IResource: ...


def mode_of(
    resource: IResource | None,
    *,
    when_unseeded: WorkflowRunMode = UNSEEDED_FALLBACK_MODE,
) -> WorkflowRunMode:
    """The mode this resource dispatches under right now.

    `when_unseeded` is the BASE to resolve against when no execution is in
    force, not a value handed back only for devices the topology never
    declared. A declared device still combines its own `sim_override` with it,
    so an operator command reaches a plain device and is held back by one
    declaring DEVICE_SIM.
    """
    base = current_run_mode.get(when_unseeded)
    if isinstance(resource, IModeAware):
        return resource.mode_under(base)
    return base


def require_system(
    system: ResourceLookup | None, device_name: str,
) -> ResourceLookup:
    """The live system, or a refusal to guess which world `device_name` is in.

    Raises `ModeUnresolvableError` when there is none: a runtime that is not
    built, or one a failed rebuild tore down. A device's `sim_override` is what
    holds a command back from real hardware, and while it is unreadable the
    honest answer is that nobody knows, not the base the caller hoped for.

    A system that IS readable but does not declare the device is a different
    answer, not a missing one: nothing declared an override, so the caller's
    base stands.
    """
    if system is None:
        raise ModeUnresolvableError(
            f"Cannot tell which world device {device_name!r} is in: the "
            "runtime is not built, so its topology declaration cannot be "
            "read. Commands are refused until the runtime builds."
        )
    return system


def resolve_device_mode(
    system: ResourceLookup | None,
    device_name: str,
    *,
    when_unseeded: WorkflowRunMode = UNSEEDED_FALLBACK_MODE,
) -> WorkflowRunMode:
    """`mode_of` for callers holding only a name."""
    live = require_system(system, device_name)
    if not live.has_resource(device_name):
        return mode_of(None, when_unseeded=when_unseeded)
    return mode_of(live.get_resource(device_name), when_unseeded=when_unseeded)


class _HasSystem(Protocol):
    @property
    def system(self) -> ResourceLookup: ...


def system_mode_resolver(
    current_runtime: Callable[[], _HasSystem | None],
    *,
    when_unseeded: WorkflowRunMode,
) -> Callable[[str], WorkflowRunMode]:
    """A name-keyed resolver for `RemoteDeviceFactory`.

    Takes a provider rather than a runtime: a topology rebuild swaps the
    SystemRuntime instance, and a resolver holding the old one keeps reading
    the discarded topology's overrides until the process restarts.

    `when_unseeded` has no default because this resolver stamps the mode that
    decides whether a command reaches an instrument, and there is no answer
    that is right for every host.
    """

    def resolve(device_name: str) -> WorkflowRunMode:
        runtime = current_runtime()
        return resolve_device_mode(
            runtime.system if runtime is not None else None, device_name,
            when_unseeded=when_unseeded,
        )

    return resolve

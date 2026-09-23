"""Workflow run modes plus the override-precedence resolver and C1 validator.

The mode hierarchy is:

    submit_override > topology per-device sim_override > deployment base_mode

Lives in its own module so foundational layers (`orca.resource_models.devices`,
`orca.resource_models.transporter`) can attach a `sim_override` kwarg without
re-importing the heavier `orca.runtime.status_models` graph.

`WorkflowRunMode` is re-exported from `orca.runtime.status_models` so existing
imports keep working; new code in resource-side packages should import it
from here directly.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum


class WorkflowRunMode(str, Enum):
    """Workflow run mode controlling sim vs wire vs live dispatch.

    PURE_SIM: every device runs against its sim driver; no wire calls.
    DEVICE_SIM: gateway-routed sim driver lives on orca-client; wire is
                exercised but devices are not real.
    LIVE: live drivers on real hardware.
    """

    PURE_SIM = "PURE_SIM"
    DEVICE_SIM = "DEVICE_SIM"
    LIVE = "LIVE"


# Per-async-task run mode for dispatch resolution.
#
# Seeded at each execution entrypoint -- `SystemRuntime._run_workflow`,
# `WorkflowExecutor.start`, `StandaloneMethodExecutor.start` -- from the
# submission's stamped `run_mode`, then re-seeded per-thread by
# `ExecutingLabwareThread.start()`. asyncio.Task inherits a snapshot of
# the parent context at creation, so child tasks (snapshot reads
# inside an execution, registry walks under a workflow task) see the
# seed automatically.
#
# No default: reads outside a seeded context raise LookupError. Snapshot
# facades pass `WorkflowRunMode.PURE_SIM` as a `current_run_mode.get(...)`
# fallback for unit-test direct-construction paths; production dispatch
# always observes the per-task seed.
current_run_mode: ContextVar[WorkflowRunMode] = ContextVar("current_run_mode")


# The execution id of the running thread, seeded at thread start next to
# `current_run_mode`. Read by the remote drivers' `_send` so the gateway
# controller can tell a workflow command (engine-bounded) from an ad-hoc one
# (gateway fail-fast timer). None outside a thread = ad-hoc.
current_execution_id: ContextVar[str | None] = ContextVar(
    "current_execution_id", default=None,
)


@dataclass(frozen=True)
class ResolvedDeviceMode:
    """Result of the v3.4 per-device mode resolution lookup.

    `resolved` is the WorkflowRunMode the device dispatches under.
    `warning` is a human-readable warning string when a LIVE submission
    encounters a sim-direction topology sim_override (the "did you forget
    to switch out of sim?" gate), or None when no warning applies.
    """

    resolved: WorkflowRunMode
    warning: str | None


def resolve_effective_mode_for_device(
    submission_mode: WorkflowRunMode,
    device_sim_override: WorkflowRunMode | None,
    *,
    device_name: str | None = None,
) -> ResolvedDeviceMode:
    """Combine a submission's run_mode with a device's topology sim_override.

    Implements the v3.4 12-row lookup table. Per-device overrides only
    ratchet toward sim relative to the submission mode; toward-live
    overrides are silently inert. The "warning" channel fires ONLY when
    the submission is LIVE and a sim-direction override is present.

    `device_name` is used only in the warning text (so messages name
    the offending device).
    """
    if submission_mode is WorkflowRunMode.PURE_SIM:
        return ResolvedDeviceMode(WorkflowRunMode.PURE_SIM, None)
    if submission_mode is WorkflowRunMode.DEVICE_SIM:
        if device_sim_override is WorkflowRunMode.PURE_SIM:
            return ResolvedDeviceMode(WorkflowRunMode.PURE_SIM, None)
        return ResolvedDeviceMode(WorkflowRunMode.DEVICE_SIM, None)
    # submission_mode is LIVE.
    if device_sim_override is None or device_sim_override is WorkflowRunMode.LIVE:
        return ResolvedDeviceMode(WorkflowRunMode.LIVE, None)
    name = device_name if device_name is not None else "device"
    msg = (
        f"device {name!r} has topology sim_override="
        f"{device_sim_override.name}; switch to LIVE if you want it live"
    )
    return ResolvedDeviceMode(device_sim_override, msg)


UNSEEDED_FALLBACK_MODE: WorkflowRunMode = WorkflowRunMode.PURE_SIM
"""The base mode a METADATA read resolves against outside a seeded scope.

A workflow always seeds `current_run_mode` at its execution entrypoint
(`SystemRuntime._run_workflow`, `WorkflowExecutor.start`,
`StandaloneMethodExecutor.start`), so an execution never sees this. Reads that
fire outside one have no submission to read, and PURE_SIM is the answer that
cannot move an instrument.

It is the default, not the only answer, and it is NOT what every out-of-run
read lands on. A caller whose intent differs states its own base: a topology
card walk takes this one, while a device SNAPSHOT and every operator write take
`OPERATOR_DEVICE_WRITE_BASE` instead, through
`orca.gateway.mode_resolution.mode_of(..., when_unseeded=...)` or
`mode_under`. The topology override is combined with whichever base applies.

Public so test code under unseeded contexts can match the fallback
deterministically.
"""


OPERATOR_DEVICE_WRITE_BASE: WorkflowRunMode = WorkflowRunMode.LIVE
"""The base mode an out-of-run device WRITE resolves against.

The mirror of `UNSEEDED_FALLBACK_MODE`: a metadata read outside a seeded scope
answers PURE_SIM because that cannot move an instrument, but an operator
lifecycle verb (initialize / connect / disconnect / reconcile-deck) MEANS the
real device -- resolving it against the read fallback made the verb drive the
in-process simulator and report success. The caller can still say otherwise
per request, and the topology sim_override ratchet is applied on top either
way, so a device declared sim never reaches hardware.
"""


@contextmanager
def mode_scope(base: WorkflowRunMode) -> Iterator[None]:
    """Seed `current_run_mode` for one call: 'this is the world I mean'.

    Device dispatch under the scope resolves each device through its own
    sim_override ratchet against `base`. The prior value is restored on exit,
    so an operator verb cannot leak its base into unrelated reads.
    """
    token = current_run_mode.set(base)
    try:
        yield
    finally:
        current_run_mode.reset(token)


__all__ = [
    "OPERATOR_DEVICE_WRITE_BASE",
    "ResolvedDeviceMode",
    "UNSEEDED_FALLBACK_MODE",
    "WorkflowRunMode",
    "current_execution_id",
    "current_run_mode",
    "mode_scope",
    "resolve_effective_mode_for_device",
]

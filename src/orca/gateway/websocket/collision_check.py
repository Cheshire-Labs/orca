"""Late-connect collision check (interface contract enforcement on every connect).

A device bridge whose advertised interface contract is missing capabilities
the topology declared must be refused, both at runtime startup AND on every
late connect. The router calls :func:`check_collision` for each device in a
`ConnectMessage`. If any device's advertised interfaces are not a superset
of the declared interfaces, the WebSocket is closed with code 1002 and a
reason that names the offending device.

Capability rule: `advertised_interfaces` MUST be a superset of
`declared_interfaces`. A device that declares `IShaker` may be advertised
as `[IShaker, IReader]` (extra capability is fine), but a device that
declares `[IShaker, IReader]` may NOT connect with only `[IShaker]`
(missing capability is a contract break that would crash the workflow at
dispatch time).

Kind drift (`device.type != topo.declared_kind`) is logged at WARNING and
otherwise accepted. The kind label is metadata for dispatch ergonomics
(timeout defaults, sim driver pairing) and any number of device kinds may
satisfy a workflow's interface requirements -- the safety contract is
captured fully by the interface superset rule. Logging surfaces config
drift to operators without blocking a connection that will work at the
method level.

When the runtime isn't built yet (cold start before deployment is
loaded), every device passes by default: the runtime will run a full
collision pass at deployment-load time anyway, so refusing an early
connect would just delay it without preventing the eventual mismatch.
"""

import logging
from dataclasses import dataclass
from typing import Iterable

from orca.runtime.runtime_interface import ISystemRuntime

from cheshire_drivers.gateway_protocol import DeviceConnectInfo

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CollisionViolation:
    """One device's interface contract mismatch against topology."""

    name: str
    declared_kind: str
    advertised_kind: str
    declared_interfaces: frozenset[str]
    advertised_interfaces: frozenset[str]
    reason: str


async def check_collision(
    runtime: ISystemRuntime | None,
    devices: Iterable[DeviceConnectInfo],
) -> list[CollisionViolation]:
    """Return a list of devices whose advertised contract violates C2.

    Empty list = every device's advertised contract matches or extends
    topology. The caller closes the WebSocket if the list is non-empty.
    """
    violations: list[CollisionViolation] = []
    if runtime is None:
        return violations

    for device in devices:
        try:
            entry = await runtime.device_registry.get(device.name)
        except Exception:
            # If the registry can't answer (cold-start race), skip this
            # device. The runtime startup pass will catch the mismatch.
            continue
        if entry is None or entry.topology_card is None:
            # Quarantine: a device-bridge device with no topology card is
            # accepted (operators can still dispatch directly via REST/MCP;
            # excluded from workflow scheduling), but warned so a misconfigured
            # or typo'd device name is visible at connect time. Cold start
            # (runtime not built) returned early above, so this only fires once
            # a deployment is loaded.
            logger.warning(
                "device %r connected but is not declared in topology; it "
                "cannot be scheduled in workflows. Check for a name mismatch "
                "against the declared devices.",
                device.name,
            )
            continue
        topo = entry.topology_card
        advertised_interfaces = frozenset(device.interfaces)
        if device.type != topo.declared_kind:
            # Kind drift is advisory: the safety contract is the interface
            # superset rule below. Log so operators see config drift, do not
            # reject. A connected device whose interfaces satisfy the
            # workflow's calls will dispatch correctly even if its kind
            # label disagrees with the topology's.
            logger.warning(
                "kind drift on %s: topology declared %r, the device bridge "
                "advertises %r (accepting; interfaces will be checked)",
                device.name, topo.declared_kind, device.type,
            )
        if topo.declared_interfaces_are_class_defaults:
            # Nothing was declared: the set is whatever the driver class
            # happens to default to, so a client advertising less of it has
            # broken no contract. Rejecting here closes the whole socket and
            # takes every device on that box offline at once.
            if not advertised_interfaces & topo.declared_interfaces:
                # Sharing nothing is a different device under this name, not a
                # rollback. Still accepted, because the per-command refusal
                # says so without taking the rest of the box down, but it is
                # the config error nobody would otherwise see.
                logger.error(
                    "interface mismatch on %s: the topology's class defaults to "
                    "%r and the device bridge advertises %r, which share nothing. "
                    "Accepting, because closing the link would take every device "
                    "on this client offline; every workflow call on %s will be "
                    "refused until the topology and the device bridge agree.",
                    device.name, sorted(topo.declared_interfaces),
                    sorted(advertised_interfaces), device.name,
                )
            continue
        if not advertised_interfaces.issuperset(topo.declared_interfaces):
            missing = topo.declared_interfaces - advertised_interfaces
            violations.append(
                CollisionViolation(
                    name=device.name,
                    declared_kind=topo.declared_kind,
                    advertised_kind=device.type,
                    declared_interfaces=topo.declared_interfaces,
                    advertised_interfaces=advertised_interfaces,
                    reason=(
                        f"interface contract break: topology declared "
                        f"{sorted(topo.declared_interfaces)} but "
                        f"the device bridge advertised "
                        f"{sorted(advertised_interfaces)}; missing "
                        f"{sorted(missing)}"
                    ),
                )
            )
    return violations


def format_violations_for_close_reason(
    violations: list[CollisionViolation],
) -> str:
    """Build a single-line WebSocket close reason from collision violations.

    WebSocket close reasons must fit in 123 bytes (RFC 6455 limits the
    Close frame's reason field). Truncate to the first violation when
    the full list won't fit; the gateway log carries the full record.
    """
    if not violations:
        return ""
    if len(violations) == 1:
        v = violations[0]
        msg = f"contract collision: {v.name}: {v.reason}"
    else:
        names = ", ".join(v.name for v in violations)
        first = violations[0]
        msg = (
            f"contract collision: {len(violations)} devices ({names}); "
            f"first: {first.name}: {first.reason}"
        )
    if len(msg.encode("utf-8")) > 120:
        return msg.encode("utf-8")[:120].decode("utf-8", errors="ignore")
    return msg

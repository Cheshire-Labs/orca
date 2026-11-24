"""DeviceFacade: operator-facing device reads, and the checks an operator command by name passes first.

The commands themselves go through the server's operator runner. Capability introspection has two sources. Interface methods come from the
device's bound LIVE driver's `interfaces` ClassVar, mapped through
`_INTERFACE_BRIDGE` to the orca-side interface that declares them, with param
types read off the annotations. Vendor commands exist on no orca-side
interface, so they come from the connected device's handshake card, where
their signatures and docstrings arrived. `cli_accessible` is False when any
param is a non-JSON-primitive.
"""

from dataclasses import replace

import inspect
from typing import Any, Callable

from pydantic import JsonValue

from cheshire_drivers.driver_introspection import (
    MethodInfo,
    derive_capabilities as _derive_capabilities,
    describe_driver as _describe_driver,
)

from orca.devices.device_interfaces import (
    ICentrifuge,
    IDelidder,
    ILiquidHandler,
    IPlateWasher,
    IProtocolRunner,
    IReader,
    ISealer,
    IShaker,
    ITempGettable,
    ITempSettable,
    IThermocycler,
)
from orca.gateway.device_gateway import DeviceGateway
from orca.gateway.registry.snapshot import DeviceSnapshot as ConnectionSnapshot
from orca.devices.devices import LiquidHandler as DeviceLiquidHandler
from orca.resource_models.device_error import DeviceUnderExternalControlError
from orca.resource_models.devices import Device
from orca.state.mounted import MountedTips
from orca.resource_models.transporter import Transporter
from orca.runtime.danger import DangerLevel, ParamSpec, dangerous
from orca.runtime.registries.device_link import (
    DeviceLinkReader,
    forget_driver_session as _forget_driver_session,
)
from orca.runtime.runtime_interface import (
    ISystemRuntime,
)
from orca.runtime.registry_protocols import (
    IDeviceConnectionSource,
    IDeviceFaultSource,
)
from orca.runtime.facade_interfaces import (
    IDeviceFacade,
)
from orca.runtime.status_models import (
    CommandDescriptor,
    DeviceFaultSummary,
    DeviceIntrospection,
    DeviceSnapshot,
)
from orca.system.system_interface import DeckComparison, ISystem



# Bridge from a driver-advertised interface NAME (the `interfaces` ClassVar
# on cheshire-drivers driver classes) to the orca-side capability interface
# that declares the invokable methods plus its operator namespace. The driver
# is the single source of truth for which capabilities a device exposes:
# `get_supported_commands` / `_allowed_invoke_methods` read
# `resource.driver.interfaces` and map each name through here, exactly
# like `get_device_introspection` and the topology registry. A liquid handler
# is no different from any other device -- its surface is whatever its bound
# driver advertises (protocol-only, plr-only, or both).
_INTERFACE_BRIDGE: dict[str, tuple[type, str]] = {
    "ITempSettable": (ITempSettable, "temperature"),
    "ITempGettable": (ITempGettable, "temperature"),
    "ISealer": (ISealer, "sealer"),
    "IShaker": (IShaker, "shaker"),
    "ICentrifuge": (ICentrifuge, "centrifuge"),
    "IThermocycler": (IThermocycler, "thermocycler"),
    "IReader": (IReader, "reader"),
    "IDelidder": (IDelidder, "delidder"),
    "IPlateWasher": (IPlateWasher, "plate_washer"),
    "IProtocolRunner": (IProtocolRunner, "protocol"),
    "ILiquidHandler": (ILiquidHandler, "liquid_handler"),
}

_PRIMITIVE_TYPES: frozenset[type] = frozenset({bool, int, float, str, bytes, type(None)})

# The orca-side namespaces `device capabilities` prints in front of an
# interface method. Only these are dropped from a submitted name: any other
# prefix belongs to the command (`gripper.ungrip`), and trimming one would
# quietly send the command to a different object.
_ORCA_NAMESPACES: frozenset[str] = frozenset(
    ns for _iface, ns in _INTERFACE_BRIDGE.values()
)


class DeviceFacade(IDeviceFacade):
    """Concrete DeviceFacade implementation."""

    def __init__(
        self,
        system: ISystem,
        connections: IDeviceConnectionSource,
        runtime: ISystemRuntime,
        device_gateway: DeviceGateway,
    ) -> None:
        self._system = system
        self._links = DeviceLinkReader(connections, system, runtime.operator_base_mode)
        self._runtime = runtime
        self._device_gateway = device_gateway
        self._faults: IDeviceFaultSource = device_gateway.controller

    def _connection_card(self, device_name: str) -> ConnectionSnapshot | None:
        """What the device advertised when its orca-client connected, if it did."""
        return self._device_gateway.tracker.peek_snapshot(device_name)

    # -- Reads ---------------------------------------------------------------

    async def get_device_status(self, device_name: str) -> DeviceSnapshot:
        return await self._snapshot(self._resolve(device_name))

    def _dispatch_target(self, device_name: str) -> Device | Transporter | None:
        """The topology resource for this device, or None if only a device
        bridge has it.

        Raises KeyError when neither knows the name, so a device that does not
        exist is reported missing rather than as one with no such command.
        """
        try:
            return self._resolve(device_name)
        except (KeyError, ValueError):
            pass
        if self._connection_card(device_name) is None:
            raise KeyError(
                f"Device '{device_name}' is not declared in the topology and no "
                f"device bridge has connected it."
            )
        return None

    def get_supported_commands(self, device_name: str) -> list[CommandDescriptor]:
        # Interface commands come from the topology resource, vendor commands
        # from the device bridge's card; an undeclared device has only the card.
        resource = self._dispatch_target(device_name)
        vendor = _vendor_descriptors(device_name, self._connection_card(device_name))
        if resource is None:
            return vendor
        return _capabilities_for_device(resource) + vendor


    def get_device_introspection(self, device_name: str) -> DeviceIntrospection:
        # A gateway device's local driver is the remote proxy, and describing
        # it describes the proxy: none of the vendor surface, and none of the
        # signatures the driver on the bench sent at its handshake.
        card = self._connection_card(device_name)
        if card is not None:
            return DeviceIntrospection(
                type=card.type,
                name=device_name,
                interfaces=tuple(sorted(card.interfaces)),
                capabilities=tuple(sorted(card.capabilities)),
                provides_state=card.provides_state,
                methods={
                    name: info.model_dump(mode="json")
                    for name, info in card.methods.items()
                },
            )
        resource = self._resolve(device_name)
        driver = resource.driver
        driver_cls = type(driver)
        interfaces_attr: frozenset[str] = getattr(driver_cls, "interfaces", frozenset())
        interfaces: tuple[str, ...] = tuple(sorted(interfaces_attr))
        capabilities = tuple(sorted(_derive_capabilities(driver_cls)))
        provides_state = bool(getattr(driver_cls, "provides_state", False))
        method_infos = _describe_driver(driver)
        methods: dict[str, dict[str, JsonValue]] = {
            name: info.model_dump(mode="json") for name, info in method_infos.items()
        }
        return DeviceIntrospection(
            type=driver_cls.__name__,
            name=device_name,
            interfaces=interfaces,
            capabilities=capabilities,
            provides_state=provides_state,
            methods=methods,
        )

    # -- Writes --------------------------------------------------------------

    async def get_mounted_tips(self, device_name: str) -> MountedTips:
        """What the record says this device's head is carrying."""
        self._system.get_device(device_name)
        return await self._system.mounted_tips.of(device_name)

    @dangerous(
        name="device.set_mounted_tips",
        level=DangerLevel.OPERATOR,
        message="State that device '{device_name}' is carrying {by_channel} on its "
                "channels and {head96} on its 96 head. "
                "Absolute: channels you leave out are recorded as carrying "
                "nothing. This writes Orca's record only; nothing sends it to "
                "the driver. A vendor protocol runs only when the record says "
                "the head is empty.",
    )
    async def set_mounted_tips(
        self, device_name: str, by_channel: dict[int, tuple[str, str]],
        *, head96: dict[int, tuple[str, str]] | None = None, reason: str | None = None, confirm: bool = False,
    ) -> None:
        del confirm, reason  # consumed by @dangerous audit
        self._system.get_device(device_name)
        await self._system.mounted_tips.assert_mounted(device_name, by_channel, head96)

    @dangerous(
        name="device.confirm_mounted_tips",
        level=DangerLevel.OPERATOR,
        message="Agree with what the record already says device "
                "'{device_name}' is carrying. Nothing changes except that the "
                "answer stops being one nobody has looked at.",
    )
    async def confirm_mounted_tips(
        self, device_name: str, *, reason: str | None = None, confirm: bool = False,
    ) -> None:
        del confirm, reason  # consumed by @dangerous audit
        self._system.get_device(device_name)
        await self._system.mounted_tips.confirm(device_name)

    def command_name(self, device_name: str, name: str) -> str:
        """The command `name` sends. An orca-side namespace (`shaker.shake`) is dropped; a vendor prefix is kept."""
        namespace, _, member = name.rpartition(".")
        if namespace not in _ORCA_NAMESPACES or not member:
            return name
        device = self._dispatch_target(device_name)
        card = self._connection_card(device_name)
        advertised = _advertised_capabilities(card) | _advertised_methods(card)
        if device is not None:
            advertised |= _allowed_invoke_methods(device)
        return name if name in advertised else member

    async def after_lifecycle(self, device_name: str, command: str) -> None:
        """A liquid handler's deck after a bring-up is pushed again from the ledger; after a disconnect it is forgotten."""
        try:
            resource = self._resolve(device_name)
        except (KeyError, ValueError):
            return
        if not isinstance(resource, DeviceLiquidHandler):
            return
        if command == "disconnect":
            # The re-seed happens before its next command, once the device is back.
            await self._forget_the_deck(resource)
        else:
            await self._deck_goes_stale(resource)

    @dangerous(
        name="device.reconcile_deck",
        level=DangerLevel.OPERATOR,
        message="Re-seed liquid handler '{device_name}' from the world model: "
                "re-dispatch its deck layout and re-declare every labware the "
                "ledger places on its deck. A state push, no motion. Use after "
                "anything that rebuilt the driver's session outside orca (a "
                "run cancelled on the touchscreen, a driver-internal recovery) "
                "so a RETRY can find its labware again.",
    )
    async def reconcile_deck(
        self, device_name: str, *,
        reason: str | None = None,
    ) -> DeckComparison | None:
        """Force the deck reconcile pass for one liquid handler NOW.

        Lifecycle verbs dispatched through orca re-seed automatically; this is
        the operator's lever for the rebuilds orca never saw. Only the world in
        force is pushed; the device's other worlds are left as they were.

        The comparison runs FIRST and its result comes back, because the push
        that follows overwrites the driver's answer: whatever the two disagreed
        about would otherwise be gone with no record that it ever existed.
        """
        del reason  # consumed by @dangerous audit
        resource = self._require_liquid_handler(device_name, "reconcile")
        comparison = await self._compare_and_report(resource)
        await self._forget_the_deck(resource)
        location = self._system.system_map.get_resource_location(resource.name)
        async with self._system.device_worlds.rebuilding(resource.name):
            await self._system.labware_placer.deck_rebuilt(location)
        return comparison

    @dangerous(
        name="device.take_external_control",
        level=DangerLevel.OPERATOR,
        message="Take '{device_name}' out of the workflow's reach. Any running "
                "thread that tries to use it, or to move labware into or out of "
                "it, fails until you release it. Hold it while you are driving "
                "the device by hand, and release it when you are done.",
    )
    async def take_external_control(
        self, device_name: str, *, reason: str | None = None,
    ) -> None:
        """Claim a device for hands-on work until it is released.

        The gateway takes external control only while an operator's commands
        are open, and gives it back when the last one ends. A workflow can start
        a move into the device between two of them. This is the standing claim:
        it lasts until someone releases it.
        """
        self._resolve(device_name).hold_external_control(reason)

    @dangerous(
        name="device.release_external_control",
        level=DangerLevel.OPERATOR,
        message="Give '{device_name}' back to the workflow. Anything waiting on "
                "it starts using it again, so be clear of the device first.",
    )
    async def release_external_control(self, device_name: str, *, reason: str | None = None) -> None:
        """Hand the device back. Idempotent."""
        del reason  # consumed by @dangerous audit
        self._resolve(device_name).release_external_control_hold()

    async def compare_deck(self, device_name: str) -> DeckComparison:
        """What this liquid handler's driver says its deck holds, against the ledger.

        Changes neither side, except to rebuild a driver world that is only
        behind the record, as its next command would. Files nothing either: an
        assistant is told to run this routinely, so filing per call would put
        three copies of every conflict on the queue for three checks. The
        reconcile is what files, because that is the call that acts.
        """
        resource = self._require_liquid_handler(device_name, "compare")
        comparison = await self._compare_and_report(resource, report=False)
        if comparison is None:
            raise ValueError(
                f"liquid handler '{device_name}' has no deck layout configured, "
                f"so there is no deck to compare"
            )
        return comparison

    def _require_liquid_handler(
        self, device_name: str, verb: str,
    ) -> DeviceLiquidHandler:
        resource = self._resolve(device_name)
        if not isinstance(resource, DeviceLiquidHandler):
            raise ValueError(
                f"device '{device_name}' is not a liquid handler; only a "
                f"liquid handler carries a deck to {verb}"
            )
        return resource

    async def _compare_and_report(
        self, resource: DeviceLiquidHandler, *, report: bool = True,
    ) -> DeckComparison | None:
        """Compare, and optionally put every difference on the incident surface.

        A driver reporting an EMPTY deck is not a disagreement about where
        anything is: its session was rebuilt and the deck is waiting to be
        re-declared, which is the reconcile's whole job. So it files nothing,
        AND it answers with nothing: a caller reading `disagreements` would
        otherwise be handed every resident as missing while the incident queue
        stayed deliberately silent, and the two would be saying opposite things.
        """
        location = self._system.system_map.get_resource_location(resource.name)
        comparison = await self._system.compare_lh_deck_occupancy(location)
        if comparison is None:
            return None
        if comparison.driver_deck_empty:
            return replace(comparison, disagreements=())
        if report:
            for conflict in comparison.disagreements:
                self._system.notify_deck_reconcile_conflict(conflict)
        return comparison

    async def reseed_deck_if_liquid_handler(self, device_name: str) -> bool:
        """A reconnected liquid handler may hold a new driver session: its deck layout is forgotten and its world in
        force rebuilt now, or at its next command if another task holds it. Other names are skipped."""
        try:
            resource = self._resolve(device_name)
        except KeyError:
            return False
        if not isinstance(resource, DeviceLiquidHandler):
            return False
        await self._forget_every_deck(resource)
        await self._system.device_worlds.bring_up_to_date(resource.name)
        return True

    def forget_driver_session(self, device_name: str) -> None:
        """Expire what this device's proxy cached about its link.

        Unknown names are skipped rather than errors because every gateway
        connect, drop and report routes through here, and a gateway device
        need not be declared in the topology.
        """
        try:
            resource = self._resolve(device_name)
        except KeyError:
            return
        _forget_driver_session(resource.driver)

    async def _forget_the_deck(self, resource: DeviceLiquidHandler) -> None:
        """The driver in force may have been rebuilt with an empty deck: its layout is forgotten and its world
        marked, so it is laid out and rebuilt from the ledger before its next command. Other modes' drivers were
        not sent the command, so their worlds are left as they were."""
        await resource.invalidate_deck_world_in_force()
        self._system.device_worlds.mark(resource.name, await resource.effective_mode())

    async def _forget_every_deck(self, resource: DeviceLiquidHandler) -> None:
        """The client may have rebuilt every driver it holds for this device: every world is forgotten and marked."""
        await resource.invalidate_deck_world()
        self._system.device_worlds.mark_every_world(resource.name)

    async def _deck_goes_stale(self, resource: DeviceLiquidHandler) -> None:
        """As `_forget_the_deck`, and the world in force is rebuilt now."""
        await self._forget_the_deck(resource)
        await self._system.device_worlds.bring_up_to_date(resource.name)

    # -- Resolution + snapshots ---------------------------------------------

    def _resolve(self, name: str) -> Device | Transporter:
        """Per-name resolver that accepts either runtime device kind.

        ``list_devices`` returns Devices and Transporters in one union
        view, so per-name reads must accept either. ``ResourceRegistry.
        get_device`` is Equipment-only (raises ValueError for Transporter)
        and ``get_transporter`` is the inverse, so try Device first and fall
        back to Transporter, surfacing KeyError when neither matches.
        """
        try:
            return self._system.get_device(name)
        except ValueError:
            pass
        try:
            return self._system.get_transporter(name)
        except ValueError as exc:
            raise KeyError(f"Unknown runtime device: {name!r}") from exc

    def _fault_summary(self, device_name: str) -> DeviceFaultSummary | None:
        """This device's unresolved fault, shaped for an operator read."""
        fault = self._faults.fault(device_name)
        return None if fault is None else DeviceFaultSummary.of(fault)

    @dangerous(
        name="device.clear_fault",
        level=DangerLevel.OPERATOR,
        message="Say that '{device_name}' has been looked at and is fit to drive. "
                "The workflow starts using it again, so put the machine right "
                "first: nothing here checks it. Often not needed: recovering a "
                "thread paused on this fault with RETRY, RETRY_ACTION or CONTINUE "
                "says the same thing, and so does a clean initialize or home.",
    )
    async def clear_fault(self, device_name: str, *, reason: str | None = None) -> DeviceFaultSummary | None:
        """Give a faulted device back to the workflow. Returns what was cleared.

        Idempotent, and it changes nothing on the instrument. It is a person
        saying they looked.
        """
        del reason  # consumed by @dangerous audit
        self._resolve(device_name)
        summary = self._fault_summary(device_name)
        await self._faults.clear_fault(device_name)
        return summary

    async def _snapshot(self, resource: Device | Transporter) -> DeviceSnapshot:
        # One world per snapshot: the same base the link flags beside it resolve.
        effective_mode = await resource.mode_under(self._runtime.operator_base_mode())
        setting = await resource.setting()
        # Not `resource.is_initialized`: on the cloud path that reads a proxy
        # cache the typed device routes never touch, so it can sit False
        # through a full bring-up while the registry route says True.
        is_initialized = await self._links.is_initialized(resource.name)
        if isinstance(resource, Transporter):
            held = resource.labware
            return DeviceSnapshot(
                name=resource.name,
                type_name=type(resource).__name__,
                is_initialized=is_initialized,
                is_busy=resource.in_use,
                effective_mode=effective_mode,
                setting=setting,
                position_ids=(),
                loaded_labware_ids=(held.id,) if held is not None else (),
                under_external_control=resource.under_external_control,
                external_control_hold=resource.external_control_hold,
                fault=self._fault_summary(resource.name),
            )
        return DeviceSnapshot(
            name=resource.name,
            type_name=type(resource).__name__,
            is_initialized=is_initialized,
            is_busy=resource.in_use,
            effective_mode=effective_mode,
            setting=setting,
            position_ids=tuple(loc.name for loc in resource.locations),
            loaded_labware_ids=resource.all_loaded_labware_ids,
            under_external_control=resource.under_external_control,
            external_control_hold=resource.external_control_hold,
            fault=self._fault_summary(resource.name),
        )


def _iface_methods(iface: type) -> list[str]:
    """Every directly-declared method on an interface (abstract or not)."""
    return sorted(
        name for name, member in inspect.getmembers(iface, inspect.isfunction)
        if not name.startswith("_") and name in iface.__dict__
    )


def _driver_interface_names(resource: Device | Transporter) -> frozenset[str]:
    """Capability interface names the resource's driver advertises; nothing for a resource without one."""
    driver = getattr(resource, "driver", None)
    if driver is None:
        return frozenset()
    return frozenset(getattr(type(driver), "interfaces", frozenset()))


def _capability_members(
    resource: Device | Transporter,
) -> list[tuple[str, type, str]]:
    """`(namespace, interface, method_name)` for every driver-advertised capability.

    Each advertised interface name is mapped through `_INTERFACE_BRIDGE` to its
    orca-side interface + namespace; methods come from the interface contract.
    Deterministic order (namespace, method).
    """
    members: list[tuple[str, type, str]] = []
    for iface_name in _driver_interface_names(resource):
        bridged = _INTERFACE_BRIDGE.get(iface_name)
        if bridged is None:
            continue
        iface, ns = bridged
        for method_name in _iface_methods(iface):
            members.append((ns, iface, method_name))
    return sorted(set(members), key=lambda m: (m[0], m[2]))


def _capabilities_for_device(
    resource: Device | Transporter,
) -> list[CommandDescriptor]:
    """CommandDescriptors for a resolved resource (driver-sourced surface)."""
    return [
        _to_descriptor(resource.name, ns, iface, method_name)
        for ns, iface, method_name in _capability_members(resource)
    ]


def _advertised_methods(card: ConnectionSnapshot | None) -> frozenset[str]:
    """Every method name the connected driver put in its handshake catalog.

    Wider than `_advertised_capabilities`: it also holds the @external
    interface methods, which is what `device capabilities` lists.
    """
    return frozenset(card.methods) if card is not None else frozenset()


def _advertised_capabilities(card: ConnectionSnapshot | None) -> frozenset[str]:
    """Vendor commands the connected driver advertised at the handshake.

    These exist on no orca-side interface: they are whatever the driver
    forwards to its vendor objects, and the handshake is the only place this
    side of the wire learns them.
    """
    return frozenset(card.capabilities) if card is not None else frozenset()


def _catalog_params(info: MethodInfo | None) -> tuple[ParamSpec, ...]:
    """ParamSpecs from a handshake catalog entry.

    A command taking a single Pydantic request carries that model's schema
    rather than the flat shape, and is reported as one parameter: the wire
    takes it whole.
    """
    if info is None:
        return ()
    specs: list[ParamSpec] = []
    for name, spec in info.params.items():
        if isinstance(spec, dict) and "required" in spec:
            # A parameter whose default IS None still has one, and the catalog
            # says so by carrying the key. Reading the value alone renders an
            # optional argument as required.
            specs.append(ParamSpec(
                name=name,
                type_name=str(spec.get("type") or "Any"),
                required=bool(spec["required"]),
                default=repr(spec["default"]) if "default" in spec else None,
                description="",
            ))
        else:
            specs.append(ParamSpec(
                name=name, type_name="object", required=True,
                default=None, description="",
            ))
    return tuple(specs)


def _vendor_descriptors(
    device_name: str, card: ConnectionSnapshot | None,
) -> list[CommandDescriptor]:
    """Descriptors for the vendor commands a connected device advertises."""
    if card is None:
        return []
    out: list[CommandDescriptor] = []
    for name in sorted(card.capabilities):
        info = card.methods.get(name)
        params = _catalog_params(info)
        docstring = (info.docstring if info is not None else "") or ""
        out.append(CommandDescriptor(
            device_name=device_name,
            capability=name,
            danger_level=DangerLevel.PHYSICAL,
            description=next(iter(docstring.strip().splitlines()), ""),
            cli_accessible=all(_is_primitive(p) for p in params),
            params=params,
        ))
    return out


def _to_descriptor(
    device_name: str, namespace: str, iface: type, method_name: str,
) -> CommandDescriptor:
    method = getattr(iface, method_name)
    params = _extract_params(method)
    return CommandDescriptor(
        device_name=device_name,
        capability=f"{namespace}.{method_name}",
        danger_level=DangerLevel.PHYSICAL,
        description=(method.__doc__ or "").strip().split("\n")[0],
        cli_accessible=all(_is_primitive(p) for p in params),
        params=params,
    )


def _allowed_invoke_methods(device: Device | Transporter) -> frozenset[str]:
    """Method names dispatchable via `DeviceFacade.invoke`.

    Mirrors `get_supported_commands`: the bare method names of every
    capability the device's live driver advertises. `execute` is excluded
    (it has its own `device.execute` facade entry point); audit-gated verbs
    (`initialize`, `take_external_control`) live on `Device`, not on
    capability interfaces, and so are unreachable through `invoke`.
    """
    return frozenset(
        method_name
        for ns, _iface, method_name in _capability_members(device)
        if ns != "generic"
    )


def _extract_params(method: Callable[..., Any]) -> tuple[ParamSpec, ...]:
    """Pull ParamSpec entries from a bound/unbound method's signature."""
    sig = inspect.signature(method)
    specs: list[ParamSpec] = []
    for pname, param in sig.parameters.items():
        if pname in ("self", "cls"):
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        annotation = param.annotation
        if annotation is inspect.Parameter.empty:
            type_name = "Any"
        else:
            type_name = getattr(annotation, "__name__", None) or str(annotation)
        has_default = param.default is not inspect.Parameter.empty
        default_repr = repr(param.default) if has_default else None
        specs.append(ParamSpec(
            name=pname,
            type_name=type_name,
            required=not has_default,
            default=default_repr,
            description="",
        ))
    return tuple(specs)


def _is_primitive(param: ParamSpec) -> bool:
    """Heuristic: primitive type names pass CLI invocation checks."""
    name = param.type_name
    # Strip typing module prefix
    name = name.replace("typing.", "")
    # Strip Optional wrappers -- Optional[T] renders as "T | None"
    name = name.replace(" | None", "").replace("Optional[", "").rstrip("]")
    primitive_names = {"bool", "int", "float", "str", "bytes", "NoneType", "None", "Any"}
    if name in primitive_names:
        return True
    # dict[str, Any] / dict[str, T] with primitive T
    if name.startswith("dict[") or name.startswith("Dict["):
        return True
    if name.startswith("list[") or name.startswith("List["):
        inner = name.split("[", 1)[1].rstrip("]")
        return inner in primitive_names
    return False

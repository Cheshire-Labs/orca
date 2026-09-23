"""The crux of the LH manual-motion vertical: a gateway-built remote liquid
handler must DECLARE the motion facets its on-prem device advertises, or the
topology card's declared-INTERSECT-advertised gate strips them and every motion
command (move_channel_to, grip_with_force, ...) is rejected as unsupported.

A single generic profile class cannot enumerate the many motion combinations, so
the profile ClassVar stays the coarse plr-only / protocol / both marker (it fixes
run_protocol behavior and is the cold-start default) while a per-instance
`declared_interfaces`, sourced from the connection card, carries the real set.
The topology registry reads the per-instance set; the capability gate then keeps
motion.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, cast

from orca.devices.devices import LiquidHandlerProtocol
from orca.gateway.controller.controller import DeviceController
from orca.gateway.registry.capabilities import validate_capability_for_device
from orca.gateway.remote_device_factory import RemoteDeviceFactory
from orca.gateway.remote_drivers import RemoteLiquidHandlerDriver
from orca.runtime.device_factory_context import use_device_factory
from orca.runtime.registries.topology_registry import SystemTopologyRegistry
from orca.runtime.run_modes import WorkflowRunMode


# A Flex: pipette + gripper motion + a force-closed jaw; no protocol, no width jaw.
FLEX = frozenset(
    {"ILiquidHandler", "IPipetteMotion", "IGripperMotion", "IForceGripperJaw"}
)


@dataclass
class _FakeController:
    calls: List[Dict[str, Any]] = field(default_factory=list)

    async def execute_command(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        return {}


def _factory(profiles: Dict[str, frozenset[str]]) -> RemoteDeviceFactory:
    return RemoteDeviceFactory(
        controller=cast(DeviceController, _FakeController()),
        default_timeout=30.0,
        mode_resolver=lambda _name: WorkflowRunMode.LIVE,
        profile_source=lambda name: profiles.get(name),
    )


def _flex_device() -> LiquidHandlerProtocol:
    with use_device_factory(_factory({"flex": FLEX})):
        device = LiquidHandlerProtocol("flex")
    assert isinstance(device, LiquidHandlerProtocol)
    return device


def _cold_start_device() -> LiquidHandlerProtocol:
    with use_device_factory(_factory({})):
        device = LiquidHandlerProtocol("lh")
    assert isinstance(device, LiquidHandlerProtocol)
    return device


class TestRemoteDeclaresAdvertisedMotion:
    def test_live_driver_carries_advertised_set_but_keeps_profile_classvar(self) -> None:
        live = _flex_device().live_driver
        # A Flex has no protocol capability, so the coarse profile stays plr-only:
        # the class + its ClassVar are unchanged (the parity guard reads these).
        assert type(live) is RemoteLiquidHandlerDriver
        assert RemoteLiquidHandlerDriver.interfaces == frozenset({"ILiquidHandler"})
        # ...but this instance declares the full advertised card, motion included.
        assert live.declared_interfaces == FLEX

    def test_cold_start_declares_nothing(self) -> None:
        live = _cold_start_device().live_driver
        assert isinstance(live, RemoteLiquidHandlerDriver)
        assert live.declared_interfaces is None

    def test_topology_entry_surfaces_motion_facets(self) -> None:
        entry = SystemTopologyRegistry._build_entry(_flex_device())
        assert "IPipetteMotion" in entry.interfaces
        assert frozenset(entry.interfaces) == FLEX

    def test_topology_entry_falls_back_to_classvar_at_cold_start(self) -> None:
        entry = SystemTopologyRegistry._build_entry(_cold_start_device())
        assert frozenset(entry.interfaces) == frozenset({"ILiquidHandler"})

    def test_effective_set_authorizes_motion_command(self) -> None:
        # Both cards now carry motion, so the declared-INTERSECT-advertised
        # effective set keeps the motion facets and the commands validate.
        effective = frozenset(SystemTopologyRegistry._build_entry(_flex_device()).interfaces) & FLEX
        assert validate_capability_for_device(effective, frozenset(), "move_channel_to")
        assert validate_capability_for_device(effective, frozenset(), "grip_with_force")

    def test_plr_only_base_would_reject_motion(self) -> None:
        # The regression the fix undoes: the plr-only ClassVar alone strips motion,
        # so move_channel_to is rejected as unsupported.
        assert not validate_capability_for_device(
            frozenset({"ILiquidHandler"}), frozenset(), "move_channel_to"
        )

"""A device can still do what it advertises when the runtime booted first.

A hosted deployment builds its runtime from `topology.py` and only then does
the on-prem client dial in, so at build time nothing has said what any device
advertises. The gateway driver falls back to its class's default interface set,
which is the bare device kind: `{ILiquidHandler}` for a handler, and nothing
past the kind for a shaker. That default used to land in the topology card and
get intersected with the connection card, so a Flex whose catalog listed `home`
refused it with "does not support command 'home'". Same for its gripper, its
pipette motion and its gantry park.

Two halves to the fix, both covered here: a driver built after the client
connected carries the device's real advertised card (every device kind now,
not just liquid handlers), and a driver built before it connected says its
interface set is only the class default so dispatch reads the connection card
instead.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from orca.devices.devices import LiquidHandlerProtocol
from orca.devices.shaker import Shaker
from orca.gateway import adhoc
from orca.gateway.controller.controller import DeviceController
from orca.gateway.gateway_backed_driver import GatewayBackedDriver
from orca.gateway.registry.capabilities import validate_capability_for_device
from orca.gateway.remote_device_factory import RemoteDeviceFactory
from orca.runtime.device_factory_context import use_device_factory
from orca.runtime.registries.topology_registry import SystemTopologyRegistry
from orca.runtime.run_modes import WorkflowRunMode
from orca.resource_models.devices import Device
from orca.runtime.status_models import TopologyDeviceEntry


# What a real Opentrons Flex advertises: it homes, it moves a gripper, it parks.
FLEX = frozenset({
    "ILiquidHandler", "ILiquidProbe", "IPipetteMotion", "IGripperMotion",
    "IForceGripperJaw", "IGantryParking", "IHomeable",
})
# What a heater-shaker advertises: the shaking, plus the block temperature.
HEATER_SHAKER = frozenset({"IShaker", "ITempSettable", "ITempGettable"})


@dataclass
class _FakeController:
    calls: List[Dict[str, Any]] = field(default_factory=list)

    async def execute_command(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        return {}


def _factory(advertised: Dict[str, frozenset[str]]) -> RemoteDeviceFactory:
    return RemoteDeviceFactory(
        controller=cast(DeviceController, _FakeController()),
        default_timeout=30.0,
        mode_resolver=lambda _name: WorkflowRunMode.LIVE,
        profile_source=lambda name: advertised.get(name),
    )


def _flex(*, client_connected: bool) -> LiquidHandlerProtocol:
    with use_device_factory(_factory({"flex_1": FLEX} if client_connected else {})):
        device = LiquidHandlerProtocol("flex_1")
    assert isinstance(device, LiquidHandlerProtocol)
    return device


def _shaker(*, client_connected: bool) -> Shaker:
    advertised = {"shaker_1": HEATER_SHAKER} if client_connected else {}
    with use_device_factory(_factory(advertised)):
        device = Shaker("shaker_1")
    assert isinstance(device, Shaker)
    return device


def _entry_for(device: Device[Any]) -> TopologyDeviceEntry:
    return SystemTopologyRegistry._build_entry(device)


class TestBuiltAfterTheClientConnected:
    def test_a_shaker_carries_the_temperature_it_advertises(self) -> None:
        entry = _entry_for(_shaker(client_connected=True))
        assert frozenset(entry.interfaces) == HEATER_SHAKER
        assert entry.interfaces_are_class_defaults is False

    def test_a_flex_carries_the_homing_it_advertises(self) -> None:
        entry = _entry_for(_flex(client_connected=True))
        assert "IHomeable" in entry.interfaces
        assert entry.interfaces_are_class_defaults is False


class TestBuiltBeforeTheClientConnected:
    def test_the_flex_entry_says_its_interfaces_are_only_the_class_default(
        self,
    ) -> None:
        entry = _entry_for(_flex(client_connected=False))
        assert frozenset(entry.interfaces) == frozenset({"ILiquidHandler"})
        assert entry.interfaces_are_class_defaults is True

    def test_the_shaker_entry_says_the_same(self) -> None:
        entry = _entry_for(_shaker(client_connected=False))
        assert frozenset(entry.interfaces) == frozenset({"IShaker"})
        assert entry.interfaces_are_class_defaults is True

    def test_a_local_driver_is_never_treated_as_a_class_default(self) -> None:
        """A device with no gateway behind it declares its own interfaces, so
        the ClassVar is the real declaration and must keep narrowing dispatch."""
        entry = _entry_for(Shaker("bench_shaker"))
        assert entry.interfaces_are_class_defaults is False

    def test_a_local_driver_that_answers_to_the_same_name_is_still_local(
        self,
    ) -> None:
        """The question is what the driver IS, not what it happens to answer to.
        Reading `declared_interfaces` off whatever has the attribute would let a
        local driver that grew one widen a dispatch gate with nothing going red.
        """
        shaker = Shaker("bench_shaker")
        driver = shaker.live_driver
        assert not isinstance(driver, GatewayBackedDriver)
        object.__setattr__(driver, "declared_interfaces", None)

        assert _entry_for(shaker).interfaces_are_class_defaults is False


def _runtime_holding(
    declared: frozenset[str], advertised: frozenset[str], *, class_defaults: bool,
) -> MagicMock:
    runtime = MagicMock()
    entry = MagicMock()
    entry.topology_card.declared_interfaces = declared
    entry.topology_card.declared_interfaces_are_class_defaults = class_defaults
    entry.connection_card.advertised_interfaces = advertised
    runtime.device_registry.get = AsyncMock(return_value=entry)
    return runtime


@pytest.mark.asyncio
class TestWhatTheDeviceIsAllowedToRun:
    async def test_home_reaches_a_flex_built_before_its_client_connected(
        self,
    ) -> None:
        runtime = _runtime_holding(
            frozenset({"ILiquidHandler"}), FLEX, class_defaults=True,
        )

        effective = await adhoc.resolve_effective_interfaces(
            runtime, "flex_1", FLEX,
        )

        assert effective == FLEX
        assert validate_capability_for_device(
            effective or frozenset(), frozenset(), "home",
        ), "the catalog lists `home`; refusing it leaves the operator homing at the arm"

    async def test_a_narrower_declaration_still_holds_the_agent_back(self) -> None:
        """The capability-drift block is untouched: a device bridge that
        advertises more than the deployment declared still cannot dispatch the
        extra."""
        runtime = _runtime_holding(
            frozenset({"IShaker"}),
            frozenset({"IShaker", "IReader"}),
            class_defaults=False,
        )

        effective = await adhoc.resolve_effective_interfaces(
            runtime, "shaker_1", frozenset({"IShaker", "IReader"}),
        )

        assert effective == frozenset({"IShaker"})

"""A device can still do what it advertises when nothing declared its interfaces at build time.

With no profile source a gateway driver falls back to its class's default
interface set, which is the bare device kind: `{ILiquidHandler}` for a handler,
and nothing past the kind for a shaker. Intersecting that default with the
connection card would make a Flex whose catalog lists `home` refuse it.

A driver built with a profile source carries the set its device setup declares,
for every device kind. A driver built without one says its interface set is only
the class default, so dispatch reads the connection card instead.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.registry_entries import registry_entry, registry_holding

from orca.devices.devices import LiquidHandlerProtocol
from orca.devices.shaker import Shaker
from orca.gateway import operator_commands
from orca.gateway.controller.controller import DeviceController
from orca.gateway.registry.capabilities import validate_capability_for_device
from orca.gateway.remote_device_factory import RemoteDeviceFactory
from orca.driver_management.device_factory_context import use_device_factory
from orca.runtime.registries.topology_registry import SystemTopologyRegistry
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.devices import Device
from orca.runtime.status_models import TopologyDeviceEntry
from orca.gateway.gateway_backed_driver import GatewayBackedDriver
from tests.gateway.mode_doubles import fixed_mode
from tests.mock import UniversalMockDevice


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


def _factory(declared: frozenset[str] | None) -> RemoteDeviceFactory:
    return RemoteDeviceFactory(
        controller=cast(DeviceController, _FakeController()),
        mode_resolver=fixed_mode(WorkflowRunMode.LIVE),
        profile_source=None if declared is None else lambda _kind, _name, _deck_modeling: declared,
    )


def _flex(*, declared: bool) -> LiquidHandlerProtocol:
    with use_device_factory(_factory(FLEX if declared else None)):
        device = LiquidHandlerProtocol("flex_1")
    assert isinstance(device, LiquidHandlerProtocol)
    return device


def _shaker(*, declared: bool) -> Shaker:
    with use_device_factory(_factory(HEATER_SHAKER if declared else None)):
        device = Shaker("shaker_1")
    assert isinstance(device, Shaker)
    return device


def _entry_for(device: Device[Any]) -> TopologyDeviceEntry:
    return SystemTopologyRegistry._build_entry(device)


class TestBuiltWithAProfileSource:
    def test_a_shaker_carries_the_temperature_it_advertises(self) -> None:
        entry = _entry_for(_shaker(declared=True))
        assert frozenset(entry.interfaces) == HEATER_SHAKER
        assert entry.interfaces_are_class_defaults is False

    def test_a_flex_carries_the_homing_it_advertises(self) -> None:
        entry = _entry_for(_flex(declared=True))
        assert "IHomeable" in entry.interfaces
        assert entry.interfaces_are_class_defaults is False


class TestBuiltWithNoProfileSource:
    def test_the_flex_entry_says_its_interfaces_are_only_the_class_default(
        self,
    ) -> None:
        entry = _entry_for(_flex(declared=False))
        assert frozenset(entry.interfaces) == frozenset({"ILiquidHandler"})
        assert entry.interfaces_are_class_defaults is True

    def test_the_shaker_entry_says_the_same(self) -> None:
        entry = _entry_for(_shaker(declared=False))
        assert frozenset(entry.interfaces) == frozenset({"IShaker"})
        assert entry.interfaces_are_class_defaults is True

    def test_a_local_driver_is_never_treated_as_a_class_default(self) -> None:
        """A device with no gateway behind it declares its own interfaces, so
        the ClassVar is the real declaration and must keep narrowing dispatch."""
        entry = _entry_for(UniversalMockDevice("bench_device"))
        assert entry.interfaces_are_class_defaults is False

    def test_a_local_driver_that_answers_to_the_same_name_is_still_local(
        self,
    ) -> None:
        """The question is what the driver IS, not what it happens to answer to.
        Reading `declared_interfaces` off whatever has the attribute would let a
        local driver that grew one widen a dispatch gate with nothing going red.
        """
        device = UniversalMockDevice("bench_device")
        driver = device.driver
        assert not isinstance(driver, GatewayBackedDriver)
        object.__setattr__(driver, "declared_interfaces", None)

        assert _entry_for(device).interfaces_are_class_defaults is False


def _runtime_holding(
    declared: frozenset[str], advertised: frozenset[str], *, class_defaults: bool,
) -> MagicMock:
    return registry_holding(registry_entry(declared, advertised, class_defaults=class_defaults))


@pytest.mark.asyncio
class TestWhatTheDeviceIsAllowedToRun:
    async def test_home_reaches_a_flex_built_with_no_profile_source(
        self,
    ) -> None:
        runtime = _runtime_holding(
            frozenset({"ILiquidHandler"}), FLEX, class_defaults=True,
        )

        effective = await operator_commands.resolve_effective_interfaces(runtime, "flex_1")

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

        effective = await operator_commands.resolve_effective_interfaces(runtime, "shaker_1")

        assert effective == frozenset({"IShaker"})

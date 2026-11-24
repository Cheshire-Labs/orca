"""Unit tests for `RemoteDeviceFactory` and the per-category Remote*Driver wrappers.

The factory builds an orca-core `Device` with both a wire-forwarding live
driver and a paired in-process `Sim*Driver`. Each Remote*Driver forwards
through `DeviceController.execute_command` carrying a per-dispatch
`effective_mode` resolved by a factory-supplied callable.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, TypeVar, cast

import pytest

from cheshire_drivers.centrifuge_models import CentrifugeRequest
from cheshire_drivers.liquid_handler_models import MovePlateRequest
from cheshire_drivers.protocol_runner_models import RunProtocolRequest
from cheshire_drivers.shaker_models import ShakeRequest

from orca.devices.centrifuge import Centrifuge
from orca.devices.devices import Delidder, LiquidHandlerProtocol, PlateWasher, Reader
from orca.devices.shaker import Shaker
from orca.devices.sealer import Sealer
from orca.resource_models.devices import Device
from orca.driver_management.device_factory_context import use_device_factory
from orca.modes.device_modes import DeviceMode
from orca.modes.run_modes import WorkflowRunMode

from orca.gateway.controller.controller import DeviceController
from orca.gateway.controller.exceptions import ModeUnresolvableError
from orca.gateway.mode_resolution import system_mode_resolver
from orca.gateway.remote_device_factory import RemoteDeviceFactory
from orca.gateway.remote_drivers import (
    RemoteCentrifugeDriver,
    RemoteDelidderDriver,
    RemoteLiquidHandlerDriver,
    RemoteLiquidHandlerWithProtocolDriver,
    RemotePlateWasherDriver,
    RemoteProtocolOnlyLiquidHandlerDriver,
    RemoteReaderDriver,
    RemoteSealerDriver,
    RemoteShakerDriver,
)
from orca.gateway.remote_drivers import RunProtocolNotSupportedError
from tests.gateway.mode_doubles import DeclaredDevice, unseeded_await, fixed_mode


@dataclass
class _RecordedCall:
    device_id: str
    command: str
    params: Optional[Dict[str, Any]]
    timeout_seconds: Optional[float]
    effective_mode: WorkflowRunMode


@dataclass
class _FakeController:
    """Stand-in for `DeviceController` recording every dispatch."""

    calls: List[_RecordedCall] = field(default_factory=list)
    responses: List[Dict[str, Any]] = field(default_factory=list)

    async def execute_command(
        self,
        device_id: str,
        command: str,
        params: Optional[Dict[str, Any]] = None,
        timeout_seconds: Optional[float] = None,
        effective_mode: WorkflowRunMode = WorkflowRunMode.LIVE,
        execution_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        self.calls.append(
            _RecordedCall(
                device_id=device_id,
                command=command,
                params=params,
                timeout_seconds=timeout_seconds,
                effective_mode=effective_mode,
            )
        )
        if self.responses:
            return self.responses.pop(0)
        return {}


def _make_factory(
    *,
    mode: WorkflowRunMode = WorkflowRunMode.LIVE,
    profiles: Optional[Dict[str, frozenset[str]]] = None,
) -> tuple[RemoteDeviceFactory, _FakeController]:
    controller = _FakeController()
    factory = RemoteDeviceFactory(
        controller=cast(DeviceController, controller),
        mode_resolver=fixed_mode(mode),
        profile_source=None if profiles is None else lambda _kind, name, _deck_modeling: profiles[name],
    )
    return factory, controller


_DeviceT = TypeVar("_DeviceT", bound=Device)


def _sdk_build(
    factory: RemoteDeviceFactory, cls: Callable[[str], _DeviceT], name: str,
) -> _DeviceT:
    """Build `cls(name)` under a bound gateway factory (the no-driver SDK path).

    Mirrors a deployment author writing `Shaker("ml_star")` with the gateway
    factory bound: the ctor's `resolve_driver` pulls its driver from
    `factory.build_driver`. Devices carry a device-type kind, so the concrete
    class is the sole input.
    """
    with use_device_factory(factory):
        return cls(name)


class TestFactoryDeviceTypes:
    """Gateway SDK-ctor build wires the right Remote* driver per device."""

    def test_shaker_returns_shaker_with_remote(self) -> None:
        factory, _ = _make_factory()
        device = _sdk_build(factory, Shaker, "shaker_1")
        assert isinstance(device, Shaker)
        assert isinstance(device.driver, RemoteShakerDriver)

    def test_centrifuge_returns_centrifuge_with_remote(self) -> None:
        factory, _ = _make_factory()
        device = _sdk_build(factory, Centrifuge, "cent_1")
        assert isinstance(device, Centrifuge)
        assert isinstance(device.driver, RemoteCentrifugeDriver)

    def test_sealer_returns_sealer_with_remote(self) -> None:
        factory, _ = _make_factory()
        device = _sdk_build(factory, Sealer, "seal_1")
        assert isinstance(device, Sealer)
        assert isinstance(device.driver, RemoteSealerDriver)

    def test_reader_returns_reader_with_remote(self) -> None:
        factory, _ = _make_factory()
        device = _sdk_build(factory, Reader, "rdr_1")
        assert isinstance(device, Reader)
        assert isinstance(device.driver, RemoteReaderDriver)

    def test_delidder_returns_delidder_with_remote(self) -> None:
        factory, _ = _make_factory()
        device = _sdk_build(factory, Delidder, "delid_1")
        assert isinstance(device, Delidder)
        assert isinstance(device.driver, RemoteDelidderDriver)

    def test_plate_washer_returns_plate_washer_with_remote(self) -> None:
        factory, _ = _make_factory()
        device = _sdk_build(factory, PlateWasher, "pw_1")
        assert isinstance(device, PlateWasher)
        assert isinstance(device.driver, RemotePlateWasherDriver)

    def test_liquid_handler_cold_start_defaults_to_plr_only(self) -> None:
        """No profile source -> plr-only LIVE profile (safe default).

        With no profile source the factory picks the plr-only live driver
        (advertises {ILiquidHandler}).
        """
        factory, _ = _make_factory()
        device = _sdk_build(factory, LiquidHandlerProtocol, "lh_1")
        assert isinstance(device, LiquidHandlerProtocol)
        live = device.driver
        assert type(live) is RemoteLiquidHandlerDriver
        assert live.interfaces == frozenset({"ILiquidHandler"})


class TestLiquidHandlerProfileSelection:
    """The LH live driver takes its profile per instance from its profile source.

    The factory reads the profile source at build time and picks the matching
    live driver class so the orca facade sees the device's true capability
    set, not a frozen {ILiquidHandler}.
    """

    def test_both_profile_advertises_lh_and_protocol(self) -> None:
        factory, _ = _make_factory(
            profiles={"mlstar": frozenset({"ILiquidHandler", "IProtocolRunner"})},
        )
        device = _sdk_build(factory, LiquidHandlerProtocol, "mlstar")
        live = device.driver
        assert type(live) is RemoteLiquidHandlerWithProtocolDriver
        assert live.interfaces == frozenset({"ILiquidHandler", "IProtocolRunner"})

    def test_protocol_only_profile_advertises_protocol_runner(self) -> None:
        factory, _ = _make_factory(
            profiles={"bravo": frozenset({"IProtocolRunner"})},
        )
        device = _sdk_build(factory, LiquidHandlerProtocol, "bravo")
        live = device.driver
        assert type(live) is RemoteProtocolOnlyLiquidHandlerDriver
        assert live.interfaces == frozenset({"IProtocolRunner"})

    def test_plr_only_profile_advertises_lh_only(self) -> None:
        factory, _ = _make_factory(
            profiles={"opentrons": frozenset({"ILiquidHandler"})},
        )
        device = _sdk_build(factory, LiquidHandlerProtocol, "opentrons")
        live = device.driver
        assert type(live) is RemoteLiquidHandlerDriver
        assert live.interfaces == frozenset({"ILiquidHandler"})


class TestLiquidHandlerRunProtocolFailFast:
    """run_protocol must fail fast at the driver boundary when the profile
    omits IProtocolRunner, mirroring orca-core LiquidHandler.run_protocol.
    """

    @pytest.mark.asyncio
    async def test_plr_only_run_protocol_fails_fast(self) -> None:
        factory, controller = _make_factory(
            profiles={"opentrons": frozenset({"ILiquidHandler"})},
        )
        device = _sdk_build(factory, LiquidHandlerProtocol, "opentrons")
        live = device.driver
        live_cls = type(live)
        assert live_cls is RemoteLiquidHandlerDriver
        with pytest.raises(RunProtocolNotSupportedError):
            await live.run_protocol(
                RunProtocolRequest(protocol_filepath="x.pro", params={})
            )
        # Fail-fast means no wire dispatch happened.
        assert controller.calls == []

    @pytest.mark.asyncio
    async def test_protocol_only_run_protocol_forwards(self) -> None:
        factory, controller = _make_factory(
            profiles={"bravo": frozenset({"IProtocolRunner"})},
        )
        device = _sdk_build(factory, LiquidHandlerProtocol, "bravo")
        live = device.driver
        live_cls = type(live)
        assert live_cls is RemoteProtocolOnlyLiquidHandlerDriver
        await live.run_protocol(
            RunProtocolRequest(protocol_filepath="x.pro", params={})
        )
        assert [c.command for c in controller.calls] == ["run_protocol"]

    @pytest.mark.asyncio
    async def test_both_profile_run_protocol_forwards(self) -> None:
        factory, controller = _make_factory(
            profiles={"mlstar": frozenset({"ILiquidHandler", "IProtocolRunner"})},
        )
        device = _sdk_build(factory, LiquidHandlerProtocol, "mlstar")
        live = device.driver
        live_cls = type(live)
        assert live_cls is RemoteLiquidHandlerWithProtocolDriver
        await live.run_protocol(
            RunProtocolRequest(protocol_filepath="x.pro", params={})
        )
        assert [c.command for c in controller.calls] == ["run_protocol"]


class TestNoDriverSdkBuildDriver:
    """`build_driver` gives each device kind its one forwarding driver."""

    def test_shaker_gets_a_forwarding_driver(self) -> None:
        factory, _ = _make_factory()
        assert isinstance(factory.build_driver("shaker", "shaker_1"), RemoteShakerDriver)

    def test_storage_gets_a_forwarding_driver(self) -> None:
        """A stacker's dispense moves hardware, so it goes to the stacker's orca-client like every command."""
        from orca.gateway.remote_drivers import RemoteStorageDriver, RemoteWasteDriver

        factory, _ = _make_factory()
        assert isinstance(factory.build_driver("storage", "stacker_1"), RemoteStorageDriver)
        assert isinstance(factory.build_driver("waste", "waste_1"), RemoteWasteDriver)

    def test_transporter_gets_a_forwarding_driver(self) -> None:
        from orca.gateway.remote_transporter_driver import RemoteTransporterDriver

        factory, _ = _make_factory()
        assert isinstance(factory.build_driver("transporter", "transporter_1"), RemoteTransporterDriver)

    def test_a_translator_declares_one_carriage(self) -> None:
        """`single_carriage` is read at topology-build time, before any device bridge has connected."""
        from orca.gateway.remote_transporter_driver import RemoteTranslatorDriver

        factory, _ = _make_factory()
        driver = factory.build_driver("translator", "translator_1")
        assert isinstance(driver, RemoteTranslatorDriver)
        assert driver.single_carriage is True

    def test_unknown_device_type_raises(self) -> None:
        factory, _ = _make_factory()
        with pytest.raises(ValueError, match="Unknown device type"):
            factory.build_driver("nonexistent_kind", "x")


class TestRemoteDriverSendIncludesEffectiveMode:
    """Each Remote*Driver forwards `effective_mode` from the resolver."""

    @pytest.mark.asyncio
    async def test_shaker_shake_includes_effective_mode_live(self) -> None:
        factory, controller = _make_factory(mode=WorkflowRunMode.LIVE)
        device = _sdk_build(factory, Shaker, "shaker_1")
        # Pull the live driver and exercise its driver-level method directly.
        live = device.driver
        assert isinstance(live, RemoteShakerDriver)
        await live.shake(ShakeRequest(speed=200.0, duration=10.0))
        assert len(controller.calls) == 1
        call = controller.calls[0]
        assert call.device_id == "shaker_1"
        assert call.command == "shake"
        assert call.effective_mode is WorkflowRunMode.LIVE

    @pytest.mark.asyncio
    async def test_connect_through_the_device_reaches_the_wire(self) -> None:
        """Cross the layer an operator actually goes through, not just the proxy.

        REST, the CLI and MCP all call `Device.connect()`, which dispatches via
        the run-mode-aware `driver` property. Driving the proxy object directly
        skips that hop, so it cannot catch a device wired to the wrong driver.
        Seeded LIVE, because an unseeded caller resolves to CHECK_RUN and would
        be talking to the sim stub.
        """
        from orca.modes.run_modes import current_run_mode

        factory, controller = _make_factory(mode=WorkflowRunMode.LIVE)
        device = _sdk_build(factory, Shaker, "shaker_1")

        token = current_run_mode.set(WorkflowRunMode.LIVE)
        try:
            await device.connect()
            assert device.driver.is_connected is True, (
                "the flag must read the driver the verb dispatched to"
            )
            await device.disconnect()
            assert device.driver.is_connected is False
        finally:
            current_run_mode.reset(token)

        assert [c.command for c in controller.calls] == ["connect", "disconnect"]

    @pytest.mark.asyncio
    async def test_lifecycle_verbs_reach_the_wire(self) -> None:
        """`connect`/`disconnect` must forward, not inherit the contract default.

        They are concrete on `BaseDriver` so backends with no separate link can
        honestly do nothing. A remote proxy that inherits that default reports
        success without the command leaving the process, which reads to the
        operator as a connected device the instrument never heard about.
        """
        factory, controller = _make_factory(mode=WorkflowRunMode.LIVE)
        device = _sdk_build(factory, Shaker, "shaker_1")
        live = device.driver
        assert isinstance(live, RemoteShakerDriver)

        await live.connect()
        await live.disconnect()

        assert [c.command for c in controller.calls] == ["connect", "disconnect"]
        assert live.is_initialized is False

    @pytest.mark.asyncio
    async def test_shaker_shake_with_sim_mode(self) -> None:
        factory, controller = _make_factory(mode=WorkflowRunMode.SIM)
        device = _sdk_build(factory, Shaker, "shaker_1")
        live = device.driver
        assert isinstance(live, RemoteShakerDriver)
        await live.shake(ShakeRequest(speed=200.0, duration=10.0))
        assert controller.calls[0].effective_mode is WorkflowRunMode.SIM

    @pytest.mark.asyncio
    async def test_centrifuge_includes_effective_mode(self) -> None:
        factory, controller = _make_factory(mode=WorkflowRunMode.LIVE)
        device = _sdk_build(factory, Centrifuge, "cent_1")
        live = device.driver
        assert isinstance(live, RemoteCentrifugeDriver)
        await live.centrifuge(CentrifugeRequest(g=500.0, duration=30.0))
        assert controller.calls[0].command == "centrifuge"
        assert controller.calls[0].effective_mode is WorkflowRunMode.LIVE


class _FakeSystem:
    """The slice of a system the resolver reads: name in, resource out."""

    def __init__(self, resource: DeclaredDevice) -> None:
        self._resource = resource

    def has_resource(self, name: str) -> bool:
        return name == self._resource.name

    def get_resource(self, name: str) -> DeclaredDevice:
        return self._resource


class _FakeRuntime:
    def __init__(self, system: _FakeSystem) -> None:
        self.system = system


class TestModeResolverFollowsTheRuntime:
    """A driver built before the runtime exists still reads the topology that
    eventually appears.

    That is the cold-start path: the first build fails, the operator drops a
    corrected topology and reloads, and drivers built in between must pick up
    the declaration. A factory that answered for itself while it had nobody to
    ask would drive a real instrument for a device declared SIM.
    """

    @staticmethod
    def _factory_over(
        runtime_slot: List[Optional[_FakeRuntime]],
    ) -> tuple[RemoteDeviceFactory, _FakeController]:
        controller = _FakeController()
        factory = RemoteDeviceFactory(
            controller=cast(DeviceController, controller),
            mode_resolver=system_mode_resolver(
                lambda: runtime_slot[0], base_mode=lambda: WorkflowRunMode.LIVE,
            ),
        )
        return factory, controller

    @pytest.mark.asyncio
    async def test_a_driver_refuses_to_dispatch_while_the_runtime_is_down(
        self,
    ) -> None:
        """A driver built before the runtime cannot read any declaration, and
        a device that declared SIM would be driven live if it guessed."""
        runtime_slot: List[Optional[_FakeRuntime]] = [None]
        factory, controller = self._factory_over(runtime_slot)
        device = _sdk_build(factory, Shaker, "shaker_1")
        live = device.driver
        assert isinstance(live, RemoteShakerDriver)

        with pytest.raises(ModeUnresolvableError):
            await unseeded_await(
                live.shake(ShakeRequest(speed=100.0, duration=5.0))
            )
        assert controller.calls == []

    @pytest.mark.asyncio
    async def test_a_declaration_that_arrives_late_still_reaches_the_wire(
        self,
    ) -> None:
        runtime_slot: List[Optional[_FakeRuntime]] = [None]
        factory, controller = self._factory_over(runtime_slot)
        device = _sdk_build(factory, Shaker, "shaker_1")
        live = device.driver
        assert isinstance(live, RemoteShakerDriver)

        # Runtime up, and it declares the device sim-side. The driver built
        # before it existed honours that without being rebuilt; drivers do not
        # cache the mode.
        runtime_slot[0] = _FakeRuntime(_FakeSystem(
            DeclaredDevice(DeviceMode.SIM, name="shaker_1"),
        ))
        await unseeded_await(live.shake(ShakeRequest(speed=100.0, duration=5.0)))
        assert controller.calls[-1].effective_mode is WorkflowRunMode.SIM

        # And a device set to live reaches the wire as LIVE.
        runtime_slot[0] = _FakeRuntime(_FakeSystem(
            DeclaredDevice(DeviceMode.LIVE, name="shaker_1"),
        ))
        await unseeded_await(live.shake(ShakeRequest(speed=100.0, duration=5.0)))
        assert controller.calls[-1].effective_mode is WorkflowRunMode.LIVE


class TestRemoteSealerGetTemperature:
    """`RemoteSealerDriver.get_temperature` must surface wire errors loudly.

    A sealer that fails to report its temperature must NOT silently appear
    to be at 0.0 C; a missing `temperature` key on the wire response is a
    driver-side failure and must raise so the gateway surfaces a real
    error code instead of a plausible freezing reading.
    """

    @pytest.mark.asyncio
    async def test_missing_temperature_key_raises(self) -> None:
        factory, controller = _make_factory(mode=WorkflowRunMode.LIVE)
        device = _sdk_build(factory, Sealer, "seal_1")
        live = device.driver
        assert isinstance(live, RemoteSealerDriver)
        # Prime the fake controller to return a response with no
        # `temperature` field, simulating a driver that failed to report.
        controller.responses.append({})

        with pytest.raises((KeyError, RuntimeError, ValueError)):
            await live.get_temperature()

    @pytest.mark.asyncio
    async def test_present_temperature_is_returned(self) -> None:
        factory, controller = _make_factory(mode=WorkflowRunMode.LIVE)
        device = _sdk_build(factory, Sealer, "seal_1")
        live = device.driver
        assert isinstance(live, RemoteSealerDriver)
        controller.responses.append({"temperature": 42.5})

        value = await live.get_temperature()
        assert value == 42.5


class TestRemoteDriverInterfaces:
    """Each Remote*Driver advertises the right interfaces ClassVar."""

    def test_shaker_driver_advertises_ishaker(self) -> None:
        assert RemoteShakerDriver.interfaces == frozenset({"IShaker"})

    def test_centrifuge_driver_advertises_icentrifuge(self) -> None:
        assert RemoteCentrifugeDriver.interfaces == frozenset({"ICentrifuge"})

    def test_liquid_handler_profiles_advertise_their_interface_sets(self) -> None:
        # Each profile-specific remote LH driver class advertises the matching
        # interface set as its ClassVar. The factory picks the class per
        # instance from the connection card; orca reads type(driver).interfaces.
        assert RemoteLiquidHandlerDriver.interfaces == frozenset({"ILiquidHandler"})
        assert RemoteLiquidHandlerWithProtocolDriver.interfaces == frozenset(
            {"ILiquidHandler", "IProtocolRunner"}
        )
        assert RemoteProtocolOnlyLiquidHandlerDriver.interfaces == frozenset(
            {"IProtocolRunner"}
        )

    def test_liquid_handler_profiles_advertise_provides_state(self) -> None:
        assert RemoteLiquidHandlerDriver.provides_state is True
        assert RemoteLiquidHandlerWithProtocolDriver.provides_state is True
        assert RemoteProtocolOnlyLiquidHandlerDriver.provides_state is True


class TestAMovePlateWithNoPlateCrossesTheWire:
    @pytest.mark.asyncio
    async def test_a_move_with_no_plate_on_record_reaches_orca_client_as_a_valid_request(self) -> None:
        """The wire drops None fields, so a move naming no plate must still parse on orca-client's side."""
        factory, controller = _make_factory(
            mode=WorkflowRunMode.LIVE, profiles={"opentrons": frozenset({"ILiquidHandler"})},
        )
        live = _sdk_build(factory, LiquidHandlerProtocol, "opentrons").driver
        assert isinstance(live, RemoteLiquidHandlerDriver)

        await live.move_plate(MovePlateRequest(plate=None, from_position="slot_1", to_position="slot_2"))

        sent = MovePlateRequest.model_validate(controller.calls[0].params)
        assert sent.plate is None
        assert (sent.from_position, sent.to_position) == ("slot_1", "slot_2")

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
from cheshire_drivers.protocol_runner_models import RunProtocolRequest
from cheshire_drivers.shaker_models import ShakeRequest
from cheshire_drivers.sims import (
    SimCentrifugeDriver,
    SimDelidderDriver,
    SimLiquidHandlerWithProtocolDriver,
    SimPlateWasherDriver,
    SimReaderDriver,
    SimSealerDriver,
    SimShakerDriver,
)

from orca.devices.centrifuge import Centrifuge
from orca.devices.devices import Delidder, LiquidHandlerProtocol, PlateWasher, Reader
from orca.devices.shaker import Shaker
from orca.devices.sealer import Sealer
from orca.resource_models.devices import Device
from orca.runtime.device_factory_context import use_device_factory
from orca.runtime.run_modes import WorkflowRunMode

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
from tests.gateway.mode_doubles import DeclaredDevice, unseeded_await


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
        resend_on_reconnect: bool = True,
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
    profile_map = dict(profiles or {})

    def _profile_source(name: str) -> Optional[frozenset[str]]:
        return profile_map.get(name)

    factory = RemoteDeviceFactory(
        controller=cast(DeviceController, controller),
        default_timeout=30.0,
        mode_resolver=lambda _name: mode,
        profile_source=_profile_source,
    )
    return factory, controller


_DeviceT = TypeVar("_DeviceT", bound=Device)


def _sdk_build(
    factory: RemoteDeviceFactory, cls: Callable[[str], _DeviceT], name: str,
) -> _DeviceT:
    """Build `cls(name)` under a bound gateway factory (the no-driver SDK path).

    Mirrors a deployment author writing `Shaker("ml_star")` with the gateway
    factory bound: the ctor's `resolve_drivers` pulls its (live, sim) pair from
    `factory.build_drivers`. Devices carry a device-type kind, so the concrete
    class is the sole input.
    """
    with use_device_factory(factory):
        return cls(name)


class TestFactoryDeviceTypes:
    """Gateway SDK-ctor build wires the right Remote*/Sim* driver pair per device."""

    def test_shaker_returns_shaker_with_remote_and_sim(self) -> None:
        factory, _ = _make_factory()
        device = _sdk_build(factory, Shaker, "shaker_1")
        assert isinstance(device, Shaker)
        # Both drivers are wired via SimulationManager.
        assert isinstance(device._sim_manager._live_driver, RemoteShakerDriver)
        assert isinstance(device._sim_manager._sim_driver, SimShakerDriver)

    def test_centrifuge_returns_centrifuge_with_remote_and_sim(self) -> None:
        factory, _ = _make_factory()
        device = _sdk_build(factory, Centrifuge, "cent_1")
        assert isinstance(device, Centrifuge)
        assert isinstance(device._sim_manager._live_driver, RemoteCentrifugeDriver)
        assert isinstance(device._sim_manager._sim_driver, SimCentrifugeDriver)

    def test_sealer_returns_sealer_with_remote_and_sim(self) -> None:
        factory, _ = _make_factory()
        device = _sdk_build(factory, Sealer, "seal_1")
        assert isinstance(device, Sealer)
        assert isinstance(device._sim_manager._live_driver, RemoteSealerDriver)
        assert isinstance(device._sim_manager._sim_driver, SimSealerDriver)

    def test_reader_returns_reader_with_remote_and_sim(self) -> None:
        factory, _ = _make_factory()
        device = _sdk_build(factory, Reader, "rdr_1")
        assert isinstance(device, Reader)
        assert isinstance(device._sim_manager._live_driver, RemoteReaderDriver)
        assert isinstance(device._sim_manager._sim_driver, SimReaderDriver)

    def test_delidder_returns_delidder_with_remote_and_sim(self) -> None:
        factory, _ = _make_factory()
        device = _sdk_build(factory, Delidder, "delid_1")
        assert isinstance(device, Delidder)
        assert isinstance(device._sim_manager._live_driver, RemoteDelidderDriver)
        assert isinstance(device._sim_manager._sim_driver, SimDelidderDriver)

    def test_plate_washer_returns_plate_washer_with_remote_and_sim(self) -> None:
        factory, _ = _make_factory()
        device = _sdk_build(factory, PlateWasher, "pw_1")
        assert isinstance(device, PlateWasher)
        assert isinstance(device._sim_manager._live_driver, RemotePlateWasherDriver)
        assert isinstance(device._sim_manager._sim_driver, SimPlateWasherDriver)

    def test_liquid_handler_cold_start_defaults_to_plr_only(self) -> None:
        """No connection card yet -> plr-only LIVE profile (safe default).

        Before an orca-client connects there is no advertised profile, so the
        factory picks the plr-only live driver (advertises {ILiquidHandler}).
        A rebuild after the client connects upgrades the profile. The sim slot
        is always the protocol-capable composite so PURE_SIM run_protocol works
        regardless of the live profile (the operator surface reads the live
        driver, not the sim).
        """
        factory, _ = _make_factory()
        device = _sdk_build(factory, LiquidHandlerProtocol, "lh_1")
        assert isinstance(device, LiquidHandlerProtocol)
        live = device._sim_manager._live_driver
        assert type(live) is RemoteLiquidHandlerDriver
        assert live.interfaces == frozenset({"ILiquidHandler"})
        assert isinstance(
            device._sim_manager._sim_driver, SimLiquidHandlerWithProtocolDriver
        )


class TestLiquidHandlerProfileSelection:
    """The LH live driver advertises the connection-card profile per instance.

    The profile source is the orca-client's advertised interface set
    (DeviceConnectInfo.interfaces, surfaced through the connection tracker).
    The factory reads it at build time and picks the matching live driver
    class so the orca facade + connect-time superset check see the device's
    true capability set, not a frozen {ILiquidHandler}.
    """

    def test_both_profile_advertises_lh_and_protocol(self) -> None:
        factory, _ = _make_factory(
            profiles={"mlstar": frozenset({"ILiquidHandler", "IProtocolRunner"})},
        )
        device = _sdk_build(factory, LiquidHandlerProtocol, "mlstar")
        live = device._sim_manager._live_driver
        assert type(live) is RemoteLiquidHandlerWithProtocolDriver
        assert live.interfaces == frozenset({"ILiquidHandler", "IProtocolRunner"})
        assert isinstance(
            device._sim_manager._sim_driver, SimLiquidHandlerWithProtocolDriver
        )

    def test_protocol_only_profile_advertises_protocol_runner(self) -> None:
        factory, _ = _make_factory(
            profiles={"bravo": frozenset({"IProtocolRunner"})},
        )
        device = _sdk_build(factory, LiquidHandlerProtocol, "bravo")
        live = device._sim_manager._live_driver
        assert type(live) is RemoteProtocolOnlyLiquidHandlerDriver
        assert live.interfaces == frozenset({"IProtocolRunner"})
        # Sim slot must support run_protocol so PURE_SIM bravo workflows run.
        assert callable(getattr(device._sim_manager._sim_driver, "run_protocol", None))

    def test_plr_only_profile_advertises_lh_only(self) -> None:
        factory, _ = _make_factory(
            profiles={"opentrons": frozenset({"ILiquidHandler"})},
        )
        device = _sdk_build(factory, LiquidHandlerProtocol, "opentrons")
        live = device._sim_manager._live_driver
        assert type(live) is RemoteLiquidHandlerDriver
        assert live.interfaces == frozenset({"ILiquidHandler"})
        assert isinstance(
            device._sim_manager._sim_driver, SimLiquidHandlerWithProtocolDriver
        )


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
        live = device._sim_manager._live_driver
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
        live = device._sim_manager._live_driver
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
        live = device._sim_manager._live_driver
        live_cls = type(live)
        assert live_cls is RemoteLiquidHandlerWithProtocolDriver
        await live.run_protocol(
            RunProtocolRequest(protocol_filepath="x.pro", params={})
        )
        assert [c.command for c in controller.calls] == ["run_protocol"]


class TestNoDriverSdkBuildDrivers:
    """`build_drivers` powers the no-driver SDK ctor flow via `resolve_drivers`.

    A deployment-package author writes ``Shaker(name="x")`` (or
    ``Transporter(name="x", teachpoint_store=...)``) and orca-core's
    ``resolve_drivers`` calls ``factory.build_drivers(device_type, name)``
    on the active factory.
    """

    def test_shaker_returns_remote_and_sim_pair(self) -> None:
        factory, _ = _make_factory()
        live, sim = factory.build_drivers("shaker", "shaker_1")
        assert isinstance(live, RemoteShakerDriver)
        assert isinstance(sim, SimShakerDriver)

    def test_transporter_returns_remote_and_sim_pair(self) -> None:
        """Transporter now has a wire-forwarding driver paired with a sim.

        `RemoteTransporterDriver` implements
        `ITransporterDriver` as a thin wire-forwarder over
        `DeviceController.execute_command`. The factory wires it into
        the live slot just like every device driver; sim driver fills
        the in-process slot. orca-core's `SimulationManager` toggles
        per dispatch via the factory's mode resolver.
        """
        from cheshire_drivers.sims import SimTransporterDriver

        from orca.gateway.remote_transporter_driver import (
            RemoteTransporterDriver,
        )

        factory, _ = _make_factory()
        live, sim = factory.build_drivers("transporter", "transporter_1")
        assert isinstance(live, RemoteTransporterDriver)
        assert isinstance(sim, SimTransporterDriver)

    def test_translator_pair_declares_one_carriage_on_both_slots(self) -> None:
        """A hosted translator must still read as a single carriage.

        `single_carriage` is read off the LIVE driver at topology-build time,
        before any device bridge has connected, so the proxy cannot learn it
        over the wire. Handing a translator the plain transporter pair is what
        let the reservation layer promise both endpoints of one physical carriage.
        """
        from cheshire_drivers.translator_driver import SimTranslatorDriver

        from orca.gateway.remote_transporter_driver import RemoteTranslatorDriver

        factory, _ = _make_factory()
        live, sim = factory.build_drivers("translator", "translator_1")
        assert isinstance(live, RemoteTranslatorDriver)
        assert isinstance(sim, SimTranslatorDriver)
        assert live.single_carriage is True
        assert sim.single_carriage is True

    def test_unknown_device_type_raises(self) -> None:
        factory, _ = _make_factory()
        with pytest.raises(ValueError, match="Unknown device type"):
            factory.build_drivers("nonexistent_kind", "x")


class TestRemoteDriverSendIncludesEffectiveMode:
    """Each Remote*Driver forwards `effective_mode` from the resolver."""

    @pytest.mark.asyncio
    async def test_shaker_shake_includes_effective_mode_live(self) -> None:
        factory, controller = _make_factory(mode=WorkflowRunMode.LIVE)
        device = _sdk_build(factory, Shaker, "shaker_1")
        # Pull the live driver and exercise its driver-level method directly.
        live = device._sim_manager._live_driver
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
        Seeded LIVE, because an unseeded caller resolves to PURE_SIM and would
        be talking to the sim stub.
        """
        from orca.runtime.run_modes import current_run_mode

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
        live = device._sim_manager._live_driver
        assert isinstance(live, RemoteShakerDriver)

        await live.connect()
        await live.disconnect()

        assert [c.command for c in controller.calls] == ["connect", "disconnect"]
        assert live.is_initialized is False

    @pytest.mark.asyncio
    async def test_shaker_shake_with_device_sim_mode(self) -> None:
        factory, controller = _make_factory(mode=WorkflowRunMode.DEVICE_SIM)
        device = _sdk_build(factory, Shaker, "shaker_1")
        live = device._sim_manager._live_driver
        assert isinstance(live, RemoteShakerDriver)
        await live.shake(ShakeRequest(speed=200.0, duration=10.0))
        assert controller.calls[0].effective_mode is WorkflowRunMode.DEVICE_SIM

    @pytest.mark.asyncio
    async def test_centrifuge_includes_effective_mode(self) -> None:
        factory, controller = _make_factory(mode=WorkflowRunMode.LIVE)
        device = _sdk_build(factory, Centrifuge, "cent_1")
        live = device._sim_manager._live_driver
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
    ask would drive a real instrument for a device declared DEVICE_SIM.
    """

    @staticmethod
    def _factory_over(
        runtime_slot: List[Optional[_FakeRuntime]],
    ) -> tuple[RemoteDeviceFactory, _FakeController]:
        controller = _FakeController()
        factory = RemoteDeviceFactory(
            controller=cast(DeviceController, controller),
            mode_resolver=system_mode_resolver(
                lambda: runtime_slot[0], when_unseeded=WorkflowRunMode.LIVE,
            ),
        )
        return factory, controller

    @pytest.mark.asyncio
    async def test_a_driver_refuses_to_dispatch_while_the_runtime_is_down(
        self,
    ) -> None:
        """A driver built before the runtime cannot read any declaration, and
        a device that declared DEVICE_SIM would be driven live if it guessed."""
        runtime_slot: List[Optional[_FakeRuntime]] = [None]
        factory, controller = self._factory_over(runtime_slot)
        device = _sdk_build(factory, Shaker, "shaker_1")
        live = device._sim_manager._live_driver
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
        live = device._sim_manager._live_driver
        assert isinstance(live, RemoteShakerDriver)

        # Runtime up, and it declares the device sim-side. The driver built
        # before it existed honours that without being rebuilt; drivers do not
        # cache the mode.
        runtime_slot[0] = _FakeRuntime(_FakeSystem(
            DeclaredDevice(WorkflowRunMode.DEVICE_SIM, name="shaker_1"),
        ))
        await unseeded_await(live.shake(ShakeRequest(speed=100.0, duration=5.0)))
        assert controller.calls[-1].effective_mode is WorkflowRunMode.DEVICE_SIM

        # And a device it declares with no override still reaches the wire.
        runtime_slot[0] = _FakeRuntime(_FakeSystem(
            DeclaredDevice(None, name="shaker_1"),
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
        live = device._sim_manager._live_driver
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
        live = device._sim_manager._live_driver
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

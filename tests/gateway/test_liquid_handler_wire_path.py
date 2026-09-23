"""Pin the production wire path for LiquidHandler: deck layouts and atomic ops.

After the device-layer Remote* classes were retired, the production
wire forwarding lives entirely in the driver-layer Remote*Driver pair
inside an orca-core LiquidHandler. This file pins two end-to-end shapes
so a future regression at any layer fails loud:

  1. ``configure_liquid_handler_decks`` (orca-core's startup hook) fires
     ``configure_deck`` on the wire with the bound DeckLayoutConfig.
  2. ``LiquidHandler.aspirate(wells, volumes)`` flattens the IWell args
     into an AspirateRequest, calls ``self.driver.aspirate``, and the
     RemoteLiquidHandlerDriver forwards over the wire with the correct
     payload shape.

Together they cover the whole wire path: at runtime startup, deck layouts
reach the on-prem driver; during workflow execution,
aspirate / dispense / etc. atomic operations reach the on-prem driver.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, cast

import pytest
from pydantic import ValidationError
from cheshire_drivers.liquid_handler_models import (
    DeckLayoutConfig,
    DeckResourceConfig,
    DiscardStrandedTipsRequest,
    ReconcileHardwareStateRequest,
)
from cheshire_drivers.sims import SimLiquidHandlerWithProtocolDriver

from orca.devices.devices import LiquidHandler, LiquidHandlerProtocol
from cheshire_drivers.pipetting import MixParams, PipettingProfile
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.deck_layout_service import seeded_deck_layout_service

from orca.gateway.controller.controller import DeviceController
from orca.gateway.remote_device_factory import RemoteDeviceFactory
from orca.gateway.remote_drivers import RemoteLiquidHandlerDriver


@dataclass
class _RecordedCall:
    device_id: str
    command: str
    params: Optional[Dict[str, Any]]
    timeout_seconds: Optional[float]
    effective_mode: WorkflowRunMode


@dataclass
class _FakeController:
    """Records every dispatch a Remote*Driver makes."""

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
        self.calls.append(_RecordedCall(
            device_id=device_id, command=command, params=params,
            timeout_seconds=timeout_seconds, effective_mode=effective_mode,
        ))
        if self.responses:
            return self.responses.pop(0)
        return {"success": True}


@dataclass
class _StubWell:
    """Minimal IWell-shaped stub (the Protocol is structural)."""
    parent_name: str
    identifier: str
    row: int = 0
    col: int = 0
    position_x: float = 0.0
    position_y: float = 0.0
    position_z: float = 0.0
    size_x: float = 9.0
    size_y: float = 9.0
    size_z: float = 10.5

    @property
    def resource_name(self) -> str:
        return self.parent_name

    @property
    def position(self) -> str | None:
        return self.identifier


def _make_factory() -> tuple[RemoteDeviceFactory, _FakeController]:
    controller = _FakeController()
    factory = RemoteDeviceFactory(
        controller=cast(DeviceController, controller),
        default_timeout=30.0,
        mode_resolver=lambda _name: WorkflowRunMode.LIVE,
    )
    return factory, controller


def _layout(deck_type: str = "STARLet") -> DeckLayoutConfig:
    # Carriers-only: the deck config declares structure, never labware
    # (occupancy rides reconcile_deck_occupancy, a separate wire op).
    return DeckLayoutConfig(
        deck_type=deck_type,
        resources=[
            DeckResourceConfig(
                name="carrier-1",
                catalog_ref="PLT_CAR_L5AC_A00",
                rail=7,
            ),
            DeckResourceConfig(
                name="carrier-2",
                catalog_ref="TIP_CAR_480_A00",
                rail=15,
            ),
        ],
    )


class TestDeckLayoutOverWire:
    """``configure_liquid_handler_decks`` reaches the on-prem driver."""

    @pytest.mark.asyncio
    async def test_configure_deck_fires_over_wire_when_layout_bound(self) -> None:
        """The runtime-startup configure_deck path under production:
        device.resolve_deck_config_async() reads from the bound store, then
        device.driver.configure_deck(config) goes over the wire via
        RemoteLiquidHandlerDriver. Pins the resolve+forward pair."""
        from orca.runtime.run_modes import current_run_mode
        factory, controller = _make_factory()
        store = seeded_deck_layout_service()
        await store.add("default", _layout())

        from orca.runtime.device_factory_context import use_device_factory
        with use_device_factory(factory):
            device = LiquidHandler(
                "lh_1",
                deck_layout_store=store, deck_layout="default",
            )

        # SimulationManager.driver reads current_run_mode at dispatch
        # time (sim-hierarchy v3.4). Production seeds via lifecycle +
        # request middleware + thread coroutine; unit tests must seed
        # explicitly to pin the dispatch mode under assertion.
        token = current_run_mode.set(WorkflowRunMode.LIVE)
        try:
            config = await device.resolve_deck_config_async()
            assert config is not None
            await device.driver.configure_deck(config)
        finally:
            current_run_mode.reset(token)

        assert len(controller.calls) == 1
        call = controller.calls[0]
        assert call.device_id == "lh_1"
        assert call.command == "configure_deck"
        assert call.effective_mode is WorkflowRunMode.LIVE
        assert call.params is not None
        assert call.params.get("deck_type") == "STARLet"
        resources = call.params.get("resources")
        assert isinstance(resources, list) and len(resources) == 2
        names = {r["name"] for r in resources}
        assert names == {"carrier-1", "carrier-2"}

    @pytest.mark.asyncio
    async def test_resolve_deck_config_returns_none_when_no_layout_bound(self) -> None:
        """If the LH has no deck_layout bound, resolve_deck_config_async returns
        None. Caller (orca-core's configure_liquid_handler_decks) skips the
        wire call entirely; nothing fires."""
        factory, controller = _make_factory()
        from orca.runtime.device_factory_context import use_device_factory
        with use_device_factory(factory):
            device = LiquidHandler("lh_1")

        config = await device.resolve_deck_config_async()
        assert config is None
        assert controller.calls == []


class TestAtomicOpsOverWire:
    """LH bridge + RemoteLiquidHandlerDriver forwards atomic ops correctly."""

    @pytest.mark.asyncio
    async def test_aspirate_through_bridge_fires_aspirate_on_wire(self) -> None:
        """LiquidHandler.aspirate(wells, volumes) ends in a wire dispatch
        with the AspirateRequest fields flat on params. Pins the full
        bridge -> driver -> _send chain that customer @orca.action code
        triggers via ctx.device(ILiquidHandler).aspirate(...)."""
        from orca.runtime.run_modes import current_run_mode
        factory, controller = _make_factory()
        from orca.runtime.device_factory_context import use_device_factory
        with use_device_factory(factory):
            device = LiquidHandlerProtocol("lh_1")
        # Verify the live driver is the wire-forwarding one. No profile card
        # is bound here, so the LH defaults to the plr-only LIVE profile; the
        # sim slot is always the protocol-capable composite.
        assert isinstance(device._sim_manager._live_driver, RemoteLiquidHandlerDriver)
        assert isinstance(
            device._sim_manager._sim_driver, SimLiquidHandlerWithProtocolDriver
        )

        wells = [
            _StubWell(parent_name="plate_a", identifier="A1"),
            _StubWell(parent_name="plate_a", identifier="B1"),
        ]
        # Seed ContextVar so SimulationManager.driver returns the LIVE
        # driver (see test_configure_deck above for rationale).
        token = current_run_mode.set(WorkflowRunMode.LIVE)
        try:
            await device.aspirate(wells, [100.0, 100.0], flow_rates=[50.0, 50.0])
        finally:
            current_run_mode.reset(token)

        assert len(controller.calls) == 1
        call = controller.calls[0]
        assert call.device_id == "lh_1"
        assert call.command == "aspirate"
        assert call.effective_mode is WorkflowRunMode.LIVE
        assert call.params is not None
        aspirations = call.params.get("aspirations")
        assert isinstance(aspirations, list) and len(aspirations) == 1
        assert aspirations[0]["labware"] == "plate_a"
        assert aspirations[0]["positions"] == ["A1", "B1"]
        assert aspirations[0]["volumes"] == [100.0, 100.0]
        assert call.params.get("flow_rates") == [50.0, 50.0]

    @pytest.mark.asyncio
    async def test_the_resolved_parameters_ride_the_aspirate_wire_call(self) -> None:
        """An author's profiles are folded into one record before the wire, so the
        pipetting height is settable from @orca.action code and the driver never
        chooses between layers."""
        from orca.runtime.run_modes import current_run_mode
        factory, controller = _make_factory()
        from orca.runtime.device_factory_context import use_device_factory
        with use_device_factory(factory):
            device = LiquidHandlerProtocol("lh_1")

        wells = [_StubWell(parent_name="plate_a", identifier="A1")]
        token = current_run_mode.set(WorkflowRunMode.LIVE)
        try:
            await device.aspirate(
                wells,
                [100.0],
                liquid_class=PipettingProfile(height=1.0, flow_rate=20.0),
                technique=PipettingProfile(height=7.5),
            )
        finally:
            current_run_mode.reset(token)

        parameters = controller.calls[0].params.get("parameters")
        assert parameters is not None
        assert parameters["height"] == 7.5, "the step outranks the liquid"
        assert parameters["flow_rate"] == 20.0, "and inherits what it does not state"

    @pytest.mark.asyncio
    async def test_a_mixs_own_rate_outranks_the_layers_on_the_wire(self) -> None:
        """A mix names the rate its own cycles run at, which is narrower than
        anything the liquid or the step said. Drop that and every mix quietly
        runs at whatever the layers decided instead."""
        from orca.runtime.run_modes import current_run_mode
        factory, controller = _make_factory()
        from orca.runtime.device_factory_context import use_device_factory
        with use_device_factory(factory):
            device = LiquidHandlerProtocol("lh_1")

        wells = [_StubWell(parent_name="plate_a", identifier="A1")]
        token = current_run_mode.set(WorkflowRunMode.LIVE)
        try:
            await device.mix(
                wells,
                MixParams(volume=80.0, repetitions=5, flow_rate=150.0),
                liquid_class=PipettingProfile(flow_rate=20.0, height=2.0),
            )
        finally:
            current_run_mode.reset(token)

        parameters = controller.calls[0].params.get("parameters")
        assert parameters is not None
        assert parameters["flow_rate"] == 150.0, "the mix's own rate is the narrowest layer"
        assert parameters["height"] == 2.0, "and the liquid still supplies the rest"


class TestHeadConfigurationOverWire:
    """get_head_configuration is a new ILiquidHandler abstract method, so the wire driver must
    forward it. Without the forwarder the remote driver could not even instantiate."""

    @pytest.mark.asyncio
    async def test_get_head_configuration_forwards_and_parses(self) -> None:
        from orca.gateway.remote_drivers import RemoteLiquidHandlerDriver
        from cheshire_drivers.liquid_handler_models import GetHeadConfigurationRequest

        controller = _FakeController()
        controller.responses.append(
            {"groups": [{"channels": [0, 1, 2, 3, 4, 5, 6, 7], "max_volume_ul": 1000.0}]}
        )
        driver = RemoteLiquidHandlerDriver(
            name="lh_1",
            controller=cast(DeviceController, controller),
            mode_resolver=lambda _name: WorkflowRunMode.LIVE,
        )

        result = await driver.get_head_configuration(GetHeadConfigurationRequest())

        assert controller.calls[0].command == "get_head_configuration"
        assert result.independent_volume_count == 1
        assert result.nozzle_count == 8


class TestHardwareReconcileOverWire:
    """The reconcile pair is on an automatic recovery path (the engine fires it
    before a RETRY_OP), so the payload and the strict response parse are pinned
    the same as any other atomic op."""

    @pytest.mark.asyncio
    async def test_reconcile_hardware_state_forwards_and_parses_the_report(self) -> None:
        controller = _FakeController()
        controller.responses.append({
            "success": True,
            "checked": True,
            "session_recovered": True,
            "mounts": [{
                "mount": "left", "sensor": "absent",
                "tracked_tips": 8, "outcome": "cleared_lost_tips",
            }],
            "requires_intervention": False,
            "message": "cleared tip records on mount(s) left",
        })
        driver = RemoteLiquidHandlerDriver(
            name="lh_1",
            controller=cast(DeviceController, controller),
            mode_resolver=lambda _name: WorkflowRunMode.LIVE,
        )

        result = await driver.reconcile_hardware_state(ReconcileHardwareStateRequest())

        assert controller.calls[0].command == "reconcile_hardware_state"
        assert controller.calls[0].params == {}
        assert result.session_recovered is True
        assert result.mounts[0].outcome == "cleared_lost_tips"
        assert result.requires_intervention is False

    @pytest.mark.asyncio
    async def test_discard_stranded_tips_forwards_and_parses_the_report(self) -> None:
        controller = _FakeController()
        controller.responses.append({
            "success": True, "checked": True, "session_recovered": False,
            "mounts": [{
                "mount": "left", "sensor": "absent",
                "tracked_tips": 0, "outcome": "in_sync",
            }],
            "requires_intervention": False,
            "message": "hardware and driver state agree",
        })
        driver = RemoteLiquidHandlerDriver(
            name="lh_1",
            controller=cast(DeviceController, controller),
            mode_resolver=lambda _name: WorkflowRunMode.LIVE,
        )

        result = await driver.discard_stranded_tips(DiscardStrandedTipsRequest())

        assert controller.calls[0].command == "discard_stranded_tips"
        assert controller.calls[0].params == {}
        assert result.mounts[0].outcome == "in_sync"

    @pytest.mark.asyncio
    async def test_an_agent_answering_the_wrong_shape_is_refused_not_guessed(self) -> None:
        """The engine acts on this report, so a payload that does not parse must
        raise rather than arrive as a silently empty all-clear."""
        controller = _FakeController()
        controller.responses.append({"checked": True, "mounts": [{"mount": "left"}]})
        driver = RemoteLiquidHandlerDriver(
            name="lh_1",
            controller=cast(DeviceController, controller),
            mode_resolver=lambda _name: WorkflowRunMode.LIVE,
        )

        with pytest.raises(ValidationError):
            await driver.reconcile_hardware_state(ReconcileHardwareStateRequest())

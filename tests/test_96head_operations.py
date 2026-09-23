"""Tests for 96-head liquid handler operations.

Verifies that LiquidHandlerProtocol bridges aspirate96/dispense96/pick_up_tips96/
drop_tips96/return_tips96 to the serializable ILiquidHandlerDriver requests.
Because LiquidHandlerProtocol is a concrete ABC subclass of ILiquidHandler, each
passing bridge call also proves the interface declares the method.
"""

import pytest

from cheshire_drivers import (
    Aspirate96Request,
    Dispense96Request,
    DropTips96Request,
    PickUpTips96Request,
    RecordingLiquidHandlerDriver,
    SimLiquidHandlerDriver,
)
from orca.devices.devices import LiquidHandlerProtocol
from orca.runtime.sim_labware import SimPlate, SimTipRack
from orca.runtime.device_factory_context import use_device_factory
from orca.runtime.device_factory_protocol import DriverPairElement


class _RecorderFactory:
    """Inject a RecordingLiquidHandlerDriver into the no-driver SDK ctor."""

    def __init__(self, driver: DriverPairElement) -> None:
        self._d = driver

    def build_drivers(
        self, device_type: str, name: str, *, deck_modeling: bool = False,
    ) -> tuple[DriverPairElement, DriverPairElement]:
        return self._d, self._d


class TestLiquidHandlerBridge96Head:
    """LiquidHandlerProtocol bridges 96-head calls to serializable driver requests."""

    @pytest.fixture
    def recording_lh(self) -> tuple[LiquidHandlerProtocol, RecordingLiquidHandlerDriver]:
        inner = SimLiquidHandlerDriver("test_lh")
        recorder = RecordingLiquidHandlerDriver(inner)
        with use_device_factory(_RecorderFactory(recorder)):
            lh = LiquidHandlerProtocol("test_lh")
        return lh, recorder

    @pytest.mark.asyncio
    async def test_aspirate96_bridges_to_driver(
        self, recording_lh: tuple[LiquidHandlerProtocol, RecordingLiquidHandlerDriver],
    ) -> None:
        lh, recorder = recording_lh
        await lh.aspirate96(SimPlate("source_plate"), volume=50.0, flow_rate=10.0)
        assert len(recorder.calls) == 1
        call = recorder.calls[0]
        assert call.method == "aspirate96"
        assert call.args["labware"] == "source_plate"
        assert call.args["volume"] == 50.0
        assert call.args["flow_rate"] == 10.0

    @pytest.mark.asyncio
    async def test_dispense96_bridges_to_driver(
        self, recording_lh: tuple[LiquidHandlerProtocol, RecordingLiquidHandlerDriver],
    ) -> None:
        lh, recorder = recording_lh
        await lh.dispense96(SimPlate("dest_plate"), volume=50.0, liquid_height=2.0)
        assert len(recorder.calls) == 1
        call = recorder.calls[0]
        assert call.method == "dispense96"
        assert call.args["labware"] == "dest_plate"
        assert call.args["volume"] == 50.0
        assert call.args["liquid_height"] == 2.0

    @pytest.mark.asyncio
    async def test_pick_up_tips96_bridges_to_driver(
        self, recording_lh: tuple[LiquidHandlerProtocol, RecordingLiquidHandlerDriver],
    ) -> None:
        lh, recorder = recording_lh
        await lh.pick_up_tips96(SimTipRack("tip_rack_1", True))
        assert len(recorder.calls) == 1
        call = recorder.calls[0]
        assert call.method == "pick_up_tips96"
        assert call.args["tip_rack"] == "tip_rack_1"

    @pytest.mark.asyncio
    async def test_drop_tips96_to_waste(
        self, recording_lh: tuple[LiquidHandlerProtocol, RecordingLiquidHandlerDriver],
    ) -> None:
        lh, recorder = recording_lh
        await lh.drop_tips96()
        assert len(recorder.calls) == 1
        call = recorder.calls[0]
        assert call.method == "drop_tips96"
        assert call.args["to_waste"] is True

    @pytest.mark.asyncio
    async def test_drop_tips96_to_rack(
        self, recording_lh: tuple[LiquidHandlerProtocol, RecordingLiquidHandlerDriver],
    ) -> None:
        lh, recorder = recording_lh
        await lh.drop_tips96(tip_rack=SimTipRack("tip_rack_1", True))
        assert len(recorder.calls) == 1
        call = recorder.calls[0]
        assert call.method == "drop_tips96"
        assert call.args["to_waste"] is False
        assert call.args["tip_rack"] == "tip_rack_1"

    @pytest.mark.asyncio
    async def test_return_tips96_bridges_to_driver(
        self, recording_lh: tuple[LiquidHandlerProtocol, RecordingLiquidHandlerDriver],
    ) -> None:
        lh, recorder = recording_lh
        await lh.return_tips96()
        assert len(recorder.calls) == 1
        assert recorder.calls[0].method == "return_tips96"

    @pytest.mark.asyncio
    async def test_aspirate96_minimal_args(
        self, recording_lh: tuple[LiquidHandlerProtocol, RecordingLiquidHandlerDriver],
    ) -> None:
        """Only labware and volume are required."""
        lh, recorder = recording_lh
        await lh.aspirate96(SimPlate("plate_1"), volume=25.0)
        call = recorder.calls[0]
        assert call.args["labware"] == "plate_1"
        assert call.args["volume"] == 25.0
        assert call.args["flow_rate"] is None
        assert call.args["liquid_height"] is None

    @pytest.mark.asyncio
    async def test_dispense96_minimal_args(
        self, recording_lh: tuple[LiquidHandlerProtocol, RecordingLiquidHandlerDriver],
    ) -> None:
        """Only labware and volume are required."""
        lh, recorder = recording_lh
        await lh.dispense96(SimPlate("plate_1"), volume=25.0)
        call = recorder.calls[0]
        assert call.args["labware"] == "plate_1"
        assert call.args["volume"] == 25.0

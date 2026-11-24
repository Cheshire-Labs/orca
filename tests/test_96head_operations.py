"""Tests for 96-head liquid handler operations.

Verifies that LiquidHandlerProtocol bridges aspirate96/dispense96/pick_up_tips96/
drop_tips96/return_tips96 to the serializable ILiquidHandlerDriver requests.
Because LiquidHandlerProtocol is a concrete ABC subclass of ILiquidHandler, each
passing bridge call also proves the interface declares the method.
"""

import pytest

from cheshire_drivers import (
    RecordingLiquidHandlerDriver,
    SimLiquidHandlerDriver,
)
from tests.real_device_path import connected, the_real_device_path
from orca.devices.devices import LiquidHandlerProtocol
from orca.resource_models.sim_labware import SimPlate, SimTipRack


class TestLiquidHandlerBridge96Head:
    """LiquidHandlerProtocol bridges 96-head calls to serializable driver requests."""

    @pytest.fixture
    async def recording_lh(self) -> tuple[LiquidHandlerProtocol, RecordingLiquidHandlerDriver]:
        recorder = RecordingLiquidHandlerDriver(SimLiquidHandlerDriver("test_lh"))
        the_real_device_path().simulate_with("test_lh", recorder)
        return await connected(LiquidHandlerProtocol("test_lh")), recorder

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

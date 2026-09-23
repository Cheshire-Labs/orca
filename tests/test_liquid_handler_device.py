"""Tests for LiquidHandlerProtocol device with well-level operations.

Verifies that the orca LiquidHandlerProtocol device correctly bridges
object-based ILiquidHandler calls to string-based ILiquidHandlerDriver.
"""

import pytest

from cheshire_drivers.interfaces import ILiquidHandlerDriver
from cheshire_drivers.liquid_handler_models import (
    AspirateRequest,
    DispenseRequest,
    DropTipsRequest,
    LabwareStateResponse,
    PickUpTipsRequest,
)
from cheshire_drivers.sims import SimLiquidHandlerDriver
from cheshire_drivers.plr.liquid_handler import ChatterboxLiquidHandlerDriver
from orca.devices.devices import LiquidHandlerProtocol
from orca.runtime.device_factory_protocol import DriverPairElement
from orca.runtime.device_factory_context import use_device_factory


class _LhInjectFactory:
    """Inject a specific LH driver instance via the no-driver SDK ctor.

    The injected driver may be a real ``ILiquidHandlerDriver`` subclass
    (Chatterbox, Sim*) or a duck-typed test double that implements just
    the methods the test exercises. ``DriverPairElement`` is the typed
    contract on the wire; runtime tests pass anything LH-shaped.
    """

    def __init__(self, driver: ILiquidHandlerDriver) -> None:
        self._d = driver

    def build_drivers(
        self, device_type: str, name: str,
    ) -> tuple[DriverPairElement, DriverPairElement]:
        return self._d, self._d


def _make_lh(name: str, driver: ILiquidHandlerDriver) -> LiquidHandlerProtocol:
    """Build a LiquidHandlerProtocol wired to a specific test driver via the factory."""
    with use_device_factory(_LhInjectFactory(driver)):
        return LiquidHandlerProtocol(name)


class TestLiquidHandlerWithChatterbox:

    def test_accepts_chatterbox_driver(self) -> None:
        driver = ChatterboxLiquidHandlerDriver(num_channels=8)
        lh = _make_lh("test_lh", driver)
        assert lh.driver is driver

    # Chatterbox-driver isinstance check dropped: wiring is pinned by
    # `test_accepts_chatterbox_driver`; behavior by `TestLiquidHandlerBridgeMultiRack`.


class _FakeWell:
    """Minimal IWell-shaped object for bridge unit tests."""

    def __init__(self, parent: str, ident: str) -> None:
        self._parent = parent
        self._ident = ident

    @property
    def parent_name(self) -> str:
        return self._parent

    @property
    def identifier(self) -> str:
        return self._ident

    @property
    def resource_name(self) -> str:
        return self._parent

    @property
    def position(self) -> str | None:
        return self._ident


class _RecordingDriver(SimLiquidHandlerDriver):
    """Captures the AspirateRequest / DispenseRequest / PickUpTipsRequest /
    DropTipsRequest the bridge sends to the driver layer. The multi-rack audit
    needed test coverage that exercises the orca-core LiquidHandlerProtocol bridge
    directly so the per-labware grouping logic stays load-bearing.

    Subclassing ``SimLiquidHandlerDriver`` (which already satisfies
    ``ILiquidHandlerDriver``) lets the no-driver SDK ctor + factory accept
    this driver without ``# type: ignore`` while still overriding only the
    four atomic-op methods the bridge tests exercise.
    """

    def __init__(self) -> None:
        super().__init__("recording_driver")
        self.aspirate_request: AspirateRequest | None = None
        self.dispense_request: DispenseRequest | None = None
        self.pick_up_tips_request: PickUpTipsRequest | None = None
        self.drop_tips_request: DropTipsRequest | None = None

    async def aspirate(self, request: AspirateRequest) -> LabwareStateResponse:
        self.aspirate_request = request
        return LabwareStateResponse(success=True)

    async def dispense(self, request: DispenseRequest) -> LabwareStateResponse:
        self.dispense_request = request
        return LabwareStateResponse(success=True)

    async def pick_up_tips(self, request: PickUpTipsRequest) -> LabwareStateResponse:
        self.pick_up_tips_request = request
        return LabwareStateResponse(success=True)

    async def drop_tips(self, request: DropTipsRequest) -> LabwareStateResponse:
        self.drop_tips_request = request
        return LabwareStateResponse(success=True)


class TestLiquidHandlerBridgeMultiRack:
    """The bridge must carry channels from multiple racks/plates to
    the driver. Earlier code collapsed the per-channel labware list to
    ``wells[0].parent_name`` and discarded the rest, producing wrong-answer
    runs that completed without error."""

    @pytest.mark.asyncio
    async def test_pick_up_tips_groups_by_rack(self) -> None:
        driver = _RecordingDriver()
        lh = _make_lh("test_lh", driver)
        await lh.pick_up_tips([
            _FakeWell("rack_a", "A1"),
            _FakeWell("rack_a", "B1"),
            _FakeWell("rack_b", "C1"),
            _FakeWell("rack_b", "D1"),
        ])
        assert driver.pick_up_tips_request is not None
        picks = driver.pick_up_tips_request.picks
        assert len(picks) == 2
        assert picks[0].tip_rack == "rack_a"
        assert picks[0].positions == ["A1", "B1"]
        assert picks[1].tip_rack == "rack_b"
        assert picks[1].positions == ["C1", "D1"]

    @pytest.mark.asyncio
    async def test_pick_up_tips_preserves_channel_order_across_runs(self) -> None:
        """Channels alternating racks must produce one TipPick per consecutive run."""
        driver = _RecordingDriver()
        lh = _make_lh("test_lh", driver)
        await lh.pick_up_tips([
            _FakeWell("rack_a", "A1"),
            _FakeWell("rack_b", "A1"),
            _FakeWell("rack_a", "A2"),
        ])
        picks = driver.pick_up_tips_request.picks
        assert [(p.tip_rack, p.positions) for p in picks] == [
            ("rack_a", ["A1"]),
            ("rack_b", ["A1"]),
            ("rack_a", ["A2"]),
        ]

    @pytest.mark.asyncio
    async def test_aspirate_groups_by_plate(self) -> None:
        driver = _RecordingDriver()
        lh = _make_lh("test_lh", driver)
        await lh.aspirate(
            [
                _FakeWell("plate_a", "A1"),
                _FakeWell("plate_a", "B1"),
                _FakeWell("plate_b", "A1"),
            ],
            volumes=[10.0, 20.0, 30.0],
        )
        assert driver.aspirate_request is not None
        aspirations = driver.aspirate_request.aspirations
        assert len(aspirations) == 2
        assert aspirations[0].labware == "plate_a"
        assert aspirations[0].positions == ["A1", "B1"]
        assert aspirations[0].volumes == [10.0, 20.0]
        assert aspirations[1].labware == "plate_b"
        assert aspirations[1].positions == ["A1"]
        assert aspirations[1].volumes == [30.0]

    @pytest.mark.asyncio
    async def test_dispense_groups_by_plate(self) -> None:
        driver = _RecordingDriver()
        lh = _make_lh("test_lh", driver)
        await lh.dispense(
            [
                _FakeWell("plate_a", "A1"),
                _FakeWell("plate_b", "A1"),
                _FakeWell("plate_b", "A2"),
            ],
            volumes=[5.0, 15.0, 25.0],
        )
        assert driver.dispense_request is not None
        dispenses = driver.dispense_request.dispenses
        assert len(dispenses) == 2
        assert dispenses[0].labware == "plate_a"
        assert dispenses[0].volumes == [5.0]
        assert dispenses[1].labware == "plate_b"
        assert dispenses[1].volumes == [15.0, 25.0]

    @pytest.mark.asyncio
    async def test_drop_tips_returns_to_original_racks(self) -> None:
        driver = _RecordingDriver()
        lh = _make_lh("test_lh", driver)
        await lh.drop_tips([
            _FakeWell("rack_a", "A1"),
            _FakeWell("rack_b", "A1"),
        ])
        assert driver.drop_tips_request is not None
        assert driver.drop_tips_request.to_waste is False
        drops = driver.drop_tips_request.drops
        assert drops is not None
        assert [(d.tip_rack, d.positions) for d in drops] == [
            ("rack_a", ["A1"]),
            ("rack_b", ["A1"]),
        ]

    @pytest.mark.asyncio
    async def test_aspirate_volume_length_mismatch_raises(self) -> None:
        driver = _RecordingDriver()
        lh = _make_lh("test_lh", driver)
        with pytest.raises(ValueError, match="must be the same length"):
            await lh.aspirate(
                [_FakeWell("plate_a", "A1"), _FakeWell("plate_a", "B1")],
                volumes=[10.0],
            )

"""Trough (single-pool container) liquid-handling, orca-core side.

A true PLR Trough is one undivided container, not a grid of wells. The
LH author hands it to every channel (``lh.aspirate(reservoir, [25]*8)``)
and orca expresses that on the wire with ``positions=None`` (channel count
= number of volumes). Volume tracking reuses the existing one-well model:
a trough's pool is the pseudo-well ``"A1"``, so aspirate/dispense fold
into the ledger exactly like a plate well without any new detail type.

These tests pin the operations path end to end (bridge -> wire shape,
interpreter -> A1 tracking, ledger single-pool fold). Trough initial-state
seeding (the seed-default and the occupancy-wire driver agreement) is owned
by the ledger-deck-occupancy wave, so the ledger test seeds explicitly.
"""
import time

import pytest

from cheshire_drivers.liquid_handler_models import (
    AspirateRequest,
    ChannelError,
    DispenseRequest,
    LabwareStateResponse,
    MixRequest,
)
from cheshire_drivers.interfaces import ILiquidHandlerDriver
from cheshire_drivers.pipetting import MixParams
from cheshire_drivers.sims import SimLiquidHandlerDriver

from orca.devices.devices import LiquidHandlerProtocol
from orca.plugins.liquid_handler_interpreter import LiquidHandlerInterpreter
from orca.resource_models.labware import TroughInstance
from orca.state.projections import well_volume
from orca.state.ops_history import OpsHistory
from orca.state.records import (
    AspirateDetails,
    DispenseDetails,
    InitialStateDetails,
    OperationRecord,
    TrackingRecord,
    TrackingSource,
)
from orca.runtime.device_factory_context import use_device_factory
from orca.runtime.device_factory_protocol import DriverPairElement
from orca.runtime.sim_labware import (
    LABWARE_TYPE_MAP,
    SimTrough,
    SimTroughTemplate,
    SimWell,
)


_OK = LabwareStateResponse(success=True)


class _LhInjectFactory:
    """Inject a specific LH driver via the no-driver SDK ctor (see test_liquid_handler_device)."""

    def __init__(self, driver: ILiquidHandlerDriver) -> None:
        self._d = driver

    def build_drivers(
        self, device_type: str, name: str, *, deck_modeling: bool = False,
    ) -> tuple[DriverPairElement, DriverPairElement]:
        return self._d, self._d


def _make_lh(name: str, driver: ILiquidHandlerDriver) -> LiquidHandlerProtocol:
    with use_device_factory(_LhInjectFactory(driver)):
        return LiquidHandlerProtocol(name)


class _RecordingDriver(SimLiquidHandlerDriver):
    """Captures the request the bridge builds for aspirate/dispense/mix."""

    def __init__(self) -> None:
        super().__init__("recording_driver")
        self.aspirate_request: AspirateRequest | None = None
        self.dispense_request: DispenseRequest | None = None
        self.mix_request: MixRequest | None = None

    async def aspirate(self, request: AspirateRequest) -> LabwareStateResponse:
        self.aspirate_request = request
        return _OK

    async def dispense(self, request: DispenseRequest) -> LabwareStateResponse:
        self.dispense_request = request
        return _OK

    async def mix(self, request: MixRequest) -> LabwareStateResponse:
        self.mix_request = request
        return _OK


def _record(ops: list[OperationRecord], action_id: str = "a1") -> TrackingRecord:
    return TrackingRecord(
        execution_id="_system",
        action_id=action_id, thread_id="t1", method_id="m1",
        source=TrackingSource.OBSERVED, timestamp=time.time(),
        operations=ops,
    )


class TestTroughBridgeWireShape:
    """A single trough handed to N channels yields one wire target with no positions."""

    @pytest.mark.asyncio
    async def test_aspirate_single_trough_targets_one_pool(self) -> None:
        driver = _RecordingDriver()
        lh = _make_lh("lh", driver)
        await lh.aspirate(SimTrough(name="reservoir"), [25.0] * 8)
        req = driver.aspirate_request
        assert req is not None
        assert len(req.aspirations) == 1
        target = req.aspirations[0]
        assert target.labware == "reservoir"
        assert target.positions is None
        assert target.volumes == [25.0] * 8

    @pytest.mark.asyncio
    async def test_dispense_single_trough_targets_one_pool(self) -> None:
        driver = _RecordingDriver()
        lh = _make_lh("lh", driver)
        await lh.dispense(SimTrough(name="reservoir"), [25.0] * 8)
        req = driver.dispense_request
        assert req is not None
        assert len(req.dispenses) == 1
        target = req.dispenses[0]
        assert target.labware == "reservoir"
        assert target.positions is None
        assert target.volumes == [25.0] * 8

    @pytest.mark.asyncio
    async def test_aspirate_single_bare_well_raises(self) -> None:
        # A single itemized well must be a list; only a single-pool container
        # broadcasts. A forgotten list must not silently fan one well across channels.
        driver = _RecordingDriver()
        lh = _make_lh("lh", driver)
        with pytest.raises(TypeError, match="must be passed in a list"):
            await lh.aspirate(SimWell(parent_name="plate", identifier="A1"), [25.0])

    @pytest.mark.asyncio
    async def test_mix_single_trough_no_positions_uses_channels(self) -> None:
        driver = _RecordingDriver()
        lh = _make_lh("lh", driver)
        await lh.mix(
            SimTrough(name="reservoir"),
            MixParams(volume=50.0, repetitions=3, flow_rate=100.0),
            use_channels=[0, 1, 2, 3, 4, 5, 6, 7],
        )
        req = driver.mix_request
        assert req is not None
        assert req.labware == "reservoir"
        assert req.positions is None
        assert req.use_channels == [0, 1, 2, 3, 4, 5, 6, 7]


class TestTroughInterpreterTracksUnderA1:
    """The interpreter maps a trough's no-position channels onto the single-pool pseudo-well."""

    def test_aspirate_single_trough_tracks_a1(self) -> None:
        interp = LiquidHandlerInterpreter()
        recs = interp.interpret(
            "aspirate", (SimTrough(name="reservoir"), [25.0] * 8), {}, _OK,
            "lh", ["reservoir"], ["reservoir-id"], "a1", "t1",
        )
        assert len(recs) == 1
        rec = recs[0]
        assert rec is not None
        details = rec.details
        assert isinstance(details, AspirateDetails)
        assert details.labware == "reservoir"
        assert details.positions == ["A1"] * 8
        assert details.volumes == [25.0] * 8

    def test_single_bare_well_raises(self) -> None:
        interp = LiquidHandlerInterpreter()
        with pytest.raises(TypeError, match="must be passed in a list"):
            interp.interpret(
                "aspirate", (SimWell(parent_name="plate", identifier="A1"), [25.0]), {}, _OK,
                "lh", ["plate"], ["plate-id"], "a1", "t1",
            )

    def test_dispense_single_trough_tracks_a1(self) -> None:
        interp = LiquidHandlerInterpreter()
        recs = interp.interpret(
            "dispense", (SimTrough(name="reservoir"), [25.0] * 8), {}, _OK,
            "lh", ["reservoir"], ["reservoir-id"], "a1", "t1",
        )
        assert len(recs) == 1
        rec = recs[0]
        assert rec is not None
        details = rec.details
        assert isinstance(details, DispenseDetails)
        assert details.positions == ["A1"] * 8


class TestTroughLedgerSinglePool:
    """All channels of a trough fold into one pool, so net volume nets across channels."""

    @pytest.mark.asyncio
    async def test_aspirate_then_dispense_nets_to_seed(self) -> None:
        history = OpsHistory()
        await history.append_initial_state(
            "reservoir", InitialStateDetails(labware="reservoir", well_volumes={"A1": 1000.0}),
        )
        interp = LiquidHandlerInterpreter()
        trough = SimTrough(name="reservoir")

        asps = interp.interpret(
            "aspirate", (trough, [25.0] * 8), {}, _OK,
            "lh", ["reservoir"], ["reservoir-id"], "a1", "t1",
        )
        assert len(asps) == 1
        asp = asps[0]
        assert asp is not None
        await history.append_record(_record([asp], action_id="a1"))
        assert well_volume(await history.ops_for("reservoir"), "reservoir", "A1") == 800.0

        disps = interp.interpret(
            "dispense", (trough, [25.0] * 8), {}, _OK,
            "lh", ["reservoir"], ["reservoir-id"], "a2", "t1",
        )
        assert len(disps) == 1
        disp = disps[0]
        assert disp is not None
        await history.append_record(_record([disp], action_id="a2"))
        assert well_volume(await history.ops_for("reservoir"), "reservoir", "A1") == 1000.0


class TestTroughPartialFailureAttribution:
    """A trough is one pool, so a partial failure attributes by COUNT, not position.

    All channels share the "A1" key; with the old position-based lookup a single
    channel error zeroed all eight and overstated the pool. Count attribution marks
    exactly k channels not-transferred so the pool's net volume stays correct."""

    def _aspirate_one_error(self) -> list[OperationRecord]:
        interp = LiquidHandlerInterpreter()
        return interp.interpret_per_channel_outcomes(
            command="aspirate",
            args=(SimTrough(name="reservoir"), [25.0] * 8),
            kwargs={},
            result=LabwareStateResponse(
                success=False,
                per_channel_errors=[ChannelError(
                    channel_id=2, well_position=None, labware="reservoir",
                    attempted_volume=25.0, error_code="HamiltonE100",
                    error_message="channel 2 pressure deviation",
                )],
            ),
            device_name="lh", affected_labware=["reservoir"],
            affected_labware_ids=["reservoir-id"], action_id="a1", thread_id="t1",
        )

    def test_one_error_yields_seven_confirmed_one_failed(self) -> None:
        records = self._aspirate_one_error()
        assert len(records) == 8
        confirmed = [r for r in records if isinstance(r.details, AspirateDetails)
                     and r.details.certainty == "confirmed_transferred"]
        failed = [r for r in records if isinstance(r.details, AspirateDetails)
                  and r.details.certainty == "definitely_not_transferred"]
        assert len(confirmed) == 7
        assert len(failed) == 1
        assert isinstance(failed[0].details, AspirateDetails)
        assert failed[0].details.positions == ["A1"]
        assert failed[0].details.volumes == [0.0]
        assert failed[0].details.error_code == "HamiltonE100"
        for r in confirmed:
            assert isinstance(r.details, AspirateDetails)
            assert r.details.volumes == [25.0]

    @pytest.mark.asyncio
    async def test_pool_drains_only_the_succeeded_channels(self) -> None:
        history = OpsHistory()
        await history.append_initial_state(
            "reservoir", InitialStateDetails(labware="reservoir", well_volumes={"A1": 1000.0}),
        )
        await history.append_record(_record(self._aspirate_one_error()))
        # 7 of 8 channels drew 25 uL: the pool drops by 175 (not 200, not 0).
        assert well_volume(await history.ops_for("reservoir"), "reservoir", "A1") == 825.0

    def test_dispense_two_errors_count_attributed(self) -> None:
        interp = LiquidHandlerInterpreter()
        errs = [
            ChannelError(channel_id=c, well_position=None, labware="reservoir",
                         attempted_volume=25.0, error_code="HamiltonE100",
                         error_message="err")
            for c in (1, 5)
        ]
        records = interp.interpret_per_channel_outcomes(
            command="dispense",
            args=(SimTrough(name="reservoir"), [25.0] * 8),
            kwargs={},
            result=LabwareStateResponse(success=False, per_channel_errors=errs),
            device_name="lh", affected_labware=["reservoir"],
            affected_labware_ids=["reservoir-id"], action_id="a1", thread_id="t1",
        )
        failed = [r for r in records if isinstance(r.details, DispenseDetails)
                  and r.details.certainty == "definitely_not_transferred"]
        confirmed = [r for r in records if isinstance(r.details, DispenseDetails)
                     and r.details.certainty == "confirmed_transferred"]
        assert len(failed) == 2
        assert len(confirmed) == 6


class TestTrough96HeadLedger:
    """A 96-head op draws through the whole head, so the single pool loses
    head_size x volume, not volume once (a plate spreads one volume per well)."""

    @pytest.mark.asyncio
    async def test_aspirate96_drains_pool_by_full_head(self) -> None:
        history = OpsHistory()
        await history.append_initial_state(
            "reservoir", InitialStateDetails(labware="reservoir", well_volumes={"A1": 100_000.0}, single_pool=True),
        )
        recs = LiquidHandlerInterpreter().interpret(
            "aspirate96", (SimTrough(name="reservoir"), 10.0), {}, _OK,
            "lh", ["reservoir"], ["reservoir-id"], "a1", "t1",
        )
        assert len(recs) == 1
        rec = recs[0]
        assert rec is not None
        await history.append_record(_record([rec]))
        # 96 channels x 10 uL from the one pool: 100000 - 960, not 100000 - 10.
        assert well_volume(await history.ops_for("reservoir"), "reservoir", "A1") == 99_040.0

    @pytest.mark.asyncio
    async def test_dispense96_credits_pool_by_full_head(self) -> None:
        history = OpsHistory()
        await history.append_initial_state(
            "reservoir", InitialStateDetails(labware="reservoir", well_volumes={"A1": 0.0}, single_pool=True),
        )
        recs = LiquidHandlerInterpreter().interpret(
            "dispense96", (SimTrough(name="reservoir"), 10.0), {}, _OK,
            "lh", ["reservoir"], ["reservoir-id"], "a1", "t1",
        )
        assert len(recs) == 1
        rec = recs[0]
        assert rec is not None
        await history.append_record(_record([rec]))
        assert well_volume(await history.ops_for("reservoir"), "reservoir", "A1") == 960.0


class TestSimTroughTemplate:
    """The sim labware map resolves a trough to a single-pool SimTrough instance."""

    async def test_creates_single_pool_instance(self) -> None:
        template = SimTroughTemplate("reservoir")
        instance = await template.create_instance()
        assert isinstance(instance, TroughInstance)
        trough = instance.trough
        # The PLR object carries the minted instance name (template prefix + id).
        assert trough.resource_name == instance.name
        assert instance.name.startswith("reservoir-")
        assert instance.template_name == "reservoir"
        assert trough.position is None

    def test_registered_in_labware_type_map(self) -> None:
        assert LABWARE_TYPE_MAP["trough"] is SimTroughTemplate

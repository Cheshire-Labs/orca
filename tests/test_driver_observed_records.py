"""DRIVER_OBSERVED OperationRecord emission + trust_driver_state opt-in.

Two layers covered:
  1. LiquidHandlerInterpreter.interpret_driver_state parses LabwareStateResponse
     into INITIAL_STATE-shaped records with source=DRIVER_OBSERVED.
  2. LiquidHandler bridge's _filter_state strips labware_state when the user
     does not opt into trust_driver_state OR when the driver does not advertise
     provides_state=True.
"""

import pytest

from cheshire_drivers.liquid_handler_models import (
    AspirateRequest,
    LabwareStateResponse,
    LabwareWellState,
)

from orca.devices.devices import LiquidHandler
from orca.plugins.liquid_handler_interpreter import LiquidHandlerInterpreter
from orca.state.projections import op_count
from orca.state.records import (
    AspirateDetails,
    DeviceOperation,
    InitialStateDetails,
    OperationRecord,
    TrackingSource,
)


# --- LiquidHandlerInterpreter.interpret_driver_state ---


def test_interpret_driver_state_returns_empty_for_non_response_result() -> None:
    interp = LiquidHandlerInterpreter()
    records = interp.interpret_driver_state(
        command="aspirate",
        result={"some": "dict"},
        device_name="lh-01",
        affected_labware=[],
        affected_labware_ids=[],
        action_id="a1",
        thread_id="t1",
    )
    assert records == []


def test_interpret_driver_state_returns_empty_for_response_with_no_state() -> None:
    interp = LiquidHandlerInterpreter()
    records = interp.interpret_driver_state(
        command="aspirate",
        result=LabwareStateResponse(success=True),
        device_name="lh-01",
        affected_labware=[],
        affected_labware_ids=[],
        action_id="a1",
        thread_id="t1",
    )
    assert records == []


def test_interpret_driver_state_emits_one_record_per_labware() -> None:
    interp = LiquidHandlerInterpreter()
    response = LabwareStateResponse(
        success=True,
        labware_state={
            "src_plate": LabwareWellState(volumes={"A1": 50.0, "B1": 50.0}, tips=None),
            "tips_01": LabwareWellState(volumes=None, tips={"A1": False, "B1": True, "C1": True}),
        },
    )
    records = interp.interpret_driver_state(
        command="aspirate",
        result=response,
        device_name="lh-01",
        affected_labware=["src_plate", "tips_01"],
        affected_labware_ids=["src_plate", "tips_01"],
        action_id="a1",
        thread_id="t1",
    )
    assert len(records) == 2
    by_labware = {r.affected_labware[0]: r for r in records}

    src = by_labware["src_plate"]
    assert src.source is TrackingSource.DRIVER_OBSERVED
    assert src.operation is DeviceOperation.INITIAL_STATE
    assert isinstance(src.details, InitialStateDetails)
    assert src.details.well_volumes == {"A1": 50.0, "B1": 50.0}
    assert src.details.tip_positions_present is None

    tips = by_labware["tips_01"]
    assert tips.source is TrackingSource.DRIVER_OBSERVED
    # Only B1 and C1 are present (A1 is False).
    assert sorted(tips.details.tip_positions_present or []) == ["B1", "C1"]
    assert tips.details.well_volumes is None


def test_a_snapshot_is_never_labelled_with_the_call_that_triggered_it() -> None:
    """A snapshot says what the driver reported, not what was asked of it.
    Labelled with the call's verb, the deck's plate got a `pick_up_tips` entry
    for a pick that happened to the rack, and a post-aspirate snapshot of the
    source plate counted as a second aspirate in `op_count`."""
    interp = LiquidHandlerInterpreter()
    response = LabwareStateResponse(
        success=True,
        labware_state={"src_plate": LabwareWellState(volumes={"A1": 100.0})},
    )

    records = interp.interpret_driver_state(
        command="pick_up_tips",
        result=response,
        device_name="lh-01",
        affected_labware=["src_plate"],
        affected_labware_ids=["src_plate"],
        action_id="a1",
        thread_id="t1",
    )

    assert len(records) == 1
    assert records[0].operation is DeviceOperation.INITIAL_STATE
    assert records[0].source is TrackingSource.DRIVER_OBSERVED


def test_a_snapshot_does_not_count_as_another_aspirate() -> None:
    """The other half of the mislabelling, and the one that moves a plate.

    Every liquid-handler call writes a snapshot per labware on the deck. While
    those wore the call's verb, `op_count` counted each one, so the SMC gate
    that waits for four combines reached four early and sent a plate on before
    it was full.
    """
    interp = LiquidHandlerInterpreter()
    real_aspirate = OperationRecord(
        operation=DeviceOperation.ASPIRATE,
        device_name="lh-01",
        affected_labware=["src_plate"],
        action_id="a1",
        thread_id="t1",
        timestamp=1.0,
        details=AspirateDetails(
            labware="src_plate", positions=["A1"], volumes=[100.0],
        ),
        source=TrackingSource.OBSERVED,
    )
    snapshots = interp.interpret_driver_state(
        command="aspirate",
        result=LabwareStateResponse(
            success=True,
            labware_state={"src_plate": LabwareWellState(volumes={"A1": 100.0})},
        ),
        device_name="lh-01",
        affected_labware=["src_plate"],
        affected_labware_ids=["src_plate"],
        action_id="a1",
        thread_id="t1",
    )

    assert snapshots, "the call did produce a snapshot to be miscounted"
    assert op_count([real_aspirate, *snapshots], "src_plate") == 1


def test_interpret_driver_state_uses_initial_state_op_for_configure_deck() -> None:
    interp = LiquidHandlerInterpreter()
    response = LabwareStateResponse(
        success=True,
        labware_state={"src_plate": LabwareWellState(volumes={"A1": 100.0})},
    )
    records = interp.interpret_driver_state(
        command="configure_deck",
        result=response,
        device_name="lh-01",
        affected_labware=["src_plate"],
        affected_labware_ids=["src_plate"],
        action_id="a1",
        thread_id="t1",
    )
    assert len(records) == 1
    assert records[0].operation is DeviceOperation.INITIAL_STATE
    assert records[0].source is TrackingSource.DRIVER_OBSERVED


# --- LiquidHandler bridge _filter_state ---


class _FakeDriverProvidesState:
    provides_state = True

    async def aspirate(self, request: AspirateRequest) -> LabwareStateResponse:
        return LabwareStateResponse(
            success=True,
            labware_state={"src_plate": LabwareWellState(volumes={"A1": 25.0})},
        )

    # Stubs to satisfy abstract interface
    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def initialize(self) -> None: ...
    is_initialized = True
    name = "fake"


class _FakeDriverNoState:
    provides_state = False

    async def aspirate(self, request: AspirateRequest) -> LabwareStateResponse:
        return LabwareStateResponse(
            success=True,
            labware_state={"src_plate": LabwareWellState(volumes={"A1": 25.0})},
        )

    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def initialize(self) -> None: ...
    is_initialized = True
    name = "fake"


@pytest.mark.asyncio
async def test_bridge_filter_state_passes_through_when_trust_and_driver_provides() -> None:
    """trust=True AND driver.provides_state=True -> labware_state passes through."""
    driver = _FakeDriverProvidesState()
    # The bridge's _filter_state is the unit under test; we don't need the full
    # Device class init machinery here.
    bridge = LiquidHandler.__new__(LiquidHandler)
    bridge._trust_driver_state = True and driver.provides_state
    response = LabwareStateResponse(
        success=True,
        labware_state={"plate": LabwareWellState(volumes={"A1": 50.0})},
    )
    filtered = bridge._filter_state(response)
    assert filtered is response  # passed through unchanged
    assert filtered.labware_state != {}


@pytest.mark.asyncio
async def test_bridge_filter_state_strips_when_user_does_not_trust() -> None:
    """trust=False -> labware_state stripped even if driver provides it."""
    driver = _FakeDriverProvidesState()
    bridge = LiquidHandler.__new__(LiquidHandler)
    bridge._trust_driver_state = False  # user opted out
    response = LabwareStateResponse(
        success=True,
        labware_state={"plate": LabwareWellState(volumes={"A1": 50.0})},
    )
    filtered = bridge._filter_state(response)
    assert filtered is not response
    assert filtered.labware_state == {}
    assert filtered.success is True


@pytest.mark.asyncio
async def test_bridge_filter_state_strips_when_driver_does_not_provide() -> None:
    """driver.provides_state=False -> labware_state stripped regardless of user opt-in."""
    driver = _FakeDriverNoState()
    bridge = LiquidHandler.__new__(LiquidHandler)
    # Effective trust = user opt-in AND driver capability; if either False, strip.
    bridge._trust_driver_state = True and driver.provides_state
    assert bridge._trust_driver_state is False
    response = LabwareStateResponse(
        success=True,
        labware_state={"plate": LabwareWellState(volumes={"A1": 50.0})},
    )
    filtered = bridge._filter_state(response)
    assert filtered.labware_state == {}

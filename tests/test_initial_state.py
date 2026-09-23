"""A template's declared contents: what a labware's opening ledger entry says,
and what that entry then projects onto the driver.

The declaration is written ONCE, at birth, and is never consulted again -- so
these cover both halves: the entry the declaration produces, and the wire state
the ledger folds back out of it.

Covers:
- PlateTemplate default = wells at 0.0.
- PlateTemplate initial_state=LabwareInitialState(max_fill=True) reads IWell.max_volume.
- PlateTemplate initial_state=LabwareInitialState(uniform_volume=50.0).
- PlateTemplate initial_state=LabwareInitialState(wells={"A1": 100.0}).
- TroughTemplate default = max_volume.
- TroughTemplate initial_state=LabwareInitialState(uniform_volume=30000.0).
- TipRackTemplate default with_tips=True = every position.
- TipRackTemplate initial_state=LabwareInitialState(tip_positions=[...]).
"""
from unittest.mock import MagicMock

import pytest

from orca.resource_models.labware import (
    LabwareInitialState,
)
from orca.state.contents import LabwareContentsLedger
from orca.state.ops_history import OpsHistory
from orca.state.records import DeviceOperation, InitialStateDetails
from tests.test_helpers import (
    FactoryPlateTemplate as PlateTemplate,
    FactoryTipRackTemplate as TipRackTemplate,
    FactoryTroughTemplate as TroughTemplate,
)


def _plate_factory(num_rows: int = 2, num_cols: int = 2, well_max: float = 300.0):
    def factory(name: str, with_lid: bool | None = None):
        plate = MagicMock()
        plate.name = name
        plate.model = "test_plate"
        plate.barcode = None
        plate.num_rows = num_rows
        plate.num_cols = num_cols
        def well_lookup(identifier: str):
            well = MagicMock()
            well.max_volume = well_max
            return well
        plate.well.side_effect = well_lookup
        return plate
    return factory


def _trough_factory(max_volume: float = 50000.0):
    def factory(name: str):
        trough = MagicMock()
        trough.name = name
        trough.model = "test_trough"
        trough.max_volume = max_volume
        return trough
    return factory


def _tip_rack_factory(num_tips: int = 4):
    def factory(name: str, with_tips: bool):
        rack = MagicMock()
        rack.name = name
        rack.model = "test_rack"
        rack.num_tips = num_tips
        spots = []
        for r in range(2):
            for c in range(num_tips // 2):
                spot = MagicMock()
                spot.identifier = f"{chr(ord('A') + r)}{c + 1}"
                spots.append(spot)
        rack.tip_spots.return_value = spots
        return rack
    return factory


async def _last_initial_details(history: OpsHistory) -> InitialStateDetails:
    """Helper: pull the InitialStateDetails from the most recent record."""
    for op in reversed(await history.all_operations()):
        if op.operation == DeviceOperation.INITIAL_STATE:
            assert isinstance(op.details, InitialStateDetails)
            return op.details
    raise AssertionError("no INITIAL_STATE op in history")


class TestPlateTemplateSeed:
    @pytest.mark.asyncio
    async def test_an_undeclared_plate_writes_no_opening_entry(self) -> None:
        """It used to write a grid of zeros, which reads back as "every well
        was seen empty" -- a confident answer about wells nobody looked in, and
        it made the `unknown` provenance unreachable for plates."""
        history = OpsHistory()
        template = PlateTemplate("plate1", _plate_factory())
        instance = await template.create_instance()
        await instance.enter_record(LabwareContentsLedger(history))
        assert not [
            op for op in await history.all_operations()
            if op.operation == DeviceOperation.INITIAL_STATE
        ]

    @pytest.mark.asyncio
    async def test_declared_wells_still_pad_the_rest_to_zero(self) -> None:
        """Naming some wells IS a statement about the plate: the author
        prepared it, so the wells they left out are empty, not unspoken."""
        history = OpsHistory()
        template = PlateTemplate(
            "plate1", _plate_factory(),
            initial_state=LabwareInitialState(wells={"A1": 120.0}),
        )
        instance = await template.create_instance()
        await instance.enter_record(LabwareContentsLedger(history))
        details = await _last_initial_details(history)
        assert details.well_volumes == {
            "A1": 120.0, "A2": 0.0, "B1": 0.0, "B2": 0.0,
        }
        assert details.single_pool is False

    @pytest.mark.asyncio
    async def test_max_fill_reads_well_max_volume(self) -> None:
        history = OpsHistory()
        template = PlateTemplate(
            "plate1",
            _plate_factory(well_max=250.0),
            initial_state=LabwareInitialState(max_fill=True),
        )
        instance = await template.create_instance()
        await instance.enter_record(LabwareContentsLedger(history))
        details = await _last_initial_details(history)
        assert all(vol == 250.0 for vol in details.well_volumes.values())

    @pytest.mark.asyncio
    async def test_uniform_volume(self) -> None:
        history = OpsHistory()
        template = PlateTemplate(
            "plate1",
            _plate_factory(),
            initial_state=LabwareInitialState(uniform_volume=50.0),
        )
        instance = await template.create_instance()
        await instance.enter_record(LabwareContentsLedger(history))
        details = await _last_initial_details(history)
        assert all(vol == 50.0 for vol in details.well_volumes.values())

    @pytest.mark.asyncio
    async def test_per_well_dict(self) -> None:
        history = OpsHistory()
        template = PlateTemplate(
            "plate1",
            _plate_factory(),
            initial_state=LabwareInitialState(wells={"A1": 100.0, "B2": 200.0}),
        )
        instance = await template.create_instance()
        await instance.enter_record(LabwareContentsLedger(history))
        details = await _last_initial_details(history)
        assert details.well_volumes["A1"] == 100.0
        assert details.well_volumes["B2"] == 200.0
        assert details.well_volumes["A2"] == 0.0  # unspecified default


class TestTroughTemplateSeed:
    @pytest.mark.asyncio
    async def test_default_is_empty(self) -> None:
        # Undeclared trough seeds EMPTY (0), matching the plate + lenient driver
        # default; the old max_volume default diverged from the sim tracker's 0.
        history = OpsHistory()
        template = TroughTemplate("wash", _trough_factory(max_volume=50000.0))
        instance = await template.create_instance()
        await instance.enter_record(LabwareContentsLedger(history))
        details = await _last_initial_details(history)
        assert details.well_volumes == {"A1": 0.0}

    @pytest.mark.asyncio
    async def test_max_fill_is_max_volume(self) -> None:
        history = OpsHistory()
        template = TroughTemplate(
            "wash", _trough_factory(max_volume=50000.0),
            initial_state=LabwareInitialState(max_fill=True),
        )
        instance = await template.create_instance()
        await instance.enter_record(LabwareContentsLedger(history))
        details = await _last_initial_details(history)
        assert details.well_volumes == {"A1": 50000.0}
        assert details.single_pool is True

    @pytest.mark.asyncio
    async def test_partial_fill(self) -> None:
        history = OpsHistory()
        template = TroughTemplate(
            "partial",
            _trough_factory(max_volume=50000.0),
            initial_state=LabwareInitialState(uniform_volume=30000.0),
        )
        instance = await template.create_instance()
        await instance.enter_record(LabwareContentsLedger(history))
        details = await _last_initial_details(history)
        assert details.well_volumes == {"A1": 30000.0}


class TestTipRackTemplateSeed:
    @pytest.mark.asyncio
    async def test_default_with_tips_all_present(self) -> None:
        history = OpsHistory()
        template = TipRackTemplate("rack", _tip_rack_factory(num_tips=4), with_tips=True)
        instance = await template.create_instance()
        await instance.enter_record(LabwareContentsLedger(history))
        details = await _last_initial_details(history)
        assert set(details.tip_positions_present) == {"A1", "A2", "B1", "B2"}

    @pytest.mark.asyncio
    async def test_without_tips_empty(self) -> None:
        history = OpsHistory()
        template = TipRackTemplate("empty_rack", _tip_rack_factory(num_tips=4), with_tips=False)
        instance = await template.create_instance()
        await instance.enter_record(LabwareContentsLedger(history))
        details = await _last_initial_details(history)
        assert details.tip_positions_present == []

    @pytest.mark.asyncio
    async def test_explicit_positions(self) -> None:
        history = OpsHistory()
        template = TipRackTemplate(
            "partial",
            _tip_rack_factory(num_tips=4),
            with_tips=True,
            initial_state=LabwareInitialState(tip_positions=["A1", "A2"]),
        )
        instance = await template.create_instance()
        await instance.enter_record(LabwareContentsLedger(history))
        details = await _last_initial_details(history)
        assert details.tip_positions_present == ["A1", "A2"]


async def _projected(template, instance=None):
    """Seed the instance at birth, then read what its driver would be told."""
    instance = instance if instance is not None else await template.create_instance()
    ledger = LabwareContentsLedger(OpsHistory())
    instance.bind_contents(ledger)
    await instance.enter_record(ledger)
    return await instance.driver_well_state()


class TestDeclarationReachesTheDriver:
    """What the opening entry projects onto the driver.

    Volumes are sparse/opt-in (None when nothing is declared, so the driver keeps
    its lenient tracker); a tip rack projects every position it HAS, so the deck
    matches the declaration rather than the driver's own factory default.
    """

    async def test_plate_none_is_none(self) -> None:
        template = PlateTemplate("p", _plate_factory())
        assert await _projected(template) is None

    async def test_plate_wells_only_specified_no_zero_pad(self) -> None:
        template = PlateTemplate(
            "p", _plate_factory(), initial_state=LabwareInitialState(wells={"A1": 100.0}))
        ws = await _projected(template)
        assert ws is not None and ws.volumes == {"A1": 100.0}

    async def test_plate_max_fill_every_well(self) -> None:
        template = PlateTemplate(
            "p", _plate_factory(well_max=250.0), initial_state=LabwareInitialState(max_fill=True))
        ws = await _projected(template)
        assert ws is not None and ws.volumes is not None
        assert len(ws.volumes) == 4 and set(ws.volumes.values()) == {250.0}

    async def test_plate_uniform_every_well(self) -> None:
        template = PlateTemplate(
            "p", _plate_factory(), initial_state=LabwareInitialState(uniform_volume=50.0))
        ws = await _projected(template)
        assert ws is not None and ws.volumes is not None and set(ws.volumes.values()) == {50.0}

    async def test_trough_none_is_none(self) -> None:
        template = TroughTemplate("tr", _trough_factory())
        assert await _projected(template) is None

    async def test_trough_uniform(self) -> None:
        template = TroughTemplate(
            "tr", _trough_factory(), initial_state=LabwareInitialState(uniform_volume=3000.0))
        ws = await _projected(template)
        assert ws is not None and ws.volumes == {"A1": 3000.0}

    async def test_trough_max_fill(self) -> None:
        template = TroughTemplate(
            "tr", _trough_factory(max_volume=9000.0), initial_state=LabwareInitialState(max_fill=True))
        ws = await _projected(template)
        assert ws is not None and ws.volumes == {"A1": 9000.0}

    async def test_tiprack_with_tips_true_projects_all_present(self) -> None:
        template = TipRackTemplate("r", _tip_rack_factory(num_tips=4), with_tips=True)
        ws = await _projected(template)
        assert ws is not None and ws.tips == {"A1": True, "A2": True, "B1": True, "B2": True}

    async def test_tiprack_with_tips_false_projects_all_empty(self) -> None:
        template = TipRackTemplate("r", _tip_rack_factory(num_tips=4), with_tips=False)
        ws = await _projected(template)
        assert ws is not None and ws.tips == {"A1": False, "A2": False, "B1": False, "B2": False}

    async def test_tiprack_explicit_positions(self) -> None:
        template = TipRackTemplate(
            "r", _tip_rack_factory(num_tips=4), with_tips=True,
            initial_state=LabwareInitialState(tip_positions=["A1", "B2"]))
        ws = await _projected(template)
        assert ws is not None and ws.tips == {"A1": True, "A2": False, "B1": False, "B2": True}

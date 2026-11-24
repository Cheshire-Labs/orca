from unittest.mock import MagicMock

import pytest

from orca.state.records import (
    AspirateDetails,
    DeviceOperation,
    GenericOperationDetails,
)
from orca.resource_models.tracking_interpreter import DefaultInterpreter
from orca.plugins.liquid_handler_interpreter import LiquidHandlerInterpreter
from orca.resource_models.labware import TipRackInstance
from orca.resource_models.tip_preflight import refuse_a_pick_from_empty_spots, tips_claimed_by


class TestDefaultInterpreter:
    def test_known_command_produces_generic_details(self) -> None:
        interpreter = DefaultInterpreter()
        records = interpreter.interpret(
            command="shake",
            args=(300, 60),
            kwargs={},
            result=None,
            device_name="shaker_1",
            affected_labware=["plate_1"],
            affected_labware_ids=["id-of-plate_1"],
            action_id="a1",
            thread_id="t1",
        )
        assert len(records) == 1
        record = records[0]
        assert record.operation == DeviceOperation.SHAKE
        assert record.device_name == "shaker_1"
        assert record.affected_labware == ["plate_1"]
        assert isinstance(record.details, GenericOperationDetails)
        assert record.details.command == "shake"

    def test_a_command_with_no_operation_of_its_own_is_recorded_by_name(self) -> None:
        interpreter = DefaultInterpreter()
        records = interpreter.interpret(
            command="do_something_weird",
            args=(),
            kwargs={},
            result=None,
            device_name="dev",
            affected_labware=[],
            affected_labware_ids=[],
            action_id="a1",
            thread_id="t1",
        )
        assert [(r.operation, r.details) for r in records] == [
            (DeviceOperation.COMMAND, GenericOperationDetails(command="do_something_weird", args_repr="()")),
        ]

    def test_it_has_no_typed_request_for_any_call(self) -> None:
        interpreter = DefaultInterpreter()
        assert interpreter.request_of("pick_up_tips", (), {}) is None


class _FakeWell:
    def __init__(self, parent_name: str, identifier: str) -> None:
        self.parent_name = parent_name
        self.identifier = identifier
        self.resource_name = parent_name
        self.position = identifier


class _FakeTipSpot:
    def __init__(self, parent_name: str, identifier: str) -> None:
        self.parent_name = parent_name
        self.identifier = identifier
        self.has_tip = True

    def set_tip(self, has_tip: bool) -> None:
        self.has_tip = has_tip


class _FakeTipRack96:
    """Structurally satisfies ITipRack for the pick_up_tips96 96-head path."""

    def __init__(self, name: str, spot_ids: list[str]) -> None:
        self.name = name
        self.size_z = 1.0
        self.num_tips = len(spot_ids)
        self.has_tips = True
        self._spots = [_FakeTipSpot(name, sid) for sid in spot_ids]

    def tip_spot(self, identifier: str) -> _FakeTipSpot:
        return next(s for s in self._spots if s.identifier == identifier)

    def tip_spots(self) -> list[_FakeTipSpot]:
        return self._spots


class TestLiquidHandlerInterpreter:
    def test_aspirate(self) -> None:
        interpreter = LiquidHandlerInterpreter()
        wells = [_FakeWell("plate_1", "A1"), _FakeWell("plate_1", "A2")]
        records = interpreter.interpret(
            command="aspirate",
            args=(wells, [50.0, 50.0]),
            kwargs={},
            result=None,
            device_name="lh_1",
            affected_labware=["plate_1", "tips_96"],
            affected_labware_ids=["id-of-plate_1", "id-of-tips_96"],
            action_id="a1",
            thread_id="t1",
        )
        assert len(records) == 1
        record = records[0]
        assert record.operation == DeviceOperation.ASPIRATE
        assert isinstance(record.details, AspirateDetails)
        assert record.details.labware == "plate_1"
        assert record.details.positions == ["A1", "A2"]
        assert record.details.volumes == [50.0, 50.0]

    def test_unknown_lh_command_falls_back_to_generic(self) -> None:
        interpreter = LiquidHandlerInterpreter()
        records = interpreter.interpret(
            command="home",
            args=(),
            kwargs={},
            result=None,
            device_name="lh_1",
            affected_labware=["plate_1"],
            affected_labware_ids=["id-of-plate_1"],
            action_id="a1",
            thread_id="t1",
        )
        assert len(records) == 1
        record = records[0]
        assert record.operation == DeviceOperation.COMMAND
        assert record.details == GenericOperationDetails(command="home", args_repr="()")

    def test_pick_up_tips(self) -> None:
        interpreter = LiquidHandlerInterpreter()
        tips = [_FakeTipSpot("tips_96", "A1"), _FakeTipSpot("tips_96", "A2")]
        records = interpreter.interpret(
            command="pick_up_tips",
            args=(tips,),
            kwargs={},
            result=None,
            device_name="lh_1",
            affected_labware=["tips_96"],
            affected_labware_ids=["id-of-tips_96"],
            action_id="a1",
            thread_id="t1",
        )
        assert len(records) == 1
        record = records[0]
        assert record.operation == DeviceOperation.PICK_UP_TIPS

    def test_a_run_pick_claims_its_positions_grouped_by_rack(self) -> None:
        interpreter = LiquidHandlerInterpreter()
        tips = [
            _FakeTipSpot("tips_96", "A1"),
            _FakeTipSpot("tips_96", "A2"),
            _FakeTipSpot("tips_other", "B1"),
        ]
        request = interpreter.request_of("pick_up_tips", (tips,), {})
        assert tips_claimed_by(request, lambda name: None) == {"tips_96": ["A1", "A2"], "tips_other": ["B1"]}

    def test_a_tip_drop_claims_nothing(self) -> None:
        interpreter = LiquidHandlerInterpreter()
        tips = [_FakeTipSpot("tips_96", "A1")]
        request = interpreter.request_of("drop_tips", (tips,), {})
        assert tips_claimed_by(request, lambda name: None) == {}

    def test_a_run_96_head_pick_claims_every_position_the_record_gives_the_rack(self) -> None:
        """A 96-channel pick names no positions; it draws every spot of the rack, as the record lays it out."""
        interpreter = LiquidHandlerInterpreter()
        request = interpreter.request_of("pick_up_tips96", (_FakeTipRack96("tips_96", ["A1"]),), {})
        rack = MagicMock()
        rack.name = "tips_96"
        rack.tip_spots.return_value = [_FakeTipSpot("tips_96", spot) for spot in ["A1", "A2", "B1"]]
        recorded = TipRackInstance(rack, template_name="tips_96", labware_type="tip_rack")
        assert tips_claimed_by(request, {"tips_96": recorded}.get) == {"tips_96": ["A1", "A2", "B1"]}

    def test_a_pick_given_a_name_where_tip_spots_belong_says_so(self) -> None:
        interpreter = LiquidHandlerInterpreter()
        with pytest.raises(TypeError, match="tip_spots expected a list of tip spots, got str"):
            interpreter.request_of("pick_up_tips", ("A1",), {})
        with pytest.raises(TypeError, match="tip_spots expected a list of tip spots, got list"):
            interpreter.request_of("pick_up_tips", (["A1"],), {})

    async def test_a_96_head_pick_on_a_rack_the_record_does_not_name_logs_that_the_check_did_not_run(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        request = LiquidHandlerInterpreter().request_of("pick_up_tips96", (_FakeTipRack96("tips_96", ["A1"]),), {})
        with caplog.at_level("WARNING", logger="orca"):
            await refuse_a_pick_from_empty_spots(tips_claimed_by(request, lambda name: None), lambda name: None)
        assert "the check did not run for it" in caplog.text

    def test_a_96_head_pick_refuses_a_name_where_a_rack_belongs(self) -> None:
        interpreter = LiquidHandlerInterpreter()
        with pytest.raises(TypeError):
            interpreter.request_of("pick_up_tips96", ("not_a_rack",), {})

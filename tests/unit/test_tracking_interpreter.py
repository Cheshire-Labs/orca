import pytest

from orca.state.records import (
    AspirateDetails,
    DeviceOperation,
    GenericOperationDetails,
)
from orca.resource_models.tracking_interpreter import DefaultInterpreter
from orca.plugins.liquid_handler_interpreter import LiquidHandlerInterpreter


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

    def test_unknown_command_records_nothing(self) -> None:
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
        assert records == []

    def test_claimed_tip_positions_is_always_empty(self) -> None:
        interpreter = DefaultInterpreter()
        assert interpreter.claimed_tip_positions("pick_up_tips", (), {}) == {}


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
            command="mix",
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
        assert record.operation == DeviceOperation.MIX
        assert isinstance(record.details, GenericOperationDetails)

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

    def test_claimed_tip_positions_groups_by_rack(self) -> None:
        interpreter = LiquidHandlerInterpreter()
        tips = [
            _FakeTipSpot("tips_96", "A1"),
            _FakeTipSpot("tips_96", "A2"),
            _FakeTipSpot("tips_other", "B1"),
        ]
        claimed = interpreter.claimed_tip_positions(
            "pick_up_tips", (tips,), {},
        )
        assert claimed == {"tips_96": ["A1", "A2"], "tips_other": ["B1"]}

    def test_claimed_tip_positions_is_empty_for_non_pick_verbs(self) -> None:
        interpreter = LiquidHandlerInterpreter()
        tips = [_FakeTipSpot("tips_96", "A1")]
        assert interpreter.claimed_tip_positions("drop_tips", (tips,), {}) == {}

    def test_claimed_tip_positions_for_96_head_claims_the_whole_rack(self) -> None:
        """A 96-channel pick has no positions arg to read -- it draws every
        spot on the rack at once, so the pre-flight check must ask the rack
        for its own layout or it silently skips 96-head picks entirely."""
        interpreter = LiquidHandlerInterpreter()
        rack = _FakeTipRack96("tips_96", ["A1", "A2", "B1"])

        claimed = interpreter.claimed_tip_positions("pick_up_tips96", (rack,), {})

        assert claimed == {"tips_96": ["A1", "A2", "B1"]}

    def test_claimed_tip_positions_for_96_head_rejects_untyped_labware(self) -> None:
        interpreter = LiquidHandlerInterpreter()
        with pytest.raises(TypeError):
            interpreter.claimed_tip_positions("pick_up_tips96", ("not_a_rack",), {})

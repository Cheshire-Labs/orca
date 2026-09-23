"""A pick that spans two racks debits each rack for its own tips.

The wire has always split this correctly: `_group_tip_spots_by_parent` sends one
`TipPick` per rack, and `test_liquid_handler_device.py` pins that, its docstring
naming the earlier bug where "code collapsed the per-channel labware list to
`wells[0].parent_name` and discarded the rest, producing wrong-answer runs that
completed without error".

The identical collapse survived in the ledger record. One `TipPickUpDetails` was
written naming the FIRST rack, carrying every position from both, so the fold
took rack B's tips out of rack A and left rack B reading full. A rack reading
full when it is not is the failure that started all of this, and a run built on
it completes without error.

An eight-channel head reaching across two racks is ordinary once a rack is part
used, which is exactly what happens on a second run over a deck left standing.
"""

from orca.plugins.liquid_handler_interpreter import LiquidHandlerInterpreter
from orca.state.records import (
    AspirateDetails,
    DeviceOperation,
    TipDropDetails,
    TipPickUpDetails,
)


class _FakeTipSpot:
    def __init__(self, parent_name: str, identifier: str) -> None:
        self.parent_name = parent_name
        self.identifier = identifier


_IDS = {"rack_a": "id-of-rack-a", "rack_b": "id-of-rack-b", "tips_96": "id-of-tips-96"}


def _records(command: str, tip_spots: list[_FakeTipSpot]):
    """Names and ids are DIFFERENT here, deliberately.

    An instance id is a UUID and a name is not, so a fixture that passes the
    name as its own id cannot tell the two fields apart -- which is how a record
    carrying a name in the id field once looked correct.
    """
    names = sorted({s.parent_name for s in tip_spots})
    return LiquidHandlerInterpreter().interpret(
        command=command,
        args=(tip_spots,),
        kwargs={},
        result=None,
        device_name="lh_1",
        affected_labware=names,
        affected_labware_ids=[_IDS[n] for n in names],
        action_id="a1",
        thread_id="t1",
    )


class TestAPickAcrossTwoRacks:
    def test_each_rack_gets_its_own_record(self) -> None:
        records = _records("pick_up_tips", [
            _FakeTipSpot("rack_a", "A1"),
            _FakeTipSpot("rack_a", "B1"),
            _FakeTipSpot("rack_b", "C1"),
        ])
        by_rack = {
            r.details.tip_rack: r.details.positions
            for r in records if isinstance(r.details, TipPickUpDetails)
        }
        assert by_rack == {"rack_a": ["A1", "B1"], "rack_b": ["C1"]}

    def test_no_rack_is_debited_for_another_rack_tips(self) -> None:
        """The bug stated as its consequence: rack_b's tip must not come out of
        rack_a, and rack_b must not be left reading untouched."""
        records = _records("pick_up_tips", [
            _FakeTipSpot("rack_a", "A1"),
            _FakeTipSpot("rack_b", "C1"),
        ])
        for record in records:
            assert isinstance(record.details, TipPickUpDetails)
            for position in record.details.positions:
                owner = "rack_a" if position == "A1" else "rack_b"
                assert record.details.tip_rack == owner

    def test_each_record_names_only_its_own_rack_as_affected(self) -> None:
        """A record debiting rack_a should not claim to have touched rack_b, or
        an audit of one rack reads operations that never moved its tips."""
        records = _records("pick_up_tips", [
            _FakeTipSpot("rack_a", "A1"),
            _FakeTipSpot("rack_b", "C1"),
        ])
        for record in records:
            assert isinstance(record.details, TipPickUpDetails)
            assert record.affected_labware == [record.details.tip_rack]

    def test_a_run_of_channels_returning_to_a_rack_stays_one_record_per_run(
        self,
    ) -> None:
        """Channels alternating racks are separate runs on the wire, and the
        ledger must agree: the order channels engage is what the positions mean."""
        records = _records("pick_up_tips", [
            _FakeTipSpot("rack_a", "A1"),
            _FakeTipSpot("rack_b", "C1"),
            _FakeTipSpot("rack_a", "B1"),
        ])
        assert [
            (r.details.tip_rack, r.details.positions)
            for r in records if isinstance(r.details, TipPickUpDetails)
        ] == [("rack_a", ["A1"]), ("rack_b", ["C1"]), ("rack_a", ["B1"])]


class TestADropAcrossTwoRacks:
    def test_each_rack_gets_its_own_record(self) -> None:
        """`_interpret_drop_tips` carried the same collapse, so tips came back to
        a rack they were never in."""
        records = _records("drop_tips", [
            _FakeTipSpot("rack_a", "A1"),
            _FakeTipSpot("rack_b", "C1"),
        ])
        by_rack = {
            r.details.tip_rack: r.details.positions
            for r in records if isinstance(r.details, TipDropDetails)
        }
        assert by_rack == {"rack_a": ["A1"], "rack_b": ["C1"]}


class TestOneRackIsUnchanged:
    def test_a_single_rack_pick_still_writes_one_record(self) -> None:
        records = _records("pick_up_tips", [
            _FakeTipSpot("tips_96", "A1"),
            _FakeTipSpot("tips_96", "A2"),
        ])
        assert len(records) == 1
        assert records[0].operation == DeviceOperation.PICK_UP_TIPS
        assert isinstance(records[0].details, TipPickUpDetails)
        assert records[0].details.tip_rack == "tips_96"
        assert records[0].details.positions == ["A1", "A2"]


class TestARecordCarriesTheInstanceId:
    """`ops_for_labware` filters on a non-empty id list, so an id field holding
    anything but the instance id drops the record out of the fold entirely -- the
    rack then reads exactly as if it had never been picked from."""

    def test_each_record_carries_its_own_rack_id(self) -> None:
        records = _records("pick_up_tips", [
            _FakeTipSpot("rack_a", "A1"),
            _FakeTipSpot("rack_b", "C1"),
        ])
        assert {
            r.details.tip_rack: tuple(r.affected_labware_ids)
            for r in records if isinstance(r.details, TipPickUpDetails)
        } == {"rack_a": ("id-of-rack-a",), "rack_b": ("id-of-rack-b",)}

    def test_the_id_is_never_the_name(self) -> None:
        for record in _records("pick_up_tips", [_FakeTipSpot("rack_a", "A1")]):
            assert record.affected_labware_ids != record.affected_labware, (
                "the id field is carrying the labware NAME; the fold filters on "
                "ids and would discard this record"
            )

    def test_a_rack_the_caller_did_not_name_carries_no_id(self) -> None:
        """Empty rather than wrong: an empty id list is what makes the fold fall
        back to matching by name, which is the behaviour a driver-reported
        record already relies on."""
        records = LiquidHandlerInterpreter().interpret(
            command="pick_up_tips",
            args=([_FakeTipSpot("unheard_of", "A1")],),
            kwargs={},
            result=None,
            device_name="lh_1",
            affected_labware=["rack_a"],
            affected_labware_ids=["id-of-rack-a"],
            action_id="a1",
            thread_id="t1",
        )
        assert [r.affected_labware_ids for r in records] == [[]]


class _FakeWell:
    def __init__(self, resource_name: str, position: str) -> None:
        self.resource_name = resource_name
        self.position = position


class TestAnAspirateAcrossTwoPlates:
    """The same collapse lived in aspirate and dispense: every volume was
    attributed to the FIRST plate, so one plate was debited for liquid drawn
    from another and the other read untouched."""

    def _aspirate(self, wells, volumes):
        names = sorted({w.resource_name for w in wells})
        return LiquidHandlerInterpreter().interpret(
            command="aspirate",
            args=(wells, volumes),
            kwargs={},
            result=None,
            device_name="lh_1",
            affected_labware=names,
            affected_labware_ids=[f"id-of-{n}" for n in names],
            action_id="a1",
            thread_id="t1",
        )

    def test_each_plate_is_debited_only_its_own_volume(self) -> None:
        records = self._aspirate(
            [_FakeWell("plate_a", "A1"), _FakeWell("plate_b", "A1")], [10.0, 50.0],
        )
        assert {
            r.details.labware: r.details.volumes
            for r in records if isinstance(r.details, AspirateDetails)
        } == {"plate_a": [10.0], "plate_b": [50.0]}

    def test_each_record_carries_its_own_plate_id(self) -> None:
        records = self._aspirate(
            [_FakeWell("plate_a", "A1"), _FakeWell("plate_b", "A1")], [10.0, 50.0],
        )
        assert {
            r.details.labware: tuple(r.affected_labware_ids) for r in records
        } == {"plate_a": ("id-of-plate_a",), "plate_b": ("id-of-plate_b",)}

    def test_one_plate_still_writes_one_record(self) -> None:
        records = self._aspirate(
            [_FakeWell("plate_a", "A1"), _FakeWell("plate_a", "A2")], [10.0, 20.0],
        )
        assert len(records) == 1
        assert records[0].details.volumes == [10.0, 20.0]


class TestTheHeadKeepsBothRacksTips:
    """The rack half of this collapse was fixed; the head half was not.

    Each rack got its own record and every one of them counted its channels
    from zero, so the fold, which keys on channel, took the second rack's tips
    and wrote them over the first rack's. Four tips on the machine, two in the
    record, and nothing said a word.
    """

    @staticmethod
    def _channels(command: str, tip_spots: list[_FakeTipSpot]) -> list[list[int]]:
        return [
            record.details.use_channels or []
            for record in _records(command, tip_spots)
            if isinstance(record.details, (TipPickUpDetails, TipDropDetails))
        ]

    def test_the_second_rack_takes_the_channels_after_the_first(self) -> None:
        channels = self._channels("pick_up_tips", [
            _FakeTipSpot("rack_a", "A1"), _FakeTipSpot("rack_a", "B1"),
            _FakeTipSpot("rack_b", "A1"), _FakeTipSpot("rack_b", "B1"),
        ])
        assert channels == [[0, 1], [2, 3]]

    def test_a_return_across_two_racks_empties_all_four_channels(self) -> None:
        channels = self._channels("drop_tips", [
            _FakeTipSpot("rack_a", "A1"), _FakeTipSpot("rack_a", "B1"),
            _FakeTipSpot("rack_b", "A1"), _FakeTipSpot("rack_b", "B1"),
        ])
        assert channels == [[0, 1], [2, 3]]

    def test_a_workflow_pick_says_it_counted_the_channels(self) -> None:
        """A method's `pick_up_tips` takes no channel numbers, so the record
        has none to keep and the read must not claim otherwise."""
        picks = [
            record.details
            for record in _records("pick_up_tips", [_FakeTipSpot("tips_96", "A1")])
            if isinstance(record.details, TipPickUpDetails)
        ]
        assert [d.channels_were_counted for d in picks] == [True]

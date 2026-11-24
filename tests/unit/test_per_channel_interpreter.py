"""Unit tests for the LiquidHandlerInterpreter per-channel emission path.

Verifies that ``interpret_per_channel_outcomes`` produces the right number
and shape of records for each combination of (full success, partial failure,
all-channel failure). Asserts the per-well certainty / error_code attribution
matches the cheshire-drivers ``per_channel_errors`` payload.
"""

import logging

import pytest

from cheshire_drivers.liquid_handler_models import (
    ChannelError,
    DropTipsRequest,
    LabwareStateResponse,
    LabwareWellState,
    TipPick,
)

from orca.gateway.controller.command_pipeline import PartialChannelFailureError
from orca.plugins.liquid_handler_interpreter import LiquidHandlerInterpreter
from orca.state.command_operations import operations_for_channels, outside_a_run
from orca.state.mounted import fold_mounted
from orca.workflow_models.actions.device_call_dispatcher import CommandRecorder
from orca.workflow_models.device_handle import ActionRequest
from orca.state.records import (
    AspirateDetails,
    DeviceOperation,
    DispenseDetails,
    OperationRecord,
    TipDiscardDetails,
    TipDropDetails,
    TipPickUpDetails,
    TrackingSource,
)


class _FakeWell:
    def __init__(self, parent_name: str, identifier: str) -> None:
        self.parent_name = parent_name
        self.identifier = identifier
        self.resource_name = parent_name
        self.position = identifier


class TestInterpretPerChannelAspirate:

    def test_full_success_emits_no_per_channel_records(self) -> None:
        interpreter = LiquidHandlerInterpreter()
        wells = [_FakeWell("plate_1", "A1"), _FakeWell("plate_1", "A2")]
        result = LabwareStateResponse(success=True)
        records = interpreter.interpret_per_channel_outcomes(
            command="aspirate",
            args=(wells, [100.0, 100.0]),
            kwargs={},
            result=result,
            device_name="lh",
            affected_labware=["plate_1"],
            affected_labware_ids=["id-of-plate-1"],
            action_id="a1",
            thread_id="t1",
        )
        assert records == []

    def test_keyword_volumes_still_attribute_per_channel(self) -> None:
        """The action body may pass volumes by keyword; attribution must not
        depend on the caller's arg/kwarg split."""
        interpreter = LiquidHandlerInterpreter()
        wells = [_FakeWell("plate_1", "A1"), _FakeWell("plate_1", "A2")]
        result = LabwareStateResponse(
            success=False,
            per_channel_errors=[
                ChannelError(
                    channel_id=0,
                    well_position="A1",
                    labware="plate_1",
                    attempted_volume=100.0,
                    error_code="HamiltonE100",
                    error_message="channel jammed",
                ),
            ],
        )
        records = interpreter.interpret_per_channel_outcomes(
            command="aspirate",
            args=(wells,),
            kwargs={"volumes": [100.0, 100.0]},
            result=result,
            device_name="lh",
            affected_labware=["plate_1"],
            affected_labware_ids=["id-of-plate-1"],
            action_id="a1",
            thread_id="t1",
        )
        assert len(records) == 2
        assert isinstance(records[0].details, AspirateDetails)
        assert records[0].details.certainty == "definitely_not_transferred"

    def test_partial_failure_emits_one_record_per_well(self) -> None:
        interpreter = LiquidHandlerInterpreter()
        wells = [
            _FakeWell("plate_1", "A1"),
            _FakeWell("plate_1", "A2"),
            _FakeWell("plate_1", "A3"),
        ]
        result = LabwareStateResponse(
            success=False,
            per_channel_errors=[
                ChannelError(
                    channel_id=1,
                    well_position="A2",
                    labware="plate_1",
                    attempted_volume=100.0,
                    error_code="HamiltonE100",
                    error_message="channel jammed",
                ),
            ],
        )
        records = interpreter.interpret_per_channel_outcomes(
            command="aspirate",
            args=(wells, [100.0, 100.0, 100.0]),
            kwargs={},
            result=result,
            device_name="lh",
            affected_labware=["plate_1"],
            affected_labware_ids=["id-of-plate-1"],
            action_id="a1",
            thread_id="t1",
        )
        assert len(records) == 3

        for record in records:
            assert record.operation == DeviceOperation.ASPIRATE
            assert record.device_name == "lh"
            assert record.action_id == "a1"
            assert isinstance(record.details, AspirateDetails)

        a1_record = records[0]
        assert isinstance(a1_record.details, AspirateDetails)
        assert a1_record.details.positions == ["A1"]
        assert a1_record.details.volumes == [100.0]
        assert a1_record.details.certainty == "confirmed_transferred"
        assert a1_record.details.error_code is None

        a2_record = records[1]
        assert isinstance(a2_record.details, AspirateDetails)
        assert a2_record.details.positions == ["A2"]
        assert a2_record.details.volumes == [0.0]
        assert a2_record.details.certainty == "definitely_not_transferred"
        assert a2_record.details.error_code == "HamiltonE100"

        a3_record = records[2]
        assert isinstance(a3_record.details, AspirateDetails)
        assert a3_record.details.positions == ["A3"]
        assert a3_record.details.volumes == [100.0]
        assert a3_record.details.certainty == "confirmed_transferred"

    def test_all_channels_failed(self) -> None:
        """Every channel errored -> every record carries definitely_not_transferred."""
        interpreter = LiquidHandlerInterpreter()
        wells = [_FakeWell("plate_1", "A1"), _FakeWell("plate_1", "A2")]
        result = LabwareStateResponse(
            success=False,
            per_channel_errors=[
                ChannelError(channel_id=0, well_position="A1", labware="plate_1",
                             attempted_volume=50.0, error_code="E1", error_message="m1"),
                ChannelError(channel_id=1, well_position="A2", labware="plate_1",
                             attempted_volume=50.0, error_code="E2", error_message="m2"),
            ],
        )
        records = interpreter.interpret_per_channel_outcomes(
            command="aspirate",
            args=(wells, [50.0, 50.0]),
            kwargs={},
            result=result,
            device_name="lh",
            affected_labware=["plate_1"],
            affected_labware_ids=["id-of-plate-1"],
            action_id="a1",
            thread_id="t1",
        )
        assert len(records) == 2
        for r in records:
            assert isinstance(r.details, AspirateDetails)
            assert r.details.certainty == "definitely_not_transferred"
            assert r.details.volumes == [0.0]

    def test_dispense_partial_failure(self) -> None:
        interpreter = LiquidHandlerInterpreter()
        wells = [_FakeWell("plate_2", "B1"), _FakeWell("plate_2", "B2")]
        result = LabwareStateResponse(
            success=False,
            per_channel_errors=[
                ChannelError(channel_id=0, well_position="B1", labware="plate_2",
                             attempted_volume=75.0, error_code="DispE", error_message="dispense failed"),
            ],
        )
        records = interpreter.interpret_per_channel_outcomes(
            command="dispense",
            args=(wells, [75.0, 75.0]),
            kwargs={},
            result=result,
            device_name="lh",
            affected_labware=["plate_2"],
            affected_labware_ids=["plate_2"],
            action_id="d1",
            thread_id="t1",
        )
        assert len(records) == 2
        for r in records:
            assert r.operation == DeviceOperation.DISPENSE
            assert isinstance(r.details, DispenseDetails)
        assert isinstance(records[0].details, DispenseDetails)
        assert records[0].details.certainty == "definitely_not_transferred"
        assert records[0].details.error_code == "DispE"
        assert isinstance(records[1].details, DispenseDetails)
        assert records[1].details.certainty == "confirmed_transferred"

    def test_unknown_command_returns_empty(self) -> None:
        """A command with no per-channel reader saves no per-channel records."""
        interpreter = LiquidHandlerInterpreter()
        result = LabwareStateResponse(
            success=False,
            per_channel_errors=[
                ChannelError(channel_id=0, well_position="A1", labware="plate_1",
                             attempted_volume=10.0, error_code="E", error_message="m"),
            ],
        )
        records = interpreter.interpret_per_channel_outcomes(
            command="return_tips96",
            args=(),
            kwargs={},
            result=result,
            device_name="lh",
            affected_labware=["plate_1"],
            affected_labware_ids=["id-of-plate-1"],
            action_id="a1",
            thread_id="t1",
        )
        assert records == []

    def test_non_labware_state_response_returns_empty(self) -> None:
        interpreter = LiquidHandlerInterpreter()
        records = interpreter.interpret_per_channel_outcomes(
            command="aspirate",
            args=([_FakeWell("p", "A1")], [10.0]),
            kwargs={},
            result=None,
            device_name="lh",
            affected_labware=[],
            affected_labware_ids=[],
            action_id="a1",
            thread_id="t1",
        )
        assert records == []

    def test_orphan_channel_error_logs_warning(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A ChannelError targeting a (labware, position) not in the request
        logs a warning and produces records flagged as confirmed_transferred
        for every well in the request -- the orphan error itself is dropped
        from the records but surfaces in the log so the operator notices the
        attribution mismatch."""
        interpreter = LiquidHandlerInterpreter()
        wells = [_FakeWell("plate_1", "A1")]
        result = LabwareStateResponse(
            success=False,
            per_channel_errors=[
                ChannelError(
                    channel_id=2,
                    well_position="Z9",
                    labware="some_other_plate",
                    attempted_volume=0.0,
                    error_code="OrphanE",
                    error_message="not a real well in the request",
                ),
            ],
        )
        with caplog.at_level(logging.WARNING, logger="orca"):
            records = interpreter.interpret_per_channel_outcomes(
                command="aspirate",
                args=(wells, [100.0]),
                kwargs={},
                result=result,
                device_name="lh",
                affected_labware=["plate_1"],
                affected_labware_ids=["id-of-plate-1"],
                action_id="a1",
                thread_id="t1",
            )
        assert any("which the request did not name" in r.getMessage() for r in caplog.records)
        assert len(records) == 1

    def test_a_command_not_read_per_channel_logs_a_warning(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A command with no per-channel reader that reports channel errors says so in the log and saves none of them."""
        interpreter = LiquidHandlerInterpreter()
        result = LabwareStateResponse(
            success=False,
            per_channel_errors=[
                ChannelError(
                    channel_id=0, well_position="A1", labware="rack_1",
                    attempted_volume=0.0, error_code="E", error_message="m",
                ),
            ],
        )
        with caplog.at_level(logging.WARNING, logger="orca"):
            records = interpreter.interpret_per_channel_outcomes(
                command="return_tips96",
                args=(),
                kwargs={},
                result=result,
                device_name="lh",
                affected_labware=[],
                affected_labware_ids=[],
                action_id="a1",
                thread_id="t1",
            )
        assert records == []
        assert any("is not read per channel" in r.getMessage() for r in caplog.records)


class _FakeTipSpot:
    def __init__(self, parent_name: str, identifier: str) -> None:
        self.parent_name = parent_name
        self.identifier = identifier
        self.has_tip = True

    def set_tip(self, has_tip: bool) -> None:
        self.has_tip = has_tip


def _tip_error(rack: str, position: str | None, channel: int) -> ChannelError:
    return ChannelError(
        channel_id=channel, well_position=position, labware=rack,
        attempted_volume=0.0, error_code="TipNotMoved", error_message="the channel did not go",
    )


def _per_channel(
    command: str, args: tuple[list[_FakeTipSpot] | list[int], ...], errors: list[ChannelError],
) -> list[OperationRecord]:
    return LiquidHandlerInterpreter().interpret_per_channel_outcomes(
        command=command, args=args, kwargs={},
        result=LabwareStateResponse(success=False, per_channel_errors=errors),
        device_name="lh", affected_labware=["rack_1"], affected_labware_ids=["id-of-rack-1"],
        action_id="a1", thread_id="t1",
    )


class TestATipCommandWithAFailedChannel:
    """A run's tip command that a channel failed is one record per channel, and only a channel that went moves a tip."""

    def test_a_pick_up_takes_only_the_tips_that_went(self) -> None:
        spots = [_FakeTipSpot("rack_1", position) for position in ("A1", "B1", "C1")]

        records = _per_channel("pick_up_tips", (spots,), [_tip_error("rack_1", "B1", 1)])

        details = [record.details for record in records]
        assert all(isinstance(d, TipPickUpDetails) for d in details)
        assert [(d.positions, d.use_channels, d.failed_positions, d.failed_channels, d.error_code) for d in details if isinstance(d, TipPickUpDetails)] == [
            (["A1"], [0], [], [], None),
            ([], [], ["B1"], [1], "TipNotMoved"),
            (["C1"], [2], [], [], None),
        ]
        assert sorted(fold_mounted(records, "lh").by_channel) == [0, 2]

    async def test_a_runs_pick_up_fails_its_step_after_saving_each_channel(self) -> None:
        saved: list[OperationRecord] = []

        async def save(records: list[OperationRecord]) -> None:
            saved.extend(records)

        spots = [_FakeTipSpot("rack_1", position) for position in ("A1", "B1")]
        recorder = CommandRecorder(LiquidHandlerInterpreter(), save, "lh", "a1", "t1")
        request = ActionRequest(device_name="lh", command="pick_up_tips", args=(spots,), kwargs={})

        with pytest.raises(PartialChannelFailureError):
            await recorder.save_completed(
                request, LabwareStateResponse(success=False, per_channel_errors=[_tip_error("rack_1", "B1", 1)]),
                ["rack_1"], ["id-of-rack-1"],
            )

        assert sorted(fold_mounted(saved, "lh").by_channel) == [0]

    def test_a_drop_returns_only_the_tips_that_went(self) -> None:
        spots = [_FakeTipSpot("rack_1", position) for position in ("A1", "B1")]
        picked = LiquidHandlerInterpreter().interpret(
            command="pick_up_tips", args=(spots,), kwargs={}, result=None, device_name="lh",
            affected_labware=["rack_1"], affected_labware_ids=["id-of-rack-1"], action_id="a0", thread_id="t1",
        )

        records = _per_channel("drop_tips", (spots,), [_tip_error("rack_1", "B1", 1)])

        details = [record.details for record in records]
        assert [(d.positions, d.use_channels, d.failed_positions, d.failed_channels, d.error_code) for d in details if isinstance(d, TipDropDetails)] == [
            (["A1"], [0], [], [], None),
            ([], [], ["B1"], [1], "TipNotMoved"),
        ]
        assert sorted(fold_mounted([*picked, *records], "lh").by_channel) == [1]

    def test_a_drop_to_waste_that_names_its_spots_is_read_by_channel(self) -> None:
        """The driver reports a failed drop to waste against the waste and the channel, with no rack spot."""
        request = DropTipsRequest(
            drops=[TipPick(tip_rack="rack_1", positions=["A1", "B1"])], to_waste=True, use_channels=[0, 1],
        )
        context = outside_a_run("lh", "drop_tips", TrackingSource.OPERATOR)

        records = operations_for_channels("drop_tips", request, [_tip_error("trash", None, 1)], context)

        details = [record.details for record in records]
        assert [(d.positions, d.failed_positions, d.failed_channels) for d in details if isinstance(d, TipDropDetails)] == [
            (["A1"], [], []),
            ([], ["B1"], [1]),
        ]

    def test_a_discard_of_named_channels_bins_only_the_tips_that_went(self) -> None:
        spots = [_FakeTipSpot("rack_1", position) for position in ("A1", "B1")]
        picked = LiquidHandlerInterpreter().interpret(
            command="pick_up_tips", args=(spots,), kwargs={}, result=None, device_name="lh",
            affected_labware=["rack_1"], affected_labware_ids=["id-of-rack-1"], action_id="a0", thread_id="t1",
        )

        records = _per_channel("discard_tips", ([0, 1],), [_tip_error("trash", None, 1)])

        details = [record.details for record in records]
        assert [(d.use_channels, d.failed_channels, d.error_code) for d in details if isinstance(d, TipDiscardDetails)] == [
            ([0], [], None),
            ([], [1], "TipNotMoved"),
        ]
        assert sorted(fold_mounted([*picked, *records], "lh").by_channel) == [1]


class TestFilterStatePreservesPerChannelErrors:
    """LiquidHandler.filter_driver_state preserves per_channel_errors regardless
    of trust_driver_state.

    Outcome attribution must reach the interpreter even when the operator opts
    out of driver-state observation, otherwise the local-handler path silently
    drops every per-channel record while the remote path keeps them. The
    asymmetry was caught in plan review; this test guards it from regression.
    """

    def test_filter_driver_state_with_trust_disabled_preserves_per_channel_errors(self) -> None:
        from orca.devices.devices import LiquidHandlerProtocol

        handler = LiquidHandlerProtocol("lh1", trust_driver_state=False)

        response = LabwareStateResponse(
            success=False,
            labware_state={"plate_1": LabwareWellState(volumes={"A1": 100.0})},
            per_channel_errors=[
                ChannelError(
                    channel_id=2,
                    well_position="A3",
                    labware="plate_1",
                    attempted_volume=100.0,
                    error_code="E1",
                    error_message="m1",
                ),
            ],
        )
        filtered = handler.filter_driver_state(response)

        assert filtered.success is False
        assert filtered.labware_state == {}
        assert len(filtered.per_channel_errors) == 1
        assert filtered.per_channel_errors[0].channel_id == 2

    def test_filter_driver_state_with_trust_enabled_preserves_everything(self) -> None:
        from orca.devices.devices import LiquidHandlerProtocol

        # The forwarding driver advertises provides_state=True, so
        # trust_driver_state survives the AND with the driver capability.
        handler = LiquidHandlerProtocol("lh1", trust_driver_state=True)

        response = LabwareStateResponse(
            success=False,
            labware_state={"plate_1": LabwareWellState(volumes={"A1": 100.0})},
            per_channel_errors=[
                ChannelError(
                    channel_id=0, well_position="A1", labware="plate_1",
                    attempted_volume=50.0, error_code="E", error_message="m",
                ),
            ],
        )
        filtered = handler.filter_driver_state(response)
        assert filtered is response
        assert filtered.labware_state != {}
        assert len(filtered.per_channel_errors) == 1


class TestAChannelRecordNamesOnlyItsOwnLabware:
    """Reading one plate's history must not list another plate's channels.

    The volumes were never wrong -- the fold keys on the record's own labware --
    but every per-channel record carried the whole action's labware list, so an
    operator asking what happened to plate 1 was shown plate 2's failed
    channels alongside it.
    """

    def test_each_record_carries_one_labware_and_its_own_id(self) -> None:
        interpreter = LiquidHandlerInterpreter()
        wells = [_FakeWell("plate_1", "A1"), _FakeWell("plate_2", "B1")]
        result = LabwareStateResponse(
            success=False,
            per_channel_errors=[
                ChannelError(
                    channel_id=1, well_position="B1", labware="plate_2",
                    attempted_volume=50.0, error_code="no_liquid",
                    error_message="nothing there",
                ),
            ],
        )
        records = interpreter.interpret_per_channel_outcomes(
            command="aspirate",
            args=(wells, [50.0, 50.0]),
            kwargs={},
            result=result,
            device_name="lh",
            affected_labware=["plate_1", "plate_2"],
            affected_labware_ids=["id-of-plate-1", "id-of-plate-2"],
            action_id="a1",
            thread_id="t1",
        )
        by_labware = {
            rec.details.labware: rec for rec in records
            if isinstance(rec.details, AspirateDetails)
        }
        assert set(by_labware) == {"plate_1", "plate_2"}
        assert by_labware["plate_1"].affected_labware == ["plate_1"]
        assert by_labware["plate_1"].affected_labware_ids == ["id-of-plate-1"]
        assert by_labware["plate_2"].affected_labware == ["plate_2"]
        assert by_labware["plate_2"].affected_labware_ids == ["id-of-plate-2"]

    def test_a_labware_the_caller_did_not_name_carries_no_id(self) -> None:
        """An empty id list folds by name. A wrong id would fold as nothing."""
        interpreter = LiquidHandlerInterpreter()
        wells = [_FakeWell("plate_1", "A1"), _FakeWell("unnamed_plate", "B1")]
        result = LabwareStateResponse(
            success=False,
            per_channel_errors=[
                ChannelError(
                    channel_id=0, well_position="A1", labware="plate_1",
                    attempted_volume=50.0, error_code="no_liquid",
                    error_message="nothing there",
                ),
            ],
        )
        records = interpreter.interpret_per_channel_outcomes(
            command="aspirate",
            args=(wells, [50.0, 50.0]),
            kwargs={},
            result=result,
            device_name="lh",
            affected_labware=["plate_1"],
            affected_labware_ids=["id-of-plate-1"],
            action_id="a1",
            thread_id="t1",
        )
        stray = next(
            rec for rec in records
            if isinstance(rec.details, AspirateDetails)
            and rec.details.labware == "unnamed_plate"
        )
        assert stray.affected_labware_ids == []

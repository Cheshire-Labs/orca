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
    LabwareStateResponse,
    LabwareWellState,
)

from orca.plugins.liquid_handler_interpreter import LiquidHandlerInterpreter
from orca.state.records import (
    AspirateDetails,
    DeviceOperation,
    DispenseDetails,
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
        """Per-channel outcomes are only meaningful for aspirate/dispense today."""
        interpreter = LiquidHandlerInterpreter()
        wells = [_FakeWell("plate_1", "A1")]
        result = LabwareStateResponse(
            success=False,
            per_channel_errors=[
                ChannelError(channel_id=0, well_position="A1", labware="plate_1",
                             attempted_volume=10.0, error_code="E", error_message="m"),
            ],
        )
        records = interpreter.interpret_per_channel_outcomes(
            command="pick_up_tips",
            args=(wells,),
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
        with caplog.at_level(logging.WARNING, logger="orca.plugins.liquid_handler_interpreter"):
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
        assert any("orphan ChannelError" in r.getMessage() for r in caplog.records)
        assert len(records) == 1

    def test_unrecognized_command_with_per_channel_errors_logs_warning(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An unrecognized command (e.g., pick_up_tips) with per_channel_errors
        logs loud rather than silently returning [] and dropping the
        attribution. Pin this so a future deferred slice can't quietly mask
        a wrapper that started populating per_channel_errors on tip ops."""
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
        with caplog.at_level(logging.WARNING, logger="orca.plugins.liquid_handler_interpreter"):
            records = interpreter.interpret_per_channel_outcomes(
                command="pick_up_tips",
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
        assert any("no per-channel branch" in r.getMessage() for r in caplog.records)


class TestFilterStatePreservesPerChannelErrors:
    """LiquidHandler._filter_state preserves per_channel_errors regardless
    of trust_driver_state.

    Outcome attribution must reach the interpreter even when the operator opts
    out of driver-state observation, otherwise the local-handler path silently
    drops every per-channel record while the remote path keeps them. The
    asymmetry was caught in plan review; this test guards it from regression.
    """

    def test_filter_state_with_trust_disabled_preserves_per_channel_errors(self) -> None:
        from cheshire_drivers.sims import SimLiquidHandlerDriver
        from orca.devices.devices import LiquidHandlerProtocol
        from orca.runtime.device_factory_context import use_device_factory
        from orca.runtime.device_factory_protocol import DriverPairElement

        class _Factory:
            def __init__(self, driver: SimLiquidHandlerDriver) -> None:
                self._d = driver

            def build_drivers(
                self, device_type: str, name: str,
            ) -> tuple[DriverPairElement, DriverPairElement]:
                return self._d, self._d

        sim = SimLiquidHandlerDriver("lh1")
        with use_device_factory(_Factory(sim)):
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
        filtered = handler._filter_state(response)

        assert filtered.success is False
        assert filtered.labware_state == {}
        assert len(filtered.per_channel_errors) == 1
        assert filtered.per_channel_errors[0].channel_id == 2

    def test_filter_state_with_trust_enabled_preserves_everything(self) -> None:
        from cheshire_drivers.sims import RecordingLiquidHandlerDriver, SimLiquidHandlerDriver
        from orca.devices.devices import LiquidHandlerProtocol
        from orca.runtime.device_factory_context import use_device_factory
        from orca.runtime.device_factory_protocol import DriverPairElement

        class _Factory:
            def __init__(self, driver: RecordingLiquidHandlerDriver) -> None:
                self._d = driver

            def build_drivers(
                self, device_type: str, name: str,
            ) -> tuple[DriverPairElement, DriverPairElement]:
                return self._d, self._d

        # RecordingLiquidHandlerDriver advertises provides_state=True so
        # trust_driver_state survives the AND with the driver capability.
        sim = RecordingLiquidHandlerDriver(SimLiquidHandlerDriver("lh1"))
        with use_device_factory(_Factory(sim)):
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
        filtered = handler._filter_state(response)
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

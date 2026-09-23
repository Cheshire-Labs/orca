"""Pydantic round-trip + discriminator coverage for tracking models.

Pins:
- Every *Details variant survives ``model_dump_json`` / ``model_validate_json``
  with full equality (no field drops, no kind-discriminator regression).
- The ``OperationDetails`` discriminated union dispatches to the right
  concrete subtype on parse.
- ``RunProtocolDetails.params`` round-trips a ``float`` value as ``float``
  (guards against Pydantic 2 smart-mode union resolution collapsing
  ``1.0`` to ``int``).
- Frozen-config rejects mutation via ``ValidationError``.
"""

import pytest
from pydantic import TypeAdapter, ValidationError

from orca.state.records import (
    AspirateDetails,
    Aspirate96Details,
    CentrifugeDetails,
    DeclaredTracking,
    DispenseDetails,
    Dispense96Details,
    GenericOperationDetails,
    IncubateDetails,
    InitialStateDetails,
    MixDetails,
    OperationDetails,
    OperationRecord,
    DeviceOperation,
    RunProtocolDetails,
    SealDetails,
    ShakeDetails,
    TipDropDetails,
    TipDrop96Details,
    TipPickUpDetails,
    TipPickUp96Details,
    TrackingRecord,
    TrackingSource,
    WellUsageDetails,
)


_DETAILS_FIXTURES: list[tuple[str, OperationDetails]] = [
    ("aspirate", AspirateDetails(
        labware="plate-1", positions=["A1", "B1"], volumes=[100.0, 50.0],
    )),
    ("dispense", DispenseDetails(
        labware="plate-1", positions=["A1"], volumes=[75.0],
        flow_rates=[1.5],
    )),
    ("pick_up_tips", TipPickUpDetails(
        tip_rack="tips-50ul", positions=["A1", "A2"],
    )),
    ("drop_tips", TipDropDetails(
        tip_rack="tips-50ul", positions=["A1"], to_waste=True,
    )),
    ("aspirate96", Aspirate96Details(
        labware="plate-1", volume=120.0, flow_rate=2.0,
    )),
    ("dispense96", Dispense96Details(
        labware="plate-1", volume=120.0, liquid_height=3.5,
    )),
    ("pick_up_tips96", TipPickUp96Details(tip_rack="tips-300ul")),
    ("drop_tips96", TipDrop96Details(tip_rack=None, to_waste=True)),
    ("shake", ShakeDetails(speed_rpm=500.0, duration_s=30.0)),
    ("seal", SealDetails(temperature_c=180.0, duration_s=2.5)),
    ("incubate", IncubateDetails(temperature_c=37.0, duration_s=3600.0)),
    ("centrifuge", CentrifugeDetails(g_force=1000.0, duration_s=300.0)),
    ("run_protocol", RunProtocolDetails(
        protocol_filepath="/tmp/p.py",
        params={"a": 1, "b": 2.5, "c": "x", "d": True},
    )),
    ("generic", GenericOperationDetails(
        command="custom_cmd", args_repr="(1, 2)",
    )),
    ("mix", MixDetails(
        labware="plate-1", positions=["A1"], volume_ul=80.0, cycles=3,
    )),
    ("well_usage", WellUsageDetails(
        labware="plate-1", positions=["A1", "A2", "A3"],
    )),
    ("initial_state", InitialStateDetails(
        labware="plate-1",
        well_volumes={"A1": 200.0, "B1": 200.0},
    )),
]


_DETAILS_ADAPTER: TypeAdapter[OperationDetails] = TypeAdapter(OperationDetails)


class TestDetailsRoundTrip:
    @pytest.mark.parametrize(
        "kind,sample",
        _DETAILS_FIXTURES,
        ids=[k for k, _ in _DETAILS_FIXTURES],
    )
    def test_dump_validate_round_trip(
        self, kind: str, sample: OperationDetails,
    ) -> None:
        """Every variant survives JSON dump+validate without field drift.

        Catches accidental schema drift (renamed fields, dropped Optionals)
        across the full discriminated union.
        """
        as_json = sample.model_dump_json()
        rebuilt = _DETAILS_ADAPTER.validate_json(as_json)
        assert rebuilt == sample
        assert rebuilt.kind == kind


class TestKindEnumParity:
    """The ``kind`` discriminator string on each ``*Details`` variant must
    equal the corresponding ``DeviceOperation`` enum value. Drift between
    these two surfaces is a wire bug: callers that switch on the enum and
    callers that switch on the discriminator would land on different keys
    for the same operation.
    """

    def test_tip_variants_kind_matches_device_operation(self) -> None:
        pickup = TipPickUpDetails(tip_rack="r", positions=["A1"])
        drop = TipDropDetails(tip_rack="r", positions=["A1"])
        pickup96 = TipPickUp96Details(tip_rack="r")
        drop96 = TipDrop96Details(tip_rack="r")
        assert pickup.kind == DeviceOperation.PICK_UP_TIPS.value
        assert drop.kind == DeviceOperation.DROP_TIPS.value
        assert pickup96.kind == DeviceOperation.PICK_UP_TIPS96.value
        assert drop96.kind == DeviceOperation.DROP_TIPS96.value


class TestDiscriminatorDispatch:
    def test_kind_dispatches_to_concrete_subtype(self) -> None:
        """The discriminator picks the right concrete class on parse.

        Without a discriminator, Pydantic would either pick the first
        union member that validates or return a bare dict.
        """
        payload = '{"kind": "aspirate", "labware": "p1", "positions": ["A1"], "volumes": [100.0]}'
        parsed = _DETAILS_ADAPTER.validate_json(payload)
        assert isinstance(parsed, AspirateDetails)

    def test_unknown_kind_raises(self) -> None:
        """Unknown kind is loud (no silent fallback to a wrong subtype)."""
        payload = '{"kind": "not_a_real_kind", "labware": "p1"}'
        with pytest.raises(ValidationError):
            _DETAILS_ADAPTER.validate_json(payload)

    def test_missing_kind_raises(self) -> None:
        """A details payload that forgot the discriminator is rejected."""
        payload = '{"labware": "p1", "positions": ["A1"], "volumes": [100.0]}'
        with pytest.raises(ValidationError):
            _DETAILS_ADAPTER.validate_json(payload)


class TestRunProtocolParamsTypePreservation:
    def test_float_param_round_trips_as_float(self) -> None:
        """Pydantic 2 smart-mode union can collapse 1.0 to int. Pin
        the contract: float-valued protocol params survive as float.

        Triggered by the union value type ``str | int | float | bool``
        on ``RunProtocolDetails.params``. A protocol runner that branches
        on ``isinstance(v, float)`` would silently take the wrong branch
        if the wire collapses ``1.0`` to ``1``.
        """
        rp = RunProtocolDetails(
            protocol_filepath="/tmp/p.py",
            params={"volume_ul": 1.0, "cycles": 3, "name": "wash"},
        )
        rebuilt = RunProtocolDetails.model_validate_json(rp.model_dump_json())
        assert rebuilt.params["volume_ul"] == 1.0
        assert isinstance(rebuilt.params["volume_ul"], float)
        assert isinstance(rebuilt.params["cycles"], int)
        assert isinstance(rebuilt.params["name"], str)


class TestOperationRecordRoundTrip:
    def test_full_record_round_trip(self) -> None:
        """``OperationRecord`` round-trips with the discriminated details
        intact and every metadata field preserved.
        """
        rec = OperationRecord(
            operation=DeviceOperation.ASPIRATE,
            device_name="lh-1",
            affected_labware=["plate-1"],
            action_id="a-1",
            thread_id="t-1",
            details=AspirateDetails(
                labware="plate-1", positions=["A1"], volumes=[100.0],
            ),
            timestamp=12345.6,
            group_id="g-1",
            source=TrackingSource.DRIVER_OBSERVED,
        )
        rebuilt = OperationRecord.model_validate_json(rec.model_dump_json())
        assert rebuilt == rec
        assert isinstance(rebuilt.details, AspirateDetails)


class TestTrackingRecordRoundTrip:
    def test_record_with_mixed_operations_round_trips(self) -> None:
        """``TrackingRecord`` with multiple ops of different kinds keeps
        each op's details typed correctly through the discriminator.
        """
        record = TrackingRecord(
            execution_id="exec-mixed",
            action_id="a-1",
            thread_id="t-1",
            method_id="m-1",
            source=TrackingSource.OBSERVED,
            timestamp=10.0,
            operations=[
                OperationRecord(
                    operation=DeviceOperation.ASPIRATE,
                    device_name="lh", affected_labware=["p"],
                    action_id="a-1", thread_id="t-1",
                    details=AspirateDetails(
                        labware="p", positions=["A1"], volumes=[50.0],
                    ),
                    timestamp=11.0,
                ),
                OperationRecord(
                    operation=DeviceOperation.SHAKE,
                    device_name="shaker", affected_labware=["p"],
                    action_id="a-1", thread_id="t-1",
                    details=ShakeDetails(speed_rpm=400.0, duration_s=10.0),
                    timestamp=12.0,
                ),
            ],
        )
        rebuilt = TrackingRecord.model_validate_json(record.model_dump_json())
        assert rebuilt == record
        assert isinstance(rebuilt.operations[0].details, AspirateDetails)
        assert isinstance(rebuilt.operations[1].details, ShakeDetails)


class TestFrozenSemantics:
    def test_mutation_raises_validation_error(self) -> None:
        """Frozen Pydantic models reject attribute assignment with
        ``ValidationError`` (the v2 idiomatic shape).
        """
        details = AspirateDetails(
            labware="p", positions=["A1"], volumes=[100.0],
        )
        with pytest.raises(ValidationError):
            details.labware = "other"  # type: ignore[misc]


class TestExecutionIdRequired:
    """Pin the contract that every TrackingRecord self-describes its
    owning execution. ``execution_id`` started out optional; it was
    never read on the model and the wire DTO/bucket
    key carried the truth instead. Making it required at the schema
    level forces every construction site to bind it, so cross-execution
    search becomes a primary-key lookup on an indexed column rather
    than a scan across in-memory dict buckets.
    """

    def test_construction_requires_execution_id(self) -> None:
        """A TrackingRecord built without ``execution_id`` fails validation
        loudly. No silent fallback, no Optional, no default sentinel.
        """
        with pytest.raises(ValidationError):
            TrackingRecord(  # type: ignore[call-arg]
                action_id="a-1",
                thread_id="t-1",
                method_id="m-1",
                source=TrackingSource.OBSERVED,
                timestamp=10.0,
            )

    def test_execution_id_round_trips(self) -> None:
        """The required field survives JSON round-trip with the value
        the constructor was given.
        """
        record = TrackingRecord(
            execution_id="exec-abc",
            action_id="a-1",
            thread_id="t-1",
            method_id="m-1",
            source=TrackingSource.OBSERVED,
            timestamp=10.0,
        )
        rebuilt = TrackingRecord.model_validate_json(record.model_dump_json())
        assert rebuilt.execution_id == "exec-abc"


class TestDeclaredTrackingRoundTrip:
    """``DeclaredTracking.operations`` carries
    ``list[tuple[DeviceOperation, OperationDetails]]`` -- a tuple-as-array
    serialization where the second element is a discriminated union. Pin
    the round-trip so a future change to the tuple shape (e.g., flattening
    to ``list[OperationDetails]``) is loud rather than silently breaking
    declared-tracking authors.
    """

    def test_operations_with_mixed_kinds_round_trip(self) -> None:
        original = DeclaredTracking(
            wells_used={"plate-1": ["A1", "B1"]},
            operations=[
                (
                    DeviceOperation.ASPIRATE,
                    AspirateDetails(
                        labware="plate-1", positions=["A1"], volumes=[50.0],
                    ),
                ),
                (
                    DeviceOperation.SHAKE,
                    ShakeDetails(speed_rpm=400.0, duration_s=60.0),
                ),
            ],
        )
        rebuilt = DeclaredTracking.model_validate_json(
            original.model_dump_json(),
        )
        assert rebuilt == original
        assert rebuilt.operations is not None
        assert rebuilt.operations[0][0] is DeviceOperation.ASPIRATE
        assert isinstance(rebuilt.operations[0][1], AspirateDetails)
        assert rebuilt.operations[1][0] is DeviceOperation.SHAKE
        assert isinstance(rebuilt.operations[1][1], ShakeDetails)

    def test_empty_declared_tracking_round_trip(self) -> None:
        """All-None DeclaredTracking is the common case (authors opt
        each field on individually); pin that it survives JSON.
        """
        original = DeclaredTracking()
        rebuilt = DeclaredTracking.model_validate_json(
            original.model_dump_json(),
        )
        assert rebuilt == original

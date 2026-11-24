"""Creating teachpoints saves every row or none, and a row read from the list saves as it is."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from cheshire_drivers import CartesianCoordinates
from cheshire_drivers.move_parameters import MoveParameterPatch
from cheshire_drivers.teachpoints import Teachpoint
from pydantic import JsonValue

from orca.operations.protocol import OperationError
from orca.operations.teachpoint_transfer import CreateTeachpointsOperation
from orca.operations.teachpoint_transfer_models import CreateTeachpointsRequest, CreateTeachpointsResponse
from orca.operations.teachpoints import ListTeachpointsOperation
from orca.operations.teachpoint_models import ListTeachpointsRequest
from orca.runtime.errors import NotATransporterError

COORDS: dict[str, JsonValue] = {"x": 1.0, "y": 2.0, "z": 3.0, "yaw": 0.0, "pitch": 90.0, "roll": 180.0}
GOOD_ROW: dict[str, JsonValue] = {
    "position_id": "nest_1", "coord_type": "cartesian", "coords": COORDS, "orientation": "right",
}
MISSING_ACCESS_CONFIG = "AccessConfig 'nope' not found; create it first"


def _taught(name: str) -> Teachpoint:
    return Teachpoint(name, CartesianCoordinates(9.0, 9.0, 9.0, 0.0, 90.0, 180.0), orientation="left")


def _runtime(existing: frozenset[str] = frozenset()) -> MagicMock:
    runtime = MagicMock()
    runtime.teachpoints.list = AsyncMock(return_value=[_taught(name) for name in sorted(existing)])
    runtime.teachpoints.add = AsyncMock(return_value=None)
    runtime.teachpoints.update = AsyncMock(return_value=None)
    runtime.teachpoints.delete = AsyncMock(return_value=True)
    return runtime


def _registries() -> MagicMock:
    registries = MagicMock()
    registries.access_configs.get = AsyncMock(return_value=None)
    return registries


async def _create(
    runtime: MagicMock, rows: list[dict[str, JsonValue]], device_id: str = "arm_1",
    **fields: bool | str,
) -> CreateTeachpointsResponse:
    op = CreateTeachpointsOperation(runtime=runtime, registries=_registries())
    request: dict[str, JsonValue] = {"device_id": device_id, "teachpoints": list[JsonValue](rows), **fields}
    result = await op.run(CreateTeachpointsRequest.model_validate(request))
    assert isinstance(result, CreateTeachpointsResponse)
    return result


async def _refused(runtime: MagicMock, rows: list[dict[str, JsonValue]], *, overwrite: bool = False) -> OperationError:
    with pytest.raises(OperationError) as refused:
        await _create(runtime, rows, overwrite=overwrite)
    return refused.value


def _sent(runtime: MagicMock) -> list[str]:
    return [call.args[1].position_id for call in runtime.teachpoints.add.await_args_list]


def _without(key: str) -> dict[str, JsonValue]:
    row = {**GOOD_ROW, "position_id": "bad_1"}
    del row[key]
    return row


BAD_ROWS: dict[str, dict[str, JsonValue]] = {
    "coords_value_not_a_number": {"coords": {**COORDS, "x": "far"}},
    "coords_incomplete": {"coords": {"x": 1.0}},
    "coords_a_list": {"coords": [1.0, 2.0, 3.0]},
    "by_labware_unknown_field": {"by_labware": {"costar_96": {"nope": 1}}},
    "taught_with_an_int": {"taught_with": 5},
    "coord_type_unknown": {"coord_type": "polar"},
    "orientation_unknown": {"orientation": "up"},
    "unknown_key": {"taughtwith": "costar_96"},
    "access_config_missing": {"access_config_name": "nope"},
}
CASES: dict[str, tuple[dict[str, JsonValue], str]] = {
    **{name: ({**GOOD_ROW, "position_id": "bad_1", **bad}, "bad_1") for name, bad in BAD_ROWS.items()},
    "orientation_missing": (_without("orientation"), "bad_1"),
    "position_id_missing": (_without("position_id"), "index 1"),
}


@pytest.mark.parametrize("case", list(CASES))
async def test_one_bad_row_refuses_the_request_and_saves_nothing(case: str) -> None:
    bad_row, label = CASES[case]
    runtime = _runtime()

    refused = await _refused(runtime, [GOOD_ROW, bad_row])

    assert (refused.surface_code(), refused.http_status) == ("validation_error", 422)
    assert refused.extras is not None
    errors = refused.extras["errors"]
    assert isinstance(errors, list) and len(errors) == 1 and label in str(errors[0])
    runtime.teachpoints.add.assert_not_awaited()


async def test_every_refused_row_is_listed() -> None:
    refused = await _refused(_runtime(), [
        {**GOOD_ROW, "position_id": "a", "orientation": "up"}, {**GOOD_ROW, "position_id": "b", "coord_type": "polar"},
    ])

    assert refused.extras is not None
    errors = refused.extras["errors"]
    assert isinstance(errors, list) and len(errors) == 2


async def test_a_name_twice_in_one_request_is_refused() -> None:
    runtime = _runtime()

    refused = await _refused(runtime, [GOOD_ROW, GOOD_ROW])

    assert refused.surface_code() == "validation_error"
    runtime.teachpoints.add.assert_not_awaited()


async def test_a_name_with_a_labware_segment_is_saved() -> None:
    """The per-labware overrides are served under their own path, so no teachpoint name reaches them."""
    runtime = _runtime()

    await _create(runtime, [{**GOOD_ROW, "position_id": "flex_1/labware/B4"}])

    assert _sent(runtime) == ["flex_1/labware/B4"]


async def test_a_taken_name_refuses_the_request_without_overwrite() -> None:
    runtime = _runtime(existing=frozenset({"nest_1"}))

    refused = await _refused(runtime, [{**GOOD_ROW, "position_id": "new_1"}, GOOD_ROW])

    assert (refused.surface_code(), refused.http_status) == ("already_exists", 409)
    assert refused.extras is not None and refused.extras["names"] == ["nest_1"]
    runtime.teachpoints.add.assert_not_awaited()
    runtime.teachpoints.update.assert_not_awaited()


async def test_overwrite_replaces_a_taken_name() -> None:
    runtime = _runtime(existing=frozenset({"nest_1"}))

    result = await _create(runtime, [GOOD_ROW], overwrite=True)

    assert (result.created, result.updated) == (0, 1)
    assert runtime.teachpoints.update.await_args.args[1].position_id == "nest_1"


async def test_a_missing_access_config_is_named_as_create_names_it() -> None:
    refused = await _refused(_runtime(), [{**GOOD_ROW, "access_config_name": "nope"}])

    assert refused.extras is not None
    assert refused.extras["errors"] == [f"Teachpoint 'nest_1': {MISSING_ACCESS_CONFIG}"]


async def test_a_save_the_store_refuses_puts_back_what_the_request_already_saved() -> None:
    runtime = _runtime(existing=frozenset({"nest_1"}))
    runtime.teachpoints.add = AsyncMock(side_effect=[None, ValueError("location 'nest_3' is not in the route graph")])

    refused = await _refused(runtime, [
        GOOD_ROW, {**GOOD_ROW, "position_id": "nest_2"}, {**GOOD_ROW, "position_id": "nest_3"},
    ], overwrite=True)

    assert refused.surface_code() == "validation_error"
    assert "location 'nest_3' is not in the route graph" in refused.message
    assert [c.args[1] for c in runtime.teachpoints.delete.await_args_list] == ["nest_2"]
    restored = runtime.teachpoints.update.await_args_list[-1].args[1]
    assert (restored.position_id, restored.orientation) == ("nest_1", "left")


async def test_the_summary_counts_what_was_saved() -> None:
    result = await _create(_runtime(existing=frozenset({"nest_1"})), [
        GOOD_ROW, {**GOOD_ROW, "position_id": "nest_2"},
    ], overwrite=True)

    assert result.summary == "saved 2 teachpoints (1 created, 1 updated)"


async def test_overrides_and_what_the_arm_held_are_kept() -> None:
    runtime = _runtime()

    await _create(runtime, [{
        **GOOD_ROW, "taught_with": "costar_96", "by_labware": {"costar_96": {"z_offset": 3.0}},
    }])

    teachpoint = runtime.teachpoints.add.await_args.args[1]
    assert teachpoint.taught_with == "costar_96"
    assert teachpoint.by_labware["costar_96"].z_offset == 3.0


async def test_null_by_labware_saves_no_overrides() -> None:
    runtime = _runtime()

    await _create(runtime, [{**GOOD_ROW, "by_labware": None}])

    assert dict(runtime.teachpoints.add.await_args.args[1].by_labware) == {}


async def test_the_reason_reaches_every_save() -> None:
    runtime = _runtime()

    await _create(runtime, [GOOD_ROW], reason="re-taught nest")

    assert runtime.teachpoints.add.await_args.kwargs == {"reason": "re-taught nest", "confirm": True}


@pytest.mark.parametrize("rows", [[GOOD_ROW], [{**GOOD_ROW, "orientation": "up"}]],
                         ids=["a_valid_row", "only_refused_rows"])
async def test_an_unknown_device_is_refused_before_any_row_is_read(
    rows: list[dict[str, JsonValue]],
) -> None:
    runtime = _runtime()
    runtime.teachpoints.list = AsyncMock(side_effect=KeyError("arm_1"))

    refused = await _refused(runtime, rows)

    assert (refused.surface_code(), refused.http_status) == ("device_not_found", 404)
    runtime.teachpoints.add.assert_not_awaited()


async def test_a_device_that_is_not_a_transporter_is_a_validation_error() -> None:
    runtime = _runtime()
    runtime.teachpoints.list = AsyncMock(side_effect=NotATransporterError("shaker_1 is not a transporter"))

    refused = await _refused(runtime, [GOOD_ROW])

    assert (refused.surface_code(), refused.http_status) == ("validation_error", 422)


async def test_a_listed_row_saves_onto_another_arm() -> None:
    runtime = _runtime()
    runtime.teachpoints.list = AsyncMock(return_value=[Teachpoint(
        "nest_1", CartesianCoordinates(1.0, 2.0, 3.0, 0.0, 90.0, 180.0), orientation="right",
        taught_with="costar_96", by_labware={"costar_96": MoveParameterPatch(z_offset=3.0)},
    )])
    listed = await ListTeachpointsOperation(runtime=runtime).run(ListTeachpointsRequest(device_id="arm_1"))
    rows = [row.model_dump(mode="json") for row in listed.root]
    runtime.teachpoints.list = AsyncMock(return_value=[])

    await _create(runtime, rows, device_id="arm_2")

    device_id, teachpoint = runtime.teachpoints.add.await_args.args
    assert device_id == "arm_2"
    assert teachpoint.position_id == "nest_1"
    assert teachpoint.by_labware["costar_96"].z_offset == 3.0

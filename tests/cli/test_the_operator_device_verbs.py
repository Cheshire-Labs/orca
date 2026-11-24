"""Each operator device verb sends its Operation's request to the route that Operation is served at."""

import pytest
from pydantic import BaseModel, JsonValue
from typer.testing import CliRunner

from orca.cli import device_operator
from orca.cli.app import app
from orca.operations.operator_device_models import (
    ASPIRATE,
    BATCH_EXECUTE,
    CONFIGURE_DECK,
    MOVE_TO,
    OPEN_GRIPPER,
    PICK_PLATE,
    PLACE_PLATE,
    SAVE_CURRENT_POSITION,
    OperatorRoute,
    TransporterCommandResponse,
)


class _Client:
    def __init__(self) -> None:
        self.sent: list[tuple[OperatorRoute, BaseModel]] = []

    def device_operator_command(self, route: OperatorRoute, req: BaseModel) -> BaseModel:
        self.sent.append((route, req))
        return TransporterCommandResponse.model_validate({"message": "done", "result": {}})


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> _Client:
    fake = _Client()
    monkeypatch.setattr(device_operator, "get_client", lambda: fake)
    return fake




@pytest.mark.parametrize(("argv", "route", "fields"), [
    (["device", "move-to", "arm", "hotel_1"], MOVE_TO, {"position_id": "hotel_1"}),
    (["device", "pick-plate", "arm", "hotel_1"], PICK_PLATE, {"position_id": "hotel_1"}),
    (["device", "place-plate", "arm", "hotel_1"], PLACE_PLATE, {"position_id": "hotel_1"}),
    (["device", "open-gripper", "arm", "--jaw-opening", "5"], OPEN_GRIPPER, {"jaw_opening": 5.0}),
    (["device", "configure-deck", "lh", "main"], CONFIGURE_DECK, {"layout_name": "main"}),
    (
        ["device", "aspirate", "lh", "--request", '{"aspirations": [{"labware": "p1", "volumes": [10]}]}'],
        ASPIRATE, {"use_channels": None},
    ),
    (["device", "batch", "shaker", "--commands", '[{"command": "open"}]'], BATCH_EXECUTE, {"stop_on_error": True}),
    (
        ["teachpoints", "capture", "arm", "hotel_1", "--orientation", "left"],
        SAVE_CURRENT_POSITION, {"coord_type": "cartesian", "orientation": "left"},
    ),
])
def test_a_verb_sends_its_operations_request_to_its_route(
    client: _Client, argv: list[str], route: OperatorRoute, fields: dict[str, JsonValue],
) -> None:
    result = CliRunner().invoke(app, argv)

    assert result.exit_code == 0, result.output
    ((sent_route, req),) = client.sent
    assert sent_route is route
    dumped = req.model_dump(mode="json")
    assert dumped["device_id"] == argv[2]
    for name, value in fields.items():
        assert dumped[name] == value

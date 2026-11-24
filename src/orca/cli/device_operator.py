"""`orca device` verbs for the operator commands that resolve something before they send, and `orca teachpoints capture`.

Each sends the Operation's own request to the route that Operation declares, on whichever server the CLI targets.
"""

import json

import typer
from pydantic import BaseModel, JsonValue, ValidationError

from orca.cli import output
from orca.cli.backend import get_client
from orca.operations.operator_device_models import (
    ASPIRATE,
    BATCH_EXECUTE,
    CONFIGURE_DECK,
    DISPENSE,
    MIX,
    MOVE_TO,
    OPEN_GRIPPER,
    PICK_PLATE,
    PLACE_PLATE,
    SAVE_CURRENT_POSITION,
    OperatorRoute,
)

def _json_object(text: str, flag: str) -> dict[str, JsonValue]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        output.fail(f"{flag} must be valid JSON: {exc}", code=output.EXIT_USAGE)
    if not isinstance(parsed, dict):
        output.fail(f"{flag} must be a JSON object", code=output.EXIT_USAGE)
    return parsed


def _send(route: OperatorRoute, fields: dict[str, JsonValue]) -> None:
    try:
        request = route.request.model_validate(fields)
    except ValidationError as exc:
        output.fail(str(exc), code=output.EXIT_USAGE)
    answer: BaseModel = get_client().device_operator_command(route, request)
    dumped = answer.model_dump(mode="json")
    if output.get_mode().value == "json" or not isinstance(dumped, dict) or "message" not in dumped:
        output.emit_document(dumped)
        return
    output.info(str(dumped["message"]))


def register_device_verbs(app: typer.Typer) -> None:
    """Add the operator verbs to `orca device`."""

    @app.command("move-to")
    def move_to(name: str = typer.Argument(...), position_id: str = typer.Argument(...)) -> None:
        """Move a transporter to one of its teachpoints."""
        _send(MOVE_TO, {"device_id": name, "position_id": position_id})

    @app.command("pick-plate")
    def pick_plate(
        name: str = typer.Argument(...), position_id: str = typer.Argument(...),
    ) -> None:
        """Pick the plate the record has at a site, as a run's move does, holding the arm throughout."""
        _send(PICK_PLATE, {"device_id": name, "position_id": position_id})

    @app.command("place-plate")
    def place_plate(
        name: str = typer.Argument(...), position_id: str = typer.Argument(...),
    ) -> None:
        """Place the plate in the jaws at a site, as a run's move does, holding the arm throughout."""
        _send(PLACE_PLATE, {"device_id": name, "position_id": position_id})

    @app.command("open-gripper")
    def open_gripper(
        name: str = typer.Argument(...),
        jaw_opening: float | None = typer.Option(None, "--jaw-opening", help="How far past the grip position."),
        position: float | None = typer.Option(None, "--position", help="An absolute gripper-axis target."),
    ) -> None:
        """Open the jaws by the arm's stored opening, or by the amount named."""
        _send(OPEN_GRIPPER, {"device_id": name, "jaw_opening": jaw_opening, "position": position})

    @app.command("configure-deck")
    def configure_deck(
        name: str = typer.Argument(...), layout_name: str = typer.Argument(...),
        reason: str | None = typer.Option(None, "--reason", help="Why, for the audit trail."),
    ) -> None:
        """Push the deck layout stored under the name to the liquid handler. `orca deck-layouts save` stores it."""
        _send(CONFIGURE_DECK, {"device_id": name, "layout_name": layout_name, "reason": reason})

    for verb, route in (("aspirate", ASPIRATE), ("dispense", DISPENSE), ("mix", MIX)):
        _register_pipetting(app, verb, route)

    @app.command("batch")
    def batch(
        name: str = typer.Argument(...),
        commands: str = typer.Option(..., "--commands", help="A JSON list of {command, params, timeout, confirm}."),
        keep_going: bool = typer.Option(False, "--keep-going", help="Run the rest after a command fails."),
    ) -> None:
        """Run commands in order on one device, holding it throughout."""
        try:
            listed = json.loads(commands)
        except json.JSONDecodeError as exc:
            output.fail(f"--commands must be valid JSON: {exc}", code=output.EXIT_USAGE)
        _send(BATCH_EXECUTE, {"device_id": name, "commands": listed, "stop_on_error": not keep_going})


def _register_pipetting(app: typer.Typer, verb: str, route: OperatorRoute) -> None:
    @app.command(verb)
    def pipette(
        name: str = typer.Argument(...),
        request: str = typer.Option(..., "--request", help=f"The {verb} request as a JSON object, without device_id."),
    ) -> None:
        _send(route, {**_json_object(request, "--request"), "device_id": name})

    pipette.__doc__ = f"Send `{verb}` to a liquid handler, with the pipetting layers the request states."


def register_teachpoint_verbs(app: typer.Typer) -> None:
    """Add `capture` to `orca teachpoints`."""

    @app.command("capture")
    def capture(
        name: str = typer.Argument(..., help="The transporter."),
        position_id: str = typer.Argument(...),
        coord_type: str = typer.Option("cartesian", "--coord-type", help="cartesian or joint."),
        orientation: str | None = typer.Option(None, "--orientation", help="left or right; cartesian needs one."),
        access_config: str | None = typer.Option(None, "--access-config"),
        gateway: str | None = typer.Option(None, "--gateway", help="The teachpoint this one is reached through."),
        reason: str | None = typer.Option(None, "--reason", help="Why, for the audit trail."),
    ) -> None:
        """Save where the arm stands as a teachpoint. On a device set to sim this saves the simulator's position."""
        _send(SAVE_CURRENT_POSITION, {
            "device_id": name, "position_id": position_id, "coord_type": coord_type,
            "orientation": orientation, "access_config_name": access_config, "gateway": gateway, "reason": reason,
        })

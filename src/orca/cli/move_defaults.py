"""`orca move-defaults ...` noun sub-app.

Read and edit what an arm's picks and places start from. An edit names the
fields it changes; everything unnamed keeps whatever it had, which is what lets
one number be corrected without restating the other nine.
"""

from typing import List, Optional

import typer
from cheshire_drivers.move_parameters import MoveParameterPatch
from pydantic import ValidationError

from orca.cli import output
from orca.cli.backend import get_client
from orca.daemon.schemas import MoveDefaultsDTO, MoveDefaultsPatchRequest


app = typer.Typer(
    help="What an arm's moves start from, per transporter.",
    no_args_is_help=True,
)


def _emit(record: MoveDefaultsDTO) -> None:
    if output.get_mode().value == "json":
        output.emit_json(record.model_dump(mode="json"))
        return
    output.emit_kv(
        f"move defaults {record.transporter_name}",
        [
            (field, f"{value} ({record.sources[field]})")
            for field, value in record.parameters.model_dump().items()
        ],
    )


@app.command("list")
def list_move_defaults() -> None:
    """List every arm's starting numbers."""
    client = get_client()
    records = client.move_defaults_list()
    if output.get_mode().value == "json":
        output.emit_json([r.model_dump(mode="json") for r in records])
        return
    output.emit_table(
        "MoveDefaults",
        ["transporter", "tuned fields"],
        [
            [
                r.transporter_name,
                ", ".join(
                    field for field, source in r.sources.items()
                    if source != "seed"
                ) or "-",
            ]
            for r in records
        ],
    )


@app.command("show")
def show_move_defaults(
    transporter_name: str = typer.Argument(..., help="Transporter name."),
) -> None:
    """Show one arm's numbers and where each of them came from."""
    _emit(get_client().move_defaults_get(transporter_name))


@app.command("set")
def set_move_defaults(
    transporter_name: str = typer.Argument(..., help="Transporter name."),
    resource_width: Optional[float] = typer.Option(None, "--resource-width"),
    resource_height: Optional[float] = typer.Option(None, "--resource-height"),
    travel_margin: Optional[float] = typer.Option(None, "--travel-margin"),
    jaw_opening: Optional[float] = typer.Option(None, "--jaw-opening"),
    z_offset: Optional[float] = typer.Option(None, "--z-offset"),
    speed: Optional[float] = typer.Option(
        None, "--speed", help="Percent of full speed.",
    ),
    clear: List[str] = typer.Option(
        [],
        "--clear",
        help=(
            "Field to hand back to the built-in seed (repeatable): "
            "resource-width, resource-height, travel-margin, jaw-opening, "
            "z-offset, speed."
        ),
    ),
) -> None:
    """Change the numbers named and leave the rest alone.

    The approach fields (access type, clearance, z above, grasp offset) are not
    here: a teachpoint supplies them for every move, so change them on the access
    config it references.
    """
    try:
        patch = MoveParameterPatch.model_validate({
            "resource_width": resource_width,
            "resource_height": resource_height,
            "travel_margin": travel_margin,
            "jaw_opening": jaw_opening,
            "z_offset": z_offset,
            "speed": speed,
        })
        body = MoveDefaultsPatchRequest.model_validate({
            "set": patch.model_dump(exclude_none=True),
            "clear": [field.replace("-", "_") for field in clear],
        })
    except ValidationError as exc:
        output.fail(str(exc), code=output.EXIT_USAGE)
    _emit(get_client().move_defaults_patch(transporter_name, body))


@app.command("reset")
def reset_move_defaults(
    transporter_name: str = typer.Argument(..., help="Transporter name."),
) -> None:
    """Discard everything tuned for this arm and put it back on the seed."""
    client = get_client()
    client.move_defaults_reset(transporter_name)
    output.info(f"reset move defaults for [cyan]{transporter_name}[/cyan]")

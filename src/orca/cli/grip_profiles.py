"""`orca grip-profiles ...` noun sub-app.

Read and edit how each labware type is held. An edit names the fields it
changes; everything unnamed keeps inheriting from the arm underneath, which is
what lets one plate's grip width be corrected without restating the rest.
"""

from typing import List, Optional

import typer
from cheshire_drivers.move_parameters import MoveParameterPatch
from pydantic import ValidationError

from orca.cli import output
from orca.cli.backend import get_client
from orca.daemon.schemas import GripProfileDTO, GripProfilePatchRequest


app = typer.Typer(
    help="How each labware type is held, per labware type.",
    no_args_is_help=True,
)


def _emit(record: GripProfileDTO) -> None:
    if output.get_mode().value == "json":
        output.emit_json(record.model_dump(mode="json"))
        return
    named = record.patch.model_dump(exclude_none=True)
    if not named:
        output.info(
            f"[cyan]{record.labware_type}[/cyan] has no grip profile; "
            "it is held the way the arm holds anything else"
        )
        return
    output.emit_kv(
        f"grip profile {record.labware_type}",
        [(field, str(value)) for field, value in named.items()],
    )


@app.command("list")
def list_grip_profiles() -> None:
    """List every labware type somebody has measured a grip for."""
    records = get_client().grip_profiles_list()
    if output.get_mode().value == "json":
        output.emit_json([r.model_dump(mode="json") for r in records])
        return
    output.emit_table(
        "Grip profiles",
        ["labware type", "set fields"],
        [
            [
                r.labware_type,
                ", ".join(
                    f"{field}={value}"
                    for field, value in r.patch.model_dump(exclude_none=True).items()
                ) or "-",
            ]
            for r in records
        ],
    )


@app.command("show")
def show_grip_profile(
    labware_type: str = typer.Argument(..., help="Labware type."),
) -> None:
    """Show what one labware type claims about how it is held."""
    _emit(get_client().grip_profiles_get(labware_type))


@app.command("set")
def set_grip_profile(
    labware_type: str = typer.Argument(..., help="Labware type."),
    resource_width: Optional[float] = typer.Option(
        None, "--resource-width", help="Jaw separation on this labware's skirt.",
    ),
    resource_height: Optional[float] = typer.Option(None, "--resource-height"),
    travel_margin: Optional[float] = typer.Option(None, "--travel-margin"),
    jaw_opening: Optional[float] = typer.Option(None, "--jaw-opening"),
    plate_present_margin: Optional[float] = typer.Option(
        None, "--plate-present-margin",
        help="How far above a nest this labware still reads as present, in mm.",
    ),
    z_offset: Optional[float] = typer.Option(
        None, "--z-offset", help="Grip height relative to what a position was taught with.",
    ),
    grip_distance_from_top: Optional[float] = typer.Option(
        None, "--grip-distance-from-top",
        help="How far below its top this labware is gripped, in mm.",
    ),
    speed: Optional[float] = typer.Option(
        None, "--speed", help="Percent of full speed.",
    ),
    clear: List[str] = typer.Option(
        [], "--clear", help="Field to hand back to the arm underneath (repeatable).",
    ),
) -> None:
    """Change the numbers named and leave the rest inheriting.

    The approach fields (access type, clearance, z above, grasp offset) are not
    here: a teachpoint supplies them for every move and resolves after this layer,
    so change them on the access config it references. For one labware at one
    position, use a per-labware override on the teachpoint instead.
    """
    try:
        patch = MoveParameterPatch.model_validate({
            "resource_width": resource_width,
            "resource_height": resource_height,
            "travel_margin": travel_margin,
            "jaw_opening": jaw_opening,
            "plate_present_margin": plate_present_margin,
            "z_offset": z_offset,
            "grip_distance_from_top": grip_distance_from_top,
            "speed": speed,
        })
        body = GripProfilePatchRequest.model_validate({
            "set": patch.model_dump(exclude_none=True),
            "clear": clear,
        })
    except ValidationError as exc:
        output.fail(str(exc), code=output.EXIT_USAGE)
    if not body.set.model_dump(exclude_none=True) and not body.clear:
        output.fail(
            "name at least one field to set or --clear", code=output.EXIT_USAGE,
        )
    _emit(get_client().grip_profiles_patch(labware_type, body))


@app.command("reset")
def reset_grip_profile(
    labware_type: str = typer.Argument(..., help="Labware type."),
) -> None:
    """Discard everything measured for this labware type."""
    get_client().grip_profiles_reset(labware_type)
    output.info(f"reset grip profile for [cyan]{labware_type}[/cyan]")

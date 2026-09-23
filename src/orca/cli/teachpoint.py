"""`orca teachpoints ...` noun sub-app.

CRUD verbs over the per-transporter teachpoint registry. Coordinates are
passed as a small flat JSON dict via ``--coords`` (inline) or
``--coords-file`` (path); the scalar fields ride as typed options. The
coord conversion + validation runs server-side in the shared
teachpoint-wire module, so both backends behave identically.
"""

import json
from pathlib import Path

import typer
from cheshire_drivers.move_parameters import MoveParameterPatch
from pydantic import ValidationError

from orca.cli import output
from orca.cli.backend import get_client
from orca.daemon.schemas import GripProfilePatchRequest, TeachpointDTO


app = typer.Typer(
    help="Per-transporter teachpoint registry.",
    no_args_is_help=True,
)


def _load_coords(
    coords: str | None, coords_file: str | None,
) -> dict[str, float | int | str]:
    """Resolve coords from exactly one of ``--coords`` / ``--coords-file``.

    Fails clean on neither/both, an unreadable file, malformed JSON, a
    non-object payload, or any non-scalar value (the wire only accepts
    ``str | int | float`` per coordinate; bool / null / nested are rejected).
    """
    if (coords is None) == (coords_file is None):
        output.fail(
            "pass exactly one of --coords (inline JSON) or --coords-file (path)",
            code=output.EXIT_USAGE,
        )
    raw = coords
    if coords_file is not None:
        try:
            raw = Path(coords_file).read_text(encoding="utf-8")
        except OSError as exc:
            output.fail(
                f"failed to read coords JSON from {coords_file!r}: {exc}",
                code=output.EXIT_USAGE,
            )
    try:
        parsed = json.loads(raw or "")
    except json.JSONDecodeError as exc:
        output.fail(f"invalid coords JSON: {exc}", code=output.EXIT_USAGE)
    if not isinstance(parsed, dict):
        output.fail("coords must be a JSON object", code=output.EXIT_USAGE)
    result: dict[str, float | int | str] = {}
    for key, value in parsed.items():
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            output.fail(
                f"coords value for {key!r} must be a number or string, "
                f"got {type(value).__name__}",
                code=output.EXIT_USAGE,
            )
        result[key] = value
    return result


@app.command("list")
def list_teachpoints(
    device_id: str | None = typer.Argument(
        None,
        help="Transporter device id. Omit to list across all transporters.",
    ),
) -> None:
    """List teachpoints (across all transporters by default, or for one).

    ``device_id`` is optional so this verb matches ``labware list`` /
    ``device list`` -- no args yields every teachpoint with its owning
    device id surfaced as an extra column.
    """
    client = get_client()
    if device_id is None:
        items = client.teachpoints_list_all()
        title = "Teachpoints (all)"
        columns = ["device_id", "position_id", "coord_type", "access_config", "gateway"]
        rows = [
            [
                i.device_id, i.position_id, i.coord_type,
                i.access_config_name or "",
                i.gateway or "",
            ]
            for i in items
        ]
    else:
        items = client.teachpoints_list(device_id)
        title = f"Teachpoints ({device_id})"
        columns = ["position_id", "coord_type", "access_config", "gateway"]
        rows = [
            [
                i.position_id, i.coord_type,
                i.access_config_name or "",
                i.gateway or "",
            ]
            for i in items
        ]
    if output.get_mode().value == "json":
        output.emit_json([i.model_dump(mode="json") for i in items])
        return
    output.emit_table(title, columns, rows)


@app.command("show")
def show_teachpoint(
    device_id: str = typer.Argument(..., help="Transporter device id."),
    position_id: str = typer.Argument(..., help="Position identifier (deck slot) this teachpoint targets."),
) -> None:
    """Show one teachpoint's coords + access config + gateway."""
    client = get_client()
    tp = client.teachpoints_get(device_id, position_id)
    if output.get_mode().value == "json":
        output.emit_json(tp.model_dump(mode="json"))
        return
    rows: list[tuple[str, str | None]] = [
        ("device", tp.device_id),
        ("position_id", tp.position_id),
        ("coord_type", tp.coord_type),
        ("access_config", tp.access_config_name or ""),
        ("orientation", tp.orientation or ""),
        ("gateway", tp.gateway or ""),
        ("taught_with", tp.taught_with or ""),
    ]
    for labware_type, patch in tp.by_labware.items():
        named = patch.model_dump(exclude_none=True)
        rows.append((
            f"labware {labware_type}",
            ", ".join(f"{f}={v}" for f, v in named.items()),
        ))
    for k, v in tp.coords.items():
        if k == "type":
            continue
        rows.append((k, str(v)))
    output.emit_kv(f"teachpoint {tp.position_id}", rows)


@app.command("create")
def create_teachpoint(
    device_id: str = typer.Argument(..., help="Transporter device id."),
    position_id: str = typer.Argument(..., help="Position identifier (deck slot)."),
    coord_type: str = typer.Option(
        ..., "--coord-type", help="cartesian or joint.",
    ),
    coords: str | None = typer.Option(
        None, "--coords", help="Inline coordinate JSON object.",
    ),
    coords_file: str | None = typer.Option(
        None, "--coords-file", help="Path to a JSON file with the coordinates.",
    ),
    access_config: str | None = typer.Option(
        None, "--access-config", help="Named access config to reference.",
    ),
    gateway: str | None = typer.Option(None, "--gateway"),
    orientation: str | None = typer.Option(
        None, "--orientation", help="left or right (required for cartesian).",
    ),
    taught_with: str | None = typer.Option(
        None, "--taught-with",
        help="Labware type this position was jogged to; grip heights read against it.",
    ),
) -> None:
    """Register a new teachpoint. 400 on bad coords or unknown access config,
    404 on unknown device, 409 on duplicate."""
    coord_data = _load_coords(coords, coords_file)
    client = get_client()
    created = client.teachpoints_add(
        device_id, position_id, coord_type, coord_data,
        access_config, gateway, orientation, taught_with,
    )
    if output.get_mode().value == "json":
        output.emit_json(created.model_dump(mode="json"))
        return
    output.info(
        f"created teachpoint [cyan]{created.position_id}[/cyan] on {created.device_id}",
    )


@app.command("update")
def update_teachpoint(
    device_id: str = typer.Argument(..., help="Transporter device id."),
    position_id: str = typer.Argument(..., help="Position identifier to update."),
    coords: str | None = typer.Option(
        None, "--coords", help="Inline coordinate JSON object.",
    ),
    coords_file: str | None = typer.Option(
        None, "--coords-file", help="Path to a JSON file with the coordinates.",
    ),
    access_config: str | None = typer.Option(
        None, "--access-config",
        help="New access config name (only changed when passed).",
    ),
    gateway: str | None = typer.Option(
        None, "--gateway", help="New gateway (only changed when passed).",
    ),
    orientation: str | None = typer.Option(
        None, "--orientation", help="New orientation (only changed when passed).",
    ),
    taught_with: str | None = typer.Option(
        None, "--taught-with",
        help="Labware type this position was jogged to (only changed when passed).",
    ),
) -> None:
    """Update a teachpoint's coordinates. Access config, gateway, orientation
    and taught-with change only when their option is passed. Per-labware
    overrides are left alone; edit them with `teachpoint labware`. coord_type is
    preserved. 404 if the teachpoint or device is unknown."""
    coord_data = _load_coords(coords, coords_file)
    client = get_client()
    updated = client.teachpoints_update(
        device_id, position_id, coord_data,
        access_config, gateway, orientation,
        access_config is not None, gateway is not None, orientation is not None,
        taught_with, taught_with is not None,
    )
    if output.get_mode().value == "json":
        output.emit_json(updated.model_dump(mode="json"))
        return
    output.info(
        f"updated teachpoint [cyan]{updated.position_id}[/cyan] on {updated.device_id}",
    )


@app.command("delete")
def delete_teachpoint(
    device_id: str = typer.Argument(..., help="Transporter device id."),
    position_id: str = typer.Argument(..., help="Position identifier to delete."),
) -> None:
    """Delete a teachpoint. 404 if the teachpoint or device is unknown."""
    client = get_client()
    client.teachpoints_delete(device_id, position_id)
    output.info(
        f"deleted teachpoint [cyan]{position_id}[/cyan] on {device_id}",
    )


labware_app = typer.Typer(
    help="Per-labware exceptions at one position: the narrowest layer there is.",
    no_args_is_help=True,
)
app.add_typer(labware_app, name="labware")


@labware_app.command("set")
def set_labware_override(
    device_id: str = typer.Argument(..., help="Transporter device id."),
    position_id: str = typer.Argument(..., help="Position identifier."),
    labware_type: str = typer.Argument(..., help="Labware type this applies to."),
    access_type: str | None = typer.Option(
        None, "--access-type", help="vertical or horizontal.",
    ),
    clearance: float | None = typer.Option(None, "--clearance"),
    z_above: float | None = typer.Option(None, "--z-above"),
    grasp_offset: float | None = typer.Option(None, "--grasp-offset"),
    resource_width: float | None = typer.Option(None, "--resource-width"),
    resource_height: float | None = typer.Option(None, "--resource-height"),
    travel_margin: float | None = typer.Option(None, "--travel-margin"),
    jaw_opening: float | None = typer.Option(None, "--jaw-opening"),
    z_offset: float | None = typer.Option(
        None, "--z-offset", help="Grip height relative to what this position was taught with.",
    ),
    speed: float | None = typer.Option(None, "--speed", help="Percent of full speed."),
    clear: list[str] = typer.Option(
        [], "--clear", help="Field to hand back to the layers underneath (repeatable).",
    ),
) -> None:
    """Change how one labware type is handled at this one position."""
    try:
        patch = MoveParameterPatch.model_validate({
            "access_type": access_type,
            "clearance": clearance,
            "z_above": z_above,
            "grasp_offset": grasp_offset,
            "resource_width": resource_width,
            "resource_height": resource_height,
            "travel_margin": travel_margin,
            "jaw_opening": jaw_opening,
            "z_offset": z_offset,
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
    updated = get_client().teachpoints_set_labware_override(
        device_id, position_id, labware_type, body,
    )
    _emit_labware_overrides(updated)


@labware_app.command("clear")
def clear_labware_override(
    device_id: str = typer.Argument(..., help="Transporter device id."),
    position_id: str = typer.Argument(..., help="Position identifier."),
    labware_type: str = typer.Argument(..., help="Labware type to stop excepting."),
) -> None:
    """Handle this labware the way the position handles everything else."""
    get_client().teachpoints_clear_labware_override(
        device_id, position_id, labware_type,
    )
    output.info(
        f"cleared the [cyan]{labware_type}[/cyan] override at "
        f"{position_id} on {device_id}",
    )


@labware_app.command("list")
def list_labware_overrides(
    device_id: str = typer.Argument(..., help="Transporter device id."),
    position_id: str = typer.Argument(..., help="Position identifier."),
) -> None:
    """List every labware this position treats as an exception."""
    _emit_labware_overrides(get_client().teachpoints_get(device_id, position_id))


def _emit_labware_overrides(tp: TeachpointDTO) -> None:
    if output.get_mode().value == "json":
        output.emit_json(tp.model_dump(mode="json"))
        return
    if not tp.by_labware:
        output.info(
            f"[cyan]{tp.position_id}[/cyan] handles every labware the same way"
        )
        return
    output.emit_table(
        "Per-labware overrides",
        ["labware type", "set fields"],
        [
            [
                labware_type,
                ", ".join(
                    f"{field}={value}"
                    for field, value in patch.model_dump(exclude_none=True).items()
                ),
            ]
            for labware_type, patch in tp.by_labware.items()
        ],
    )

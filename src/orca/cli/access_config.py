"""`orca access-configs ...` noun sub-app.

CRUD verbs over the deployment-wide access-config registry. Reads and
writes route through the same control-plane client both backends share.
"""

import typer
from pydantic import ValidationError

from cheshire_drivers.teachpoints import AccessConfig

from orca.cli import output
from orca.cli.backend import get_client


app = typer.Typer(
    help="Deployment-wide access-config registry.",
    no_args_is_help=True,
)


def _build_access_config(
    name: str, access_type: str, gripper_offset: float,
    vertical_clearance: float, horizontal_clearance: float,
) -> AccessConfig:
    """Validate CLI inputs into an AccessConfig value, failing clean on bad
    fields (e.g. an access_type outside vertical/horizontal)."""
    try:
        return AccessConfig.model_validate({
            "name": name,
            "access_type": access_type,
            "gripper_offset": gripper_offset,
            "vertical_clearance": vertical_clearance,
            "horizontal_clearance": horizontal_clearance,
        })
    except ValidationError as exc:
        output.fail(str(exc), code=output.EXIT_USAGE)


@app.command("list")
def list_access_configs() -> None:
    """List all registered access configs."""
    client = get_client()
    items = client.access_configs_list()
    if output.get_mode().value == "json":
        output.emit_json([i.model_dump(mode="json") for i in items])
        return
    output.emit_table(
        "AccessConfigs",
        ["name", "type", "gripper_offset", "vert_clear", "horz_clear"],
        [
            [
                i.name, i.access_type,
                f"{i.gripper_offset:.1f}",
                f"{i.vertical_clearance:.1f}",
                f"{i.horizontal_clearance:.1f}",
            ]
            for i in items
        ],
    )


@app.command("show")
def show_access_config(
    name: str = typer.Argument(..., help="Access config name."),
) -> None:
    """Show one access config's full fields."""
    client = get_client()
    cfg = client.access_configs_get(name)
    if output.get_mode().value == "json":
        output.emit_json(cfg.model_dump(mode="json"))
        return
    output.emit_kv(
        f"access_config {cfg.name}",
        [
            ("access_type", cfg.access_type),
            ("gripper_offset", f"{cfg.gripper_offset:.3f}"),
            ("vertical_clearance", f"{cfg.vertical_clearance:.3f}"),
            ("horizontal_clearance", f"{cfg.horizontal_clearance:.3f}"),
        ],
    )


@app.command("create")
def create_access_config(
    name: str = typer.Argument(..., help="Unique access config name."),
    access_type: str = typer.Option(
        ..., "--access-type", help="vertical or horizontal.",
    ),
    gripper_offset: float = typer.Option(20.0, "--gripper-offset"),
    vertical_clearance: float = typer.Option(20.0, "--vertical-clearance"),
    horizontal_clearance: float = typer.Option(100.0, "--horizontal-clearance"),
) -> None:
    """Register a new access config. 409 on duplicate name."""
    config = _build_access_config(
        name, access_type, gripper_offset,
        vertical_clearance, horizontal_clearance,
    )
    client = get_client()
    created = client.access_configs_add(config)
    if output.get_mode().value == "json":
        output.emit_json(created.model_dump(mode="json"))
        return
    output.info(f"created access_config [cyan]{created.name}[/cyan]")


@app.command("update")
def update_access_config(
    name: str = typer.Argument(..., help="Access config name to update."),
    access_type: str = typer.Option(..., "--access-type"),
    gripper_offset: float = typer.Option(20.0, "--gripper-offset"),
    vertical_clearance: float = typer.Option(20.0, "--vertical-clearance"),
    horizontal_clearance: float = typer.Option(100.0, "--horizontal-clearance"),
) -> None:
    """Update an access config. 404 if the name is unknown."""
    config = _build_access_config(
        name, access_type, gripper_offset,
        vertical_clearance, horizontal_clearance,
    )
    client = get_client()
    updated = client.access_configs_update(config)
    if output.get_mode().value == "json":
        output.emit_json(updated.model_dump(mode="json"))
        return
    output.info(f"updated access_config [cyan]{updated.name}[/cyan]")


@app.command("delete")
def delete_access_config(
    name: str = typer.Argument(..., help="Access config name to delete."),
) -> None:
    """Delete an access config. 409 if protected or still referenced."""
    client = get_client()
    client.access_configs_delete(name)
    output.info(f"deleted access_config [cyan]{name}[/cyan]")

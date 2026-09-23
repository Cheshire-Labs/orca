"""`orca deck-layouts ...` noun sub-app.

CRUD verbs over the per-liquid-handler deck-layout registry. Deck-layout
edits are rebuild-required from the runtime's perspective; reads always
reflect the current registry contents. Writes take a DeckLayoutConfig
JSON file since the config (deck_type + resources) is too large to inline.
"""

import json
from pathlib import Path

import typer
from pydantic import ValidationError

from cheshire_drivers.liquid_handler_models import DeckLayoutConfig

from orca.cli import output
from orca.cli.backend import get_client


app = typer.Typer(
    help="Per-liquid-handler deck-layout registry.",
    no_args_is_help=True,
)


def _read_config_from_path(path: str) -> DeckLayoutConfig:
    """Load + validate a DeckLayoutConfig from a JSON file.

    Fails clean on a missing file, malformed JSON, or a config the
    DeckLayoutConfig validators reject (dup resource names / bad placement).
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        output.fail(
            f"failed to read deck-layout JSON from {path!r}: {exc}",
            code=output.EXIT_USAGE,
        )
    try:
        return DeckLayoutConfig.model_validate(raw)
    except ValidationError as exc:
        output.fail(str(exc), code=output.EXIT_USAGE)


@app.command("list")
def list_deck_layouts(
    device_id: str | None = typer.Argument(
        None,
        help="Liquid handler device id. Omit to list across all liquid handlers.",
    ),
) -> None:
    """List deck layouts (across all liquid handlers by default, or for one).

    ``device_id`` is optional so this verb matches ``labware list`` /
    ``device list``.
    """
    client = get_client()
    if device_id is None:
        items = client.deck_layouts_list_all()
        title = "DeckLayouts (all)"
        columns = ["device_id", "name", "deck_type"]
        rows = [[i.device_id, i.name, i.deck_type] for i in items]
    else:
        items = client.deck_layouts_list(device_id)
        title = f"DeckLayouts ({device_id})"
        columns = ["name", "deck_type"]
        rows = [[i.name, i.deck_type] for i in items]
    if output.get_mode().value == "json":
        output.emit_json([i.model_dump(mode="json") for i in items])
        return
    output.emit_table(title, columns, rows)


@app.command("show")
def show_deck_layout(
    device_id: str = typer.Argument(..., help="Liquid handler device id."),
    name: str = typer.Argument(..., help="Deck layout name."),
) -> None:
    """Show one deck layout (full DeckLayoutConfig as JSON)."""
    client = get_client()
    layout = client.deck_layouts_get(device_id, name)
    if output.get_mode().value == "json":
        output.emit_json(layout.model_dump(mode="json"))
        return
    resources = layout.config.resources
    output.emit_kv(
        f"deck_layout {layout.name}",
        [
            ("device", layout.device_id),
            ("deck_type", layout.config.deck_type),
            ("resources", str(len(resources))),
        ],
    )
    if resources:
        output.emit_table(
            "Resources",
            ["name", "catalog_ref", "rail", "parent_id"],
            [
                [
                    r.name,
                    r.catalog_ref,
                    str(r.rail) if r.rail is not None else "",
                    r.parent_id,
                ]
                for r in resources
            ],
        )
    if output.get_mode().value == "table":
        output.emit_json(layout.config.model_dump(mode="json"))


@app.command("create")
def create_deck_layout(
    device_id: str = typer.Argument(..., help="Liquid handler device id."),
    name: str = typer.Argument(..., help="New deck layout name."),
    config_file: str = typer.Option(
        ..., "--config-file",
        help="Path to a JSON file with the DeckLayoutConfig.",
    ),
) -> None:
    """Register a new deck layout. 404 if the device is not a liquid
    handler; 409 on a duplicate name. Rebuild-required to take effect."""
    config = _read_config_from_path(config_file)
    client = get_client()
    created = client.deck_layouts_add(device_id, name, config)
    if output.get_mode().value == "json":
        output.emit_json(created.model_dump(mode="json"))
        return
    output.info(
        f"created deck_layout [cyan]{created.name}[/cyan] "
        f"on {created.device_id} (rebuild-required)",
    )


@app.command("update")
def update_deck_layout(
    device_id: str = typer.Argument(..., help="Liquid handler device id."),
    name: str = typer.Argument(..., help="Deck layout name to update."),
    config_file: str = typer.Option(..., "--config-file"),
) -> None:
    """Update a deck layout. 404 on unknown device or name.
    Rebuild-required to take effect."""
    config = _read_config_from_path(config_file)
    client = get_client()
    updated = client.deck_layouts_update(device_id, name, config)
    if output.get_mode().value == "json":
        output.emit_json(updated.model_dump(mode="json"))
        return
    output.info(
        f"updated deck_layout [cyan]{updated.name}[/cyan] "
        f"on {updated.device_id} (rebuild-required)",
    )


@app.command("delete")
def delete_deck_layout(
    device_id: str = typer.Argument(..., help="Liquid handler device id."),
    name: str = typer.Argument(..., help="Deck layout name to delete."),
) -> None:
    """Delete a deck layout. 404 on unknown device or name."""
    client = get_client()
    client.deck_layouts_delete(device_id, name)
    output.info(
        f"deleted deck_layout [cyan]{name}[/cyan] on {device_id}",
    )

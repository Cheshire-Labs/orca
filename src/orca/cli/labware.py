"""`orca labware ...` noun sub-app.

The journey, labware-clear and discharge verbs need a cloud backend; the
rest (``list``, ``catalog`` + CRUD, ``where``, ``history``, edit-*,
register) work against either.

Read verbs: list, catalog, where, history, journey.
Operator-override verbs: edit-location, edit-barcode, reset-location, register,
carry, carry-normally, load-run.

``list`` and ``catalog`` are intentionally distinct nouns:
- ``list`` (local-only) enumerates runtime labware INSTANCES tracked
  by ILabwareFacade in the active SystemRuntime.
- ``catalog`` (both backends) enumerates labware DEFINITIONS via the
  deployment-registries catalog facade -- the seeded geometry rows +
  operator-custom rows used to author deck layouts and workflows. The store
  behind the facade is in-memory on the daemon, or whatever store the server injects.
"""

import sys

import typer
from cheshire_drivers.move_parameters import MoveParameterField, MoveParameterPatch
from pydantic import JsonValue, TypeAdapter, ValidationError

from orca.cli import output
from orca.cli.backend import get_client
from orca.cli.control_plane import IControlPlaneClient
from orca.operations.labware_models import JourneyMove
from orca.operations.run_loading_models import LoadRunLabwareRequest, RunLabwareItem
from orca.operations.labware_models import LabwareSnapshotModel
from orca.state.placement import PlacementState
from orca.state.provenance import Provenance


app = typer.Typer(help="Labware identity, location, and operator overrides.", no_args_is_help=True)


def _stdin_is_tty() -> bool:
    """Wrapper for `sys.stdin.isatty()` so tests can patch a single
    seam without fighting CliRunner's stdin redirection.
    """
    return sys.stdin.isatty()


def _each_item(values: list[str]) -> list[str]:
    """Every value of a repeatable option, a comma-separated one counting as several."""
    return [item.strip() for value in values for item in value.split(",") if item.strip()]


def _resolve_labware_id_or_passthrough(
    client: IControlPlaneClient, labware_id: str,
) -> str:
    """Resolve `labware_id` against the local `labware_list()` catalog:
    exact match wins, then unique prefix (>=4 chars), then pass-through.

    Ambiguous prefix fails locally with the candidate list -- the daemon
    does exact match only, so a pass-through would just 404 and lose the
    disambiguation context.

    Pass-through covers the store-only-after-rebuild case. The daemon's
    `LabwareFacade._find_by_id` checks the labware service first, while
    `list_all` only iterates `system.labwares`. After a runtime rebuild
    a labware can live in the durable store without appearing in
    `labware_list()`; passing the input through lets the daemon do the
    authoritative store-first lookup.
    """
    ids = [entry.id for entry in client.labware_list()]
    if labware_id in ids:
        return labware_id
    prefix_matches = [c for c in ids if c.startswith(labware_id)]
    if len(prefix_matches) == 1 and len(labware_id) >= 4:
        return prefix_matches[0]
    if len(prefix_matches) > 1 and len(labware_id) >= 4:
        output.ambiguous("labware", labware_id, prefix_matches)
    return labware_id


@app.command("list")
def list_labware() -> None:
    """List all registered labware instances."""
    client = get_client()
    items = client.labware_list()
    if output.get_mode().value == "json":
        output.emit_json([i.model_dump(mode="json") for i in items])
        return
    output.emit_table(
        "Labware",
        ["id", "template", "type", "barcode", "location"],
        [
            [
                i.id[:8], i.template_name, i.labware_type,
                i.barcode or "", _where(i),
            ]
            for i in items
        ],
    )


def _where(item: LabwareSnapshotModel) -> str:
    """The location column, saying so when the labware is not there yet."""
    if item.current_location is None:
        return ""
    if item.placement is PlacementState.EXPECTED:
        return f"{item.current_location} (expected)"
    if item.placement is PlacementState.RETIRED:
        return f"{item.current_location} (removed)"
    return item.current_location


def _read_geometry_from_path(path: str) -> dict[str, JsonValue]:
    """Read a JSON file containing a labware geometry blob.

    Used by `add` and `update` so operators don't have to inline the
    geometry on the command line. Fails clean if the file is missing or
    isn't JSON-decodable; downstream the server validates the shape.
    """
    import json
    from pathlib import Path as _Path
    contents = _Path(path).read_text(encoding="utf-8")
    try:
        return json.loads(contents)
    except json.JSONDecodeError as exc:
        output.fail(
            f"failed to parse geometry JSON from {path!r}: {exc}",
            code=output.EXIT_USAGE,
        )


@app.command("get")
def get_labware(
    labware_type: str = typer.Argument(..., help="Catalog labware_type identifier."),
) -> None:
    """Show one labware definition by labware_type. Works on both backends.

    Mirrors ``GET /api/labware-catalog/{labware_type}``. 404 on miss.
    """
    client = get_client()
    entry = client.get_labware(labware_type)
    if output.get_mode().value == "json":
        output.emit_json(entry.model_dump(mode="json"))
        return
    output.emit_kv(
        f"labware {entry.labware_type}",
        [
            ("category", entry.category),
            ("display_name", entry.display_name),
            ("vendor", entry.vendor or ""),
            ("source", entry.source),
            ("plr_class_name", entry.plr_class_name or ""),
        ],
    )


@app.command("add")
def add_labware(
    labware_type: str = typer.Argument(..., help="Unique catalog key."),
    display_name: str = typer.Option(..., "--display-name", help="Human-readable name."),
    category: str = typer.Option(
        ..., "--category",
        help="Labware category: plate, tip_rack, trough, tube, or carrier.",
    ),
    geometry_file: str = typer.Option(
        ..., "--geometry-file",
        help="Path to a JSON file with the labware geometry blob.",
    ),
    vendor: str = typer.Option("", "--vendor"),
    plr_class_name: str = typer.Option("", "--plr-class-name"),
) -> None:
    """Add an operator-custom labware definition. Works on both backends.

    ``source`` is forced to ``operator_custom`` server-side; the CLI
    cannot insert seed rows. 409 on duplicate ``labware_type``.
    """
    geometry = _read_geometry_from_path(geometry_file)
    client = get_client()
    entry = client.add_labware(
        labware_type=labware_type,
        display_name=display_name,
        category=category,
        geometry=geometry,
        vendor=vendor or None,
        plr_class_name=plr_class_name or None,
    )
    if output.get_mode().value == "json":
        output.emit_json(entry.model_dump(mode="json"))
        return
    output.info(
        f"added labware [cyan]{entry.labware_type}[/cyan] "
        f"(category={entry.category}, source={entry.source})",
    )


@app.command("update")
def update_labware(
    labware_type: str = typer.Argument(..., help="Catalog labware_type to update."),
    display_name: str = typer.Option(..., "--display-name"),
    category: str = typer.Option(..., "--category"),
    geometry_file: str = typer.Option(..., "--geometry-file"),
    vendor: str = typer.Option("", "--vendor"),
    plr_class_name: str = typer.Option("", "--plr-class-name"),
) -> None:
    """Update an operator-custom labware definition. Works on both backends.

    Refuses ``plr_seed`` rows with 409. 404 on miss.
    """
    geometry = _read_geometry_from_path(geometry_file)
    client = get_client()
    entry = client.update_labware(
        labware_type,
        display_name=display_name,
        category=category,
        geometry=geometry,
        vendor=vendor or None,
        plr_class_name=plr_class_name or None,
    )
    if output.get_mode().value == "json":
        output.emit_json(entry.model_dump(mode="json"))
        return
    output.info(
        f"updated labware [cyan]{entry.labware_type}[/cyan] "
        f"(category={entry.category})",
    )


@app.command("delete")
def delete_labware(
    labware_type: str = typer.Argument(..., help="Catalog labware_type to delete."),
) -> None:
    """Delete an operator-custom labware definition. Works on both backends.

    Refuses ``plr_seed`` rows with 409. 404 on miss.
    """
    client = get_client()
    client.delete_labware(labware_type)
    output.info(f"deleted labware [cyan]{labware_type}[/cyan]")


@app.command("catalog")
def catalog(
    category: str = typer.Option(
        "", "--category",
        help=(
            "Filter to one labware category: plate, tip_rack, trough, "
            "tube, or carrier. Default lists every row."
        ),
    ),
) -> None:
    """List labware definitions from the catalog. Works on both backends.

    List routes through the deployment-registries catalog facade on both the
    local daemon and a hosted server, so each row
    is a geometry-free ``LabwareCatalogSummary`` (labware_type + display_name +
    category + vendor + source + plr_class_name) regardless of backend; fetch
    geometry per row via ``labware get``.
    """
    client = get_client()
    entries = client.list_labware(category=category or None)
    if output.get_mode().value == "json":
        output.emit_json([e.model_dump(mode="json") for e in entries])
        return
    output.emit_table(
        "Labware catalog",
        ["labware_type", "category", "display_name", "vendor", "source"],
        [
            [
                e.labware_type, e.category, e.display_name,
                e.vendor or "", e.source,
            ]
            for e in entries
        ],
    )


@app.command("where")
def where(
    ident: str = typer.Argument(
        ..., help="Labware id (full or 4+ char prefix) or barcode.",
    ),
) -> None:
    """Show current location for a labware.

    Resolution order: exact id, exact barcode, then id-prefix (>=4 chars
    matching the prefix shown in ``labware list``). On a local miss,
    falls through to the daemon's store-first lookup so a labware that
    survived a runtime rebuild (lives in the labware service but not in
    `system.labwares`) is still reachable. The daemon calls use
    non-bailing variants so a missed id lookup does not leak a 404 to
    stderr before the barcode fallback runs.
    """
    client = get_client()
    catalog = client.labware_list()
    by_id = {entry.id: entry for entry in catalog}
    by_barcode = {entry.barcode: entry for entry in catalog if entry.barcode}
    snap = None
    if ident in by_id:
        snap = by_id[ident]
    elif ident in by_barcode:
        snap = by_barcode[ident]
    else:
        ids = list(by_id)
        prefix_matches = [c for c in ids if c.startswith(ident)]
        if len(prefix_matches) == 1 and len(ident) >= 4:
            snap = by_id[prefix_matches[0]]
        elif len(prefix_matches) > 1 and len(ident) >= 4:
            output.ambiguous("labware", ident, prefix_matches)
        else:
            snap = (
                client.labware_get_by_id_or_none(ident)
                or client.labware_get_by_barcode_or_none(ident)
            )
            if snap is None:
                output.not_found("labware", ident)
    output.emit_json(snap.model_dump(mode="json"))
    if output.get_mode().value == "table":
        output.emit_kv(f"labware {snap.id[:8]}", [
            ("template", snap.template_name),
            ("type", snap.labware_type),
            ("barcode", snap.barcode or ""),
            ("location", _where(snap)),
        ])


@app.command("journey")
def journey(
    labware_id: str = typer.Argument(..., help="Labware id (full or 4+ char prefix)."),
    kind: list[str] = typer.Option(
        [], "--kind",
        help="Filter entries by kind (move/action). Repeat for both. --kind move is the location history.",
    ),
    limit: int | None = typer.Option(
        None, "--limit", min=1,
        help="Show only the newest N entries. Default: every entry.",
    ),
) -> None:
    """Show one labware's moves and the operations done to it, oldest first."""
    client = get_client()
    labware_id = _resolve_labware_id_or_passthrough(client, labware_id)
    payload = client.labware_journey(labware_id, kinds=kind or None)
    if limit is not None:
        payload = payload.model_copy(update={"entries": payload.entries[-limit:]})
    if output.get_mode().value == "json":
        output.emit_json(payload.model_dump(mode="json"))
        return
    entries = payload.entries
    output.emit_table(
        f"Journey ({labware_id[:8]})",
        ["kind", "timestamp", "where_or_op"],
        [
            [
                e.kind,
                str(e.timestamp),
                e.position_id if isinstance(e, JourneyMove) else e.operation,
            ]
            for e in entries
        ],
    )


# --- Operator-override verbs --------------------------------------------


@app.command("edit-location")
def edit_location(
    labware_id: str = typer.Argument(
        ..., help="Labware id (full or 4+ char prefix).",
    ),
    location: str = typer.Argument(...),
    reason: str = typer.Option(
        ..., "--reason", help="Why (audit trail).",
    ),
) -> None:
    """Operator override: mark labware as physically at a new location.

    No transporter is invoked; you are asserting the plate is physically
    there already. A thread carrying this labware throws away the move it
    had planned and plans a fresh one from here, so you may put the plate
    anywhere reachable rather than back where the run left it. Refused only
    if another plate is already there, or a thread has reserved the spot.
    """
    client = get_client()
    resolved = _resolve_labware_id_or_passthrough(client, labware_id)
    client.labware_edit_location(resolved, location, reason)
    output.info(f"labware [cyan]{resolved[:8]}[/cyan] -> location '{location}'")


@app.command("carry")
def carry(
    labware_id: str = typer.Argument(
        ..., help="Labware id (full or 4+ char prefix).",
    ),
    access_type: str | None = typer.Option(None, "--access-type"),
    clearance: float | None = typer.Option(None, "--clearance"),
    z_above: float | None = typer.Option(None, "--z-above"),
    grasp_offset: float | None = typer.Option(None, "--grasp-offset"),
    resource_width: float | None = typer.Option(None, "--resource-width"),
    resource_height: float | None = typer.Option(None, "--resource-height"),
    travel_margin: float | None = typer.Option(None, "--travel-margin"),
    jaw_opening: float | None = typer.Option(None, "--jaw-opening"),
    z_offset: float | None = typer.Option(None, "--z-offset"),
    speed: float | None = typer.Option(
        None, "--speed", help="Percent of full speed.",
    ),
    clear: list[str] = typer.Option(
        [], "--clear", help="Field to hand back to the layers underneath.",
    ),
    reason: str | None = typer.Option(
        None, "--reason", help="Why, for the audit trail.",
    ),
) -> None:
    """Carry THIS one piece of labware with these numbers.

    Wins over the labware type's grip profile, the position's numbers and the
    arm's defaults. For what is true of this object and nothing else, like a lid
    fitted at the sealer. Anything true of the labware TYPE belongs in
    `orca grip-profiles set`, where the next plate of that type inherits it.
    """
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
        cleared = TypeAdapter(list[MoveParameterField]).validate_python(clear)
    except ValidationError as exc:
        output.fail(str(exc), code=output.EXIT_USAGE)
    if not patch.model_dump(exclude_none=True) and not cleared:
        output.fail(
            "name at least one field to set or --clear", code=output.EXIT_USAGE,
        )
    client = get_client()
    resolved = _resolve_labware_id_or_passthrough(client, labware_id)
    carried = client.labware_set_carry_override(resolved, patch, cleared, reason=reason)
    named = carried.model_dump(exclude_none=True)
    if output.get_mode().value == "json":
        output.emit_json({"labware_id": resolved, "carry_override": named})
        return
    output.emit_kv(
        f"carrying {resolved[:8]}",
        [(field, str(value)) for field, value in named.items()],
    )


@app.command("carry-normally")
def carry_normally(
    labware_id: str = typer.Argument(
        ..., help="Labware id (full or 4+ char prefix).",
    ),
    reason: str | None = typer.Option(
        None, "--reason", help="Why, for the audit trail.",
    ),
) -> None:
    """Carry this labware the way anything else of its type is carried."""
    client = get_client()
    resolved = _resolve_labware_id_or_passthrough(client, labware_id)
    client.labware_clear_carry_override(resolved, reason=reason)
    output.info(f"labware [cyan]{resolved[:8]}[/cyan] carries normally again")


@app.command("edit-barcode")
def edit_barcode(
    labware_id: str = typer.Argument(
        ..., help="Labware id (full or 4+ char prefix).",
    ),
    barcode: str | None = typer.Argument(None, help="New barcode (or use --barcode)."),
    barcode_opt: str | None = typer.Option(
        None, "--barcode",
        help="Alternative form of the barcode argument.",
    ),
    reason: str | None = typer.Option(
        None, "--reason", help="Why, for the audit trail.",
    ),
) -> None:
    """Update the barcode associated with a labware instance.

    Either form accepted: ``edit-barcode <id> <barcode>`` or
    ``edit-barcode <id> --barcode <barcode>``. Conflicting values are
    rejected.

    In-flight actions that captured the old barcode will not see the change;
    subsequent barcode lookups use the new value.
    """
    if barcode is not None and barcode_opt is not None and barcode != barcode_opt:
        output.fail(
            "conflicting values: positional barcode and --barcode disagree",
            code=output.EXIT_USAGE,
        )
    chosen = barcode if barcode is not None else barcode_opt
    if chosen is None:
        output.fail(
            "barcode required: pass as positional arg or --barcode <value>",
            code=output.EXIT_USAGE,
        )
    client = get_client()
    resolved = _resolve_labware_id_or_passthrough(client, labware_id)
    client.labware_edit_barcode(resolved, chosen, reason=reason)
    output.info(f"labware [cyan]{resolved[:8]}[/cyan] -> barcode '{chosen}'")


@app.command("reset-location")
def reset_location(
    labware_id: str = typer.Argument(
        ..., help="Labware id (full or 4+ char prefix).",
    ),
    location: str = typer.Argument(...),
    reason: str = typer.Option(
        ..., "--reason", help="Why (audit trail).",
    ),
) -> None:
    """Clear the labware's location history and set a new starting location.

    Only safe before any thread has moved this labware; at runtime this
    corrupts the owning thread's location assumptions.
    """
    client = get_client()
    resolved = _resolve_labware_id_or_passthrough(client, labware_id)
    client.labware_reset_location(resolved, location, reason)
    output.info(f"labware [cyan]{resolved[:8]}[/cyan] reset to '{location}'")


@app.command("register")
def register(
    template: str = typer.Argument(
        "", help="Labware template name, for labware the deployment package declares.",
    ),
    labware_type: str = typer.Option(
        "", "--labware-type",
        help="Catalog labware type, for labware it does not declare. "
             "Derives an ad-hoc template.",
    ),
    barcode: str = typer.Option("", "--barcode"),
    location: str = typer.Option("", "--location"),
    reason: str | None = typer.Option(
        None, "--reason", help="Why, for the audit trail.",
    ),
) -> None:
    """Register a new labware instance mid-run.

    Name it either way: a template argument for labware the deployment package
    declares, or --labware-type for a catalog definition it does not. Optionally
    pins a barcode and places the fresh instance at the given location, which
    may be any deck site, device site, storage position or mover gripper.
    """
    if (template == "") == (labware_type == ""):
        raise typer.BadParameter(
            "give either a template name or --labware-type, not both and not neither.",
        )
    client = get_client()
    snap = client.labware_register(
        template_name=template or None,
        labware_type=labware_type or None,
        barcode=barcode or None,
        location=location or None,
        reason=reason,
    )
    if output.get_mode().value == "json":
        output.emit_json(snap.model_dump(mode="json"))
        return
    output.info(
        f"registered labware [cyan]{snap.id[:8]}[/cyan] "
        f"(template={snap.template_name}"
        + (f", barcode={snap.barcode}" if snap.barcode else "")
        + (f", location={snap.current_location}" if snap.current_location else "")
        + ")"
    )



@app.command("check-run")
def check_run(
    execution_id: str = typer.Argument(..., help="The check run's execution id."),
) -> None:
    """Show where a check run left each labware, and its volumes, from the run's own store.

    A check run works on a copy of the record, so this is never the real deck.
    """
    answer = get_client().check_run_labware(execution_id)
    if output.get_mode().value == "json":
        output.emit_json(answer.model_dump(mode="json"))
        return
    output.emit_table(
        f"Check run {execution_id[:8]}: labware",
        ["id", "template", "location", "wells_known", "provenance"],
        [
            [lw.id[:8], lw.template_name, lw.location, str(len(lw.well_volumes)), lw.provenance.value]
            for lw in answer.labware
        ],
    )


@app.command("set-volume")
def set_volume(
    labware_id: str = typer.Argument(
        ..., help="Labware id (full or 4+ char prefix).",
    ),
    well: list[str] = typer.Option(
        ..., "--well",
        help="WELL=VOLUME pair in uL, e.g. --well A1=100. Repeat per well, or separate pairs with commas.",
    ),
    reason: str = typer.Option(
        ..., "--reason", help="Why (audit trail).",
    ),
) -> None:
    """Operator override: set absolute per-well volumes (uL).

    You are asserting the wells physically hold these amounts. The value
    overrides tracked volume and seeds the driver tracker if the labware is
    on-deck. Overfill beyond a well's capacity is rejected.
    """
    volumes: dict[str, float] = {}
    for pair in _each_item(well):
        if "=" not in pair:
            output.fail(
                f"invalid --well {pair!r}: expected WELL=VOLUME (e.g. A1=100)",
                code=output.EXIT_USAGE,
            )
        well_id, _, raw = pair.partition("=")
        try:
            volumes[well_id] = float(raw)
        except ValueError:
            output.fail(
                f"invalid volume in --well {pair!r}: {raw!r} is not a number",
                code=output.EXIT_USAGE,
            )
    client = get_client()
    resolved = _resolve_labware_id_or_passthrough(client, labware_id)
    client.labware_set_well_volumes(resolved, volumes, reason)
    output.info(
        f"labware [cyan]{resolved[:8]}[/cyan] volumes set: "
        + ", ".join(f"{w}={v}" for w, v in volumes.items())
    )


@app.command("contents")
def contents(
    labware_id: str = typer.Argument(
        ..., help="Labware id (full or 4+ char prefix).",
    ),
) -> None:
    """What this labware holds. The answer, and where it came from.

    `provenance` is `unknown` when nobody has ever said what it holds, which is
    not the same as empty; `stale` when a restart, a reconnect or an
    error pause went by unobserved and nobody has looked since. The layer table
    shows what the other views say, so there is no need to go and compare them.
    """
    client = get_client()
    resolved_id = _resolve_labware_id_or_passthrough(client, labware_id)
    answer = client.labware_resolve_contents(resolved_id)
    if output.get_mode().value == "json":
        output.emit_json(answer.model_dump(mode="json"))
        return
    headline = f"{answer.labware_name}: "
    if answer.tip_count is not None:
        headline += f"{answer.tip_count} tips"
    elif answer.volumes:
        headline += f"{len(answer.volumes)} wells with volume"
    else:
        headline += "nothing tracked"
    headline += f" [{answer.provenance.value}, from {answer.source.value}]"
    if answer.provenance is Provenance.STALE:
        headline += " -- worth a look"
    output.info(headline)
    output.emit_table(
        "Layers",
        ["layer", "tips", "agrees", "what it is"],
        [
            [
                layer.layer,
                "-" if layer.tip_count is None else str(layer.tip_count),
                "yes" if layer.agrees else "NO",
                layer.note,
            ]
            for layer in answer.layers
        ],
    )


@app.command("mark-tips-used")
def mark_tips_used(
    labware_id: str = typer.Argument(
        ..., help="Tip rack id (full or 4+ char prefix).",
    ),
    position: list[str] = typer.Option(
        ..., "--position", "-p",
        help="Position that is actually empty. Repeat for several, or separate them with commas.",
    ),
    reason: str = typer.Option(..., "--reason", help="Why (audited)."),
) -> None:
    """Mark positions as empty and move the rack's next pick past them.

    For when a pick found air or a hand took a column: every other position
    keeps its tracked state, so there is no need to restate the whole rack.
    """
    client = get_client()
    resolved = _resolve_labware_id_or_passthrough(client, labware_id)
    answer = client.labware_mark_tips_used(resolved, _each_item(position), reason)
    output.info(
        f"rack [cyan]{resolved[:8]}[/cyan] now holds "
        f"{len(answer.tip_positions_present)} tips"
    )


@app.command("set-tips")
def set_tips(
    labware_id: str = typer.Argument(
        ..., help="Tip rack id (full or 4+ char prefix).",
    ),
    position: list[str] = typer.Option(
        [], "--position",
        help="Position that holds a tip, e.g. --position A1. Repeat per "
             "position, or separate them with commas; none asserts an empty rack.",
    ),
    reason: str = typer.Option(
        ..., "--reason", help="Why (audit trail).",
    ),
) -> None:
    """Operator override: set the rack's absolute tip layout.

    You are asserting tips physically sit at exactly these positions. The
    value overrides tracked tip state and seeds the driver rack if the labware
    is on-deck; the rack stops reading stale.
    """
    client = get_client()
    resolved = _resolve_labware_id_or_passthrough(client, labware_id)
    positions = _each_item(position)
    client.labware_set_tip_state(resolved, positions, reason)
    output.info(
        f"labware [cyan]{resolved[:8]}[/cyan] tip layout set: "
        + (", ".join(positions) if positions else "(empty rack)")
    )


@app.command("confirm-tips")
def confirm_tips(
    labware_id: str = typer.Argument(
        ..., help="Tip rack id (full or 4+ char prefix).",
    ),
    reason: str | None = typer.Option(
        None, "--reason", help="Optional note (audit trail).",
    ),
) -> None:
    """Confirm the rack physically matches its tracked tip layout.

    Records the layout as an operator-asserted baseline, which is what clears
    the stale mark a restart puts on the rack. If the rack does NOT match,
    use `labware set-tips` instead. Refused when an unfinished or an aborted
    action has left the record not describing the rack: there is nothing there
    worth agreeing with, and `set-tips` is the verb.
    """
    client = get_client()
    resolved = _resolve_labware_id_or_passthrough(client, labware_id)
    client.labware_confirm_tip_state(resolved, reason)
    output.info(f"labware [cyan]{resolved[:8]}[/cyan] tip state confirmed")


@app.command("confirm-volumes")
def confirm_volumes(
    labware_id: str = typer.Argument(
        ..., help="Labware id (full or 4+ char prefix).",
    ),
    reason: str | None = typer.Option(
        None, "--reason", help="Optional note (audit trail).",
    ),
) -> None:
    """Confirm the labware physically matches its tracked well volumes.

    Records the volumes as an operator-asserted baseline, which is what clears
    the stale mark a restart puts on the labware. If they do NOT match, use
    `labware set-volume` instead. Refused when an unfinished or an aborted
    action has left the record not describing the labware.
    """
    client = get_client()
    resolved = _resolve_labware_id_or_passthrough(client, labware_id)
    client.labware_confirm_well_volumes(resolved, reason)
    output.info(f"labware [cyan]{resolved[:8]}[/cyan] well volumes confirmed")


@app.command("clear-submission")
def clear_submission(
    submission_id: str = typer.Argument(..., help="Submission UUID to clear labware for."),
    force: bool = typer.Option(False, "--force", help="Bypass the active-execution refusal."),
    reason: str | None = typer.Option(
        None, "--reason", help="Why, for the audit trail.",
    ),
) -> None:
    """Take a submission's labware off the platform, leaving deck residents.

    The usual end-of-run tidy-up: the run's plates are closed out and
    anything a thread declared LEAVE_IN_PLACE stays, because a resident's
    identity is what carries its volume and remaining tips to the next run.
    """
    client = get_client()
    result = client.labware_clear_submission(submission_id, force=force, reason=reason)
    if output.get_mode().value == "json":
        output.emit_json(result)
        return
    cleared = result.get("cleared", [])
    preserved = result.get("preserved_reuse_bound", [])
    output.info(
        f"cleared {len(cleared)} labware from submission {submission_id[:8]}",
    )
    if preserved:
        output.info(
            f"preserved {len(preserved)} reuse-bound labware: "
            f"{', '.join(preserved)}",
        )


@app.command("discharge")
def discharge(
    labware_id: str = typer.Argument(..., help="Labware UUID to remove."),
    force: bool = typer.Option(
        False, "--force",
        help="Bypass the active-thread refusal for EVERY thread. Not needed to "
             "release a manual-remove park; that thread is already exempt.",
    ),
    reason: str | None = typer.Option(
        None, "--reason", help="Why, for the audit trail.",
    ),
) -> None:
    """Remove ONE labware from the runtime."""
    client = get_client()
    result = client.labware_discharge(labware_id, force=force, reason=reason)
    if output.get_mode().value == "json":
        output.emit_json(result)
        return
    output.info(f"discharged labware {labware_id}")


@app.command("clear-all")
def clear_all(
    force: bool = typer.Option(False, "--force", help="Bypass the active-execution refusal."),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Skip the interactive confirmation prompt (required on non-TTY).",
    ),
    reason: str | None = typer.Option(
        None, "--reason", help="Why, for the audit trail.",
    ),
) -> None:
    """Panic button: clear EVERY labware in the runtime, deck residents included.

    Prompts for confirmation on an interactive TTY since clear-all is
    catastrophic on misfire. On a non-TTY (CI, scripts, piped stdin)
    the operator MUST pass `--yes` explicitly so a typo doesn't wipe
    a live deck.
    """
    _confirm_clearing_every_labware("clear-all", yes)
    client = get_client()
    result = client.labware_clear_all(force=force, reason=reason)
    if output.get_mode().value == "json":
        output.emit_json(result)
        return
    output.info(f"cleared {len(result['cleared_labware_ids'])} labware")


def _confirm_clearing_every_labware(verb: str, yes: bool) -> None:
    """Prompt on a TTY; refuse without --yes on a non-TTY, so a typo never wipes a live deck."""
    if yes:
        return
    if not _stdin_is_tty():
        output.info(
            f"{verb} refuses without --yes on a non-TTY: this would "
            "wipe EVERY labware in the runtime."
        )
        raise typer.Exit(code=2)
    confirmed = typer.confirm(
        "Clear EVERY labware in the runtime? This is the panic button.",
        default=False,
    )
    if not confirmed:
        output.info(f"{verb} aborted by operator")
        raise typer.Exit(code=1)


def _run_item(text: str) -> RunLabwareItem:
    name, sep, volume = text.partition("=")
    if not sep:
        return RunLabwareItem(thread_template_name=name)
    try:
        return RunLabwareItem(thread_template_name=name, volume_ul=float(volume))
    except ValueError as exc:
        raise typer.BadParameter(f"{text!r}: give a volume in uL, zero or more, after '='") from exc


@app.command("load-run")
def load_run(
    workflow_name: str = typer.Argument(..., help="The workflow whose labware is being put down."),
    items: list[str] = typer.Option(
        [], "--item",
        help="Load only this thread, as NAME, or NAME=UL to set uL in every well. Repeat for each. "
             "Omitted, every thread the workflow puts down before a run is loaded.",
    ),
    clear_all_first: bool = typer.Option(
        False, "--clear-all-first",
        help="Clear every labware record first, reused labware included.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the --clear-all-first confirmation prompt (required on non-TTY).",
    ),
) -> None:
    """Record a run's labware as loaded: keep reused labware in place, otherwise take off what is there, record it, set its volume."""
    if clear_all_first:
        _confirm_clearing_every_labware("load-run --clear-all-first", yes)
    request = LoadRunLabwareRequest(
        workflow_name=workflow_name, items=[_run_item(i) for i in items], clear_all_first=clear_all_first,
    )
    result = get_client().labware_load_run(request)
    if output.get_mode().value == "json":
        output.emit_json(result.model_dump(mode="json"))
        return
    if result.cleared:
        output.info(f"cleared {len(result.cleared)} labware")
    for loaded in result.loaded:
        output.info(f"{loaded.thread_template_name}: loaded as {loaded.labware_id}")
    for kept in result.kept:
        output.info(f"{kept.thread_template_name}: already there, kept as {kept.labware_id}")
    for failed in result.not_loaded:
        output.info(f"{failed.thread_template_name}: not loaded: {failed.message}")
    if result.not_loaded:
        raise typer.Exit(code=1)

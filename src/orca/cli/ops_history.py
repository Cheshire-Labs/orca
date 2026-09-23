"""`orca ops-history ...` CLI: per-execution archive read + cross-execution search.

Works against both backends: the local daemon binds the ops-history
Operations at ``POST /operations/list-ops-history`` and
``POST /operations/search-ops-history``; a cloud deployment exposes the same surface
under ``/api/operations/...`` (and the matching MCP tools
``ops_history_get`` / ``ops_history_search``).
"""

import typer

from orca.cli import output, resolve
from orca.cli.backend import get_client
from orca.state.records import TrackingRecord


def _affected_ids(record: TrackingRecord) -> str:
    """Distinct canonical labware UUIDs across a record's ops.

    These are the ids ``orca labware journey`` / ``labware history`` accept;
    the display names alone are not resolvable.
    """
    ids: list[str] = []
    for op in record.operations:
        for lw_id in op.affected_labware_ids:
            if lw_id not in ids:
                ids.append(lw_id)
    return ", ".join(ids)


app = typer.Typer(
    help="OpsHistory archive: per-execution list + cross-execution search.",
    no_args_is_help=True,
)


@app.command("get")
def ops_history_get(
    execution_id: str = typer.Argument(
        ..., help="Execution id (full UUID or 4+ char prefix).",
    ),
) -> None:
    """List every TrackingRecord archived for one execution."""
    client = get_client()
    ids = [r.id for r in client.list_executions()]
    resolved = resolve.resolve_execution_id(execution_id, ids)
    payload = client.ops_history_get(resolved)
    if output.get_mode().value == "json":
        output.emit_json(payload.model_dump(mode="json"))
        return
    records = payload.records
    output.emit_table(
        f"OpsHistory ({resolved[:8]}, {len(records)} records)",
        ["action_id", "thread_id", "method_id", "source", "timestamp", "ops", "affected_labware_ids"],
        [
            [
                r.action_id[:12],
                r.thread_id[:12],
                r.method_id[:12] if r.method_id is not None else None,
                r.source.value,
                str(r.timestamp),
                str(len(r.operations)),
                _affected_ids(r),
            ]
            for r in records
        ],
    )


@app.command("search")
def ops_history_search(
    execution_id: str | None = typer.Option(
        None, "--execution-id", help="Filter by owning execution id.",
    ),
    action_id: str | None = typer.Option(
        None, "--action-id", help="Filter by action id.",
    ),
    thread_id: str | None = typer.Option(
        None, "--thread-id", help="Filter by thread id.",
    ),
    method_id: str | None = typer.Option(
        None, "--method-id", help="Filter by method id.",
    ),
    source: str | None = typer.Option(
        None, "--source",
        help="Filter by tracking source (observed / declared / driver_observed).",
    ),
    operation: str | None = typer.Option(
        None, "--operation",
        help="Filter to records that contain at least one op of this DeviceOperation value.",
    ),
    labware_name: str | None = typer.Option(
        None, "--labware",
        help="Filter to records that touched this labware name.",
    ),
    device_name: str | None = typer.Option(
        None, "--device",
        help="Filter to records that touched this device name.",
    ),
) -> None:
    """Cross-execution search over OpsHistory. All filters AND-combine."""
    client = get_client()
    payload = client.ops_history_search(
        execution_id=execution_id,
        action_id=action_id,
        thread_id=thread_id,
        method_id=method_id,
        source=source,
        operation=operation,
        labware_name=labware_name,
        device_name=device_name,
    )
    if output.get_mode().value == "json":
        output.emit_json(payload.model_dump(mode="json"))
        return
    records = payload.records
    output.emit_table(
        f"OpsHistory search ({len(records)} hits)",
        ["execution", "action_id", "thread_id", "source", "timestamp", "ops", "affected_labware_ids"],
        [
            [
                r.execution_id[:8],
                r.action_id[:12],
                r.thread_id[:12],
                r.source.value,
                str(r.timestamp),
                str(len(r.operations)),
                _affected_ids(r),
            ]
            for r in records
        ],
    )

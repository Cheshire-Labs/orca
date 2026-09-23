"""`orca audit` -- inspect the @dangerous audit trail.

Reads the active backend's in-memory ring buffer via GET /audit (local
daemon) or GET /api/audit (cloud). The durable mirror lives at
log_dir/orca_audit.log (rotating handler). Use this command for live
operator inspection without parsing the file.
"""

import datetime as _dt

import typer

from orca.cli import output
from orca.cli.backend import get_client


app = typer.Typer(help="Inspect the @dangerous audit trail.", no_args_is_help=True)


@app.command("list")
def audit_list(
    action_name: str = typer.Option(
        "", "--action-name",
        help="Filter to one action (e.g. 'thread.skip_method').",
    ),
    limit: int = typer.Option(
        50, "--limit", help="Max number of most-recent entries to return.",
    ),
) -> None:
    """List recent confirmed @dangerous operations."""
    client = get_client()
    entries = client.audit_list(
        action_name=action_name or None, limit=limit,
    )

    if output.get_mode().value == "json":
        output.emit_json([e.model_dump(mode="json") for e in entries])
        return

    if not entries:
        output.info("(no audit entries)")
        return

    rows = []
    for e in entries:
        ts = _dt.datetime.fromtimestamp(e.timestamp).isoformat(timespec="seconds")
        reason_short = (e.reason or "")[:40]
        rows.append((ts, e.action_name, e.danger_level, reason_short))
    output.emit_table(
        title=None,
        columns=["timestamp", "action", "level", "reason"],
        rows=rows,
    )

"""`orca events` -- read the events the server stored, via GET /api/events."""

import datetime as _dt

import typer

from orca.cli import output
from orca.cli.backend import get_client


app = typer.Typer(help="Read stored runtime events.", no_args_is_help=True)


@app.command("list")
def events_list(
    since: int = typer.Option(0, "--since", help="Cursor to resume after."),
    execution_id: str | None = typer.Option(None, "--execution", help="Only this run's events."),
    limit: int = typer.Option(500, "--limit", help="Max number of events."),
    tail: bool = typer.Option(False, "--tail", help="The newest LIMIT events instead of reading forward from --since."),
) -> None:
    """List stored events, oldest first, each with the cursor to resume after it."""
    events = get_client().events_list(since=since, execution_id=execution_id, limit=limit, tail=tail)

    if output.get_mode().value == "json":
        output.emit_json([e.model_dump(mode="json") for e in events])
        return
    if not events:
        output.info("(no events)")
        return
    output.emit_table(
        title=None,
        columns=["cursor", "time", "event", "entity", "status", "execution"],
        rows=[
            (
                str(e.cursor),
                _dt.datetime.fromtimestamp(e.timestamp).isoformat(timespec="seconds"),
                e.event_name, f"{e.entity_type}:{e.entity_id}", e.status, e.execution_id,
            )
            for e in events
        ],
    )

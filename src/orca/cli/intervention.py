"""`orca intervention next`: wait for the next event that needs a person."""

import typer

from orca.cli import output
from orca.cli.backend import get_client
from orca.operations.interventions import DEFAULT_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS


app = typer.Typer(help="Wait for operator interventions.", no_args_is_help=True)


@app.command("next")
def next_intervention(
    after: int | None = typer.Option(
        None, "--after",
        help="The cursor the last call printed. Omit to wait only for interventions from now.",
    ),
    timeout: float = typer.Option(
        DEFAULT_TIMEOUT_SECONDS, "--timeout", min=0.001, max=MAX_TIMEOUT_SECONDS,
        help="Seconds to wait before giving up.",
    ),
) -> None:
    """Wait for the next manual place, manual remove, manual step, incident or device fault."""
    result = get_client().intervention_next(after_seq=after, timeout_seconds=timeout)
    if output.get_mode().value == "json":
        output.emit_json(result.model_dump(mode="json"))
        return
    found = result.intervention
    if found is None:
        output.info(f"no intervention within {timeout:g}s (next cursor {result.next_cursor})")
        return
    execution = found.execution_id[:8] if found.execution_id is not None else ""
    detail = found.instruction or found.message or found.target_location or ""
    output.emit_table(
        title=None,
        columns=["cursor", "kind", "execution", "detail"],
        rows=[(str(found.seq), found.kind, execution, detail)],
    )

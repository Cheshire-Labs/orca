"""`orca reservation ...` noun sub-app.

Reservations are per-execution. `list` reads GET /executions/{id}/reservations
and `cancel` hits DELETE /executions/{id}/reservations/{rsv}.
"""

import typer

from orca.cli import output, resolve
from orca.cli.app import STATE
from orca.cli.backend import get_client


app = typer.Typer(help="Inspect and cancel resource reservations.", no_args_is_help=True)


@app.command("list")
def list_reservations(
    execution: str | None = typer.Option(
        None, "--execution",
        help="Execution id or prefix. Omit to default to the latest "
             "execution (with a one-line hint). Use --all to span all.",
    ),
    all_executions: bool = typer.Option(
        False, "--all",
        help="Span every tracked execution.",
    ),
) -> None:
    """List reservations held by an execution (latest by default; --all spans)."""
    client = get_client()
    if all_executions:
        if execution is not None:
            output.fail(
                "--all is mutually exclusive with --execution",
                code=output.EXIT_USAGE,
            )
        rows = client.reservations_list_all()
        if output.get_mode().value == "json":
            output.emit_json([r.model_dump(mode="json") for r in rows])
            return
        output.emit_table(
            "Reservations (all executions)",
            ["execution", "id", "thread_id", "location"],
            [
                [
                    r.execution_id[:8] if r.execution_id is not None else None,
                    r.reservation_id[:8],
                    r.thread_id[:8] if r.thread_id is not None else None,
                    r.position_id,
                ]
                for r in rows
            ],
        )
        return

    ids = [r.id for r in client.list_executions()]
    if execution is None:
        last_eid = resolve.get_last_execution_id()
        if last_eid is None:
            output.fail(
                "no 'last' execution recorded in this CLI session -- "
                "pass --execution <id> or --all",
                code=output.EXIT_USAGE,
            )
        # Resolve through the standard "last" path so the stale-recorded
        # case produces the same `no longer known to runtime` message
        # rather than a misleading "using latest X" hint followed by a
        # cryptic not-found.
        eid = resolve.resolve_execution_id("last", ids)
        output.info(
            f"using latest execution {eid[:8]} "
            f"(pass --execution or --all to override)",
        )
    else:
        eid = resolve.resolve_execution_id(execution, ids)

    snaps = client.list_reservations(eid)
    if output.get_mode().value == "json":
        output.emit_json([s.model_dump(mode="json") for s in snaps])
        return
    output.emit_table(
        f"Reservations ({eid[:8]})",
        ["id", "thread_id", "location", "holding"],
        [
            [
                s.reservation_id[:8],
                s.thread_id[:8] if s.thread_id is not None else None,
                s.position_id,
                _what_the_hold_is_for(
                    s.labware_name, s.awaiting_operator, s.arriving,
                ),
            ]
            for s in snaps
        ],
    )


def _what_the_hold_is_for(
    labware_name: str | None, awaiting_operator: bool, arriving: bool,
) -> str | None:
    """A held position reads the same however it is held, and the ways mean
    different things to whoever wants the position next."""
    if labware_name is None:
        return None
    if awaiting_operator:
        return f"{labware_name} (awaiting placement)"
    if arriving:
        return f"{labware_name} (inbound)"
    return labware_name


@app.command("cancel")
def cancel(
    reservation_id: str = typer.Argument(...),
    execution: str = typer.Option(..., "--execution"),
    reason: str = typer.Option(
        ...,
        "--reason",
        help="Operator justification. Required: this action is CRITICAL "
             "and is written to the audit log.",
    ),
) -> None:
    """Cancel a reservation held by an execution's thread."""
    client = get_client()
    ids = [r.id for r in client.list_executions()]
    eid = resolve.resolve_execution_id(execution, ids)
    if not STATE.force:
        if not typer.confirm(
            f"Cancel reservation {reservation_id[:8]} in {eid[:8]}?",
            default=False,
        ):
            output.confirmation_denied()
    client.reservation_cancel(eid, reservation_id, reason=reason)
    output.info(f"cancelled reservation {reservation_id[:8]}")

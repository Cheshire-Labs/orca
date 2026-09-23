"""`orca manual-step ...` noun sub-app.

Operator surface for `ctx.manual_step(instruction)` pauses. `list`
shows emitted-but-unconfirmed steps (global by default, or scoped to one
execution); `confirm` fires the confirmation that releases a parked
thread. Confirm is SAFE -- no prompt, no reason, fires immediately.
Both verbs work against the local daemon and a cloud deployment.
"""

import typer

from orca.cli import output, resolve
from orca.cli.backend import active_backend, cloud_client, local_client


app = typer.Typer(
    help="List and confirm operator manual steps.", no_args_is_help=True,
)


@app.command("list")
def list_pending(
    execution: str | None = typer.Option(
        None, "--execution",
        help="Execution id or prefix to scope to. Omit for all executions.",
    ),
    all_executions: bool = typer.Option(
        False, "--all",
        help="Span every tracked execution (the default when --execution "
             "is omitted; accepted for explicitness).",
    ),
) -> None:
    """List emitted-but-unconfirmed operator manual steps.

    Global by default (all executions). Pass --execution to scope to one.
    """
    if active_backend() == "cloud":
        client = cloud_client()
    else:
        client = local_client()
    if execution is not None and all_executions:
        output.fail(
            "--all is mutually exclusive with --execution",
            code=output.EXIT_USAGE,
        )
    eid: str | None = None
    if execution is not None:
        ids = [r.id for r in client.list_executions()]
        eid = resolve.resolve_execution_id(execution, ids)
    rows = client.manual_steps_list(eid)
    if output.get_mode().value == "json":
        output.emit_json([r.model_dump(mode="json") for r in rows])
        return
    title = "Manual steps" if eid is None else f"Manual steps ({eid[:8]})"
    output.emit_table(
        title,
        ["execution", "step_id", "instruction", "emitted_at"],
        [
            [
                r.execution_id[:8],
                r.step_id,
                r.instruction,
                r.emitted_at.isoformat(),
            ]
            for r in rows
        ],
    )


@app.command("confirm")
def confirm(
    step_id: str = typer.Argument(..., help="The manual step's step_id."),
    execution: str = typer.Option(
        ..., "--execution", help="Execution id or prefix the step belongs to.",
    ),
) -> None:
    """Confirm a pending manual step, releasing the parked thread.

    SAFE: fires immediately, no confirmation prompt.
    """
    if active_backend() == "cloud":
        client = cloud_client()
    else:
        client = local_client()
    ids = [r.id for r in client.list_executions()]
    eid = resolve.resolve_execution_id(execution, ids)
    client.manual_step_confirm(eid, step_id)
    output.info(f"confirmed manual step {step_id} in {eid[:8]}")

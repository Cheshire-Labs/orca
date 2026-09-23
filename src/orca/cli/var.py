"""`orca var ...` noun sub-app.

Dispatches to the active backend via `get_client()` (local daemon or cloud
cloud). Profile loading moved to `orca run <workflow> --profile <path>` (see
cli/app.py): profiles are applied at submission time so the new execution's
variable partition is populated before any thread reads it.
"""

import typer

from orca.cli import output, resolve
from orca.cli.app import STATE
from orca.cli.backend import get_client
from orca.variables.errors import OptionValue


app = typer.Typer(help="Read and write execution / global variables.", no_args_is_help=True)


def _coerce(value: str) -> OptionValue:
    """CLI takes strings; coerce to bool/int/float when possible."""
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


@app.command("get")
def get(
    name: str = typer.Argument(...),
    execution: str = typer.Option(..., "--execution", help="Execution id or prefix."),
    submission: str | None = typer.Option(
        None, "--submission",
        help="Resolve as this submission's threads do. Omit for the value "
             "submissions without an override of their own resolve.",
    ),
) -> None:
    """Get a single variable's value for an execution."""
    client = get_client()
    ids = [r.id for r in client.list_executions()]
    eid = resolve.resolve_execution_id(execution, ids)
    result = client.variables_get(eid, name, submission)
    output.emit_json(result.model_dump(mode="json"))
    if output.get_mode().value == "table":
        rows: list[tuple[str, str | None]] = [
            ("value", str(result.value)), ("source", result.source.value),
        ]
        output.emit_kv(f"var {name}", rows)
        _warn_if_shadowed(name, eid, result.shadowed_by)


@app.command("explain")
def explain(
    name: str = typer.Argument(...),
    execution: str = typer.Option(..., "--execution", help="Execution id or prefix."),
) -> None:
    """Show what each submission of an execution resolves for a variable."""
    client = get_client()
    ids = [r.id for r in client.list_executions()]
    eid = resolve.resolve_execution_id(execution, ids)
    result = client.variables_resolution(eid, name)
    if output.get_mode().value == "json":
        output.emit_json(result.model_dump(mode="json"))
        return
    source = "none" if result.source is None else result.source.value
    output.emit_kv(
        f"var {name} ({eid[:8]})",
        [("value", str(result.value)), ("source", source)],
    )
    output.emit_table(
        "Submission overrides",
        ["submission", "value"],
        [[o.submission_id[:8], str(o.value)] for o in result.overrides],
    )


def _warn_if_shadowed(name: str, execution_id: str, shadowed_by: list[str]) -> None:
    """Say when submission values outrank what the operator just read or wrote."""
    if not shadowed_by:
        return
    ids = ", ".join(s[:8] for s in shadowed_by)
    output.info(
        f"{name} is overridden by submission(s) {ids}, whose threads resolve "
        f"their own value. See them with "
        f"'orca var explain {name} --execution {execution_id}', and clear one "
        f"with 'orca var unset {name} --execution {execution_id} "
        f"--submission <id>'."
    )


@app.command("list")
def list_vars(
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
    """List variables (one execution by default; --all spans every exec)."""
    client = get_client()
    if all_executions:
        if execution is not None:
            output.fail(
                "--all is mutually exclusive with --execution",
                code=output.EXIT_USAGE,
            )
        rows = client.variables_list_all()
        if output.get_mode().value == "json":
            output.emit_json([r.model_dump(mode="json") for r in rows])
            return
        output.emit_table(
            "Variables (all executions)",
            ["execution", "name", "value"],
            [
                [row.execution_id[:8], row.name, str(row.value)]
                for row in rows
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
        eid = resolve.resolve_execution_id("last", ids)
        output.info(
            f"using latest execution {eid[:8]} "
            f"(pass --execution or --all to override)",
        )
    else:
        eid = resolve.resolve_execution_id(execution, ids)

    values = client.variables_list(eid)
    if output.get_mode().value == "json":
        output.emit_json(values)
        return
    output.emit_table(
        f"Variables ({eid[:8]})",
        ["name", "value"],
        [[k, str(v)] for k, v in sorted(values.items())],
    )


@app.command("set")
def set_var(
    name: str = typer.Argument(...),
    value: str = typer.Argument(...),
    execution: str = typer.Option(..., "--execution", help="Execution id or prefix."),
    submission: str | None = typer.Option(
        None, "--submission",
        help="Write the submission's own layer, which outranks the execution "
             "scope. Required to change what an already-submitted thread resolves.",
    ),
) -> None:
    """Set a variable in one execution's (or one submission's) scope."""
    client = get_client()
    ids = [r.id for r in client.list_executions()]
    eid = resolve.resolve_execution_id(execution, ids)
    coerced = _coerce(value)
    scope = f"execution {eid[:8]}" if submission is None else f"submission {submission[:8]}"
    if not STATE.force:
        if not typer.confirm(
            f"Set {name!r} = {coerced!r} in {scope}?",
            default=False,
        ):
            output.confirmation_denied()
    if submission is not None:
        client.variables_set_submission(eid, submission, name, coerced)
        output.info(f"{name} = {coerced}  ({scope})")
        return
    result = client.variables_set(eid, name, coerced)
    output.info(f"{name} = {coerced}  ({scope})")
    _warn_if_shadowed(name, eid, result.shadowed_by)


@app.command("set-global")
def set_global(
    name: str = typer.Argument(...),
    value: str = typer.Argument(...),
) -> None:
    """Set a variable globally (affects all executions without local override)."""
    client = get_client()
    coerced = _coerce(value)
    if not STATE.force:
        if not typer.confirm(
            f"Set global {name!r} = {coerced!r}?", default=False,
        ):
            output.confirmation_denied()
    client.variables_set_global(name, coerced)
    output.info(f"global {name} = {coerced}")


@app.command("unset")
def unset(
    name: str = typer.Argument(...),
    execution: str = typer.Option(..., "--execution", help="Execution id or prefix."),
    submission: str | None = typer.Option(
        None, "--submission",
        help="Clear this submission's own override so its threads fall through "
             "to the execution value.",
    ),
) -> None:
    """Remove a variable override (falls back to the layers below it)."""
    client = get_client()
    ids = [r.id for r in client.list_executions()]
    eid = resolve.resolve_execution_id(execution, ids)
    scope = f"execution {eid[:8]}" if submission is None else f"submission {submission[:8]}"
    if not STATE.force:
        if not typer.confirm(
            f"Unset {name!r} in {scope}?", default=False,
        ):
            output.confirmation_denied()
    if submission is None:
        client.variables_unset(eid, name)
    else:
        client.variables_unset_submission(eid, submission, name)
    output.info(f"unset {name} ({scope})")



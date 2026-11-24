"""`orca runtime` CLI verbs: `status` and `reload`.

`status` reports whether the runtime is built and, if not, the last build or
rebuild failure type, message and recovery hint. `reload` rebuilds the runtime
from the package files on disk.
"""

import typer

from orca.cli import output
from orca.cli.backend import get_client


app = typer.Typer(
    help="Runtime status and reload.",
    no_args_is_help=True,
)


@app.command("status")
def runtime_status() -> None:
    """Diagnostic snapshot of the runtime lifecycle. Works against both backends.

    Call after any operation that returns RUNTIME_NOT_READY. The
    response carries whether the runtime is built and, if not, the
    last build/rebuild failure (type, message, and a recovery hint
    derived from the exception class + message). On the local daemon the
    failure is the last mount's.
    """
    client = get_client()
    payload = client.runtime_status()
    if output.get_mode().value == "json":
        output.emit_json(payload.model_dump(mode="json"))
        return
    if payload.built:
        typer.echo("runtime built")
        return
    typer.echo("runtime NOT built")
    if payload.last_build_error is not None:
        err = payload.last_build_error
        typer.echo(f"  last error type: {err.get('type', '(unknown)')}")
        typer.echo(f"  message:         {err.get('message', '')}")
        typer.echo(f"  hint:            {err.get('hint', '')}")


@app.command("reload")
def runtime_reload(
    reason: str = typer.Option("", "--reason", help="Why, for the audit row."),
) -> None:
    """Shut the runtime down and build it again from the package files on disk.

    Refused while a run or a check run is active. Works against both backends.
    """
    payload = get_client().runtime_reload(reason)
    if output.get_mode().value == "json":
        output.emit_json(payload.model_dump(mode="json"))
        return
    output.info(f"reloaded; {len(payload.registered)} workflow(s) registered: {', '.join(payload.registered)}")

"""`orca runtime status` CLI verb.

Reports whether the runtime is built and, if not, the last build or
rebuild failure type, message and recovery hint.
"""

import typer

from orca.cli import output
from orca.cli.backend import get_client


app = typer.Typer(
    help="Runtime status.",
    no_args_is_help=True,
)


@app.command("status")
def runtime_status() -> None:
    """Diagnostic snapshot of the runtime lifecycle. Works against both backends.

    Call after any operation that returns RUNTIME_NOT_READY. The
    response carries whether the runtime is built and, if not, the
    last build/rebuild failure (type, message, and a recovery hint
    derived from the exception class + message). The local daemon has
    no submission pipeline, so it never reports a build error.
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

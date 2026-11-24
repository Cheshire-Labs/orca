"""`orca topology` CLI verbs.

`mount` is backend-dispatched: the local daemon builds its package folder
(`topology.py` plus `workflows/`) into a SystemRuntime; a cloud deployment
takes a submitted topology.py source file (full system rebuild).
`get` works against both.

Pairs with `orca unload`, which tears the mounted runtime down.
"""

from pathlib import Path

import typer

from orca.cli import output
from orca.cli.backend import active_backend, cloud_backend_installed, cloud_client, cloud_help, daemon_client, get_client


app = typer.Typer(
    help="Mount + read the deployment topology (the physical lab).",
    no_args_is_help=True,
)


@app.command("mount")
def topology_mount(
    target: str | None = typer.Argument(
        None,
        help="Local: leave out; the daemon builds its own package folder."
             + cloud_help(" On the cloud backend, a topology.py source file, submitted to trigger a rebuild."),
        show_default=False,
    ),
    message: str = typer.Option(
        "", "--message", "-m",
        help="Cloud only: commit message for the submitted topology source.",
        hidden=not cloud_backend_installed(),
    ),
) -> None:
    """Mount the lab: build the system a workflow runs on.

    The daemon builds its package folder, `ORCA_PACKAGE_DIR` or the folder
    `orca start` ran in: `topology.py` and every `workflows/<name>.py`. To pick
    up edited files, run `orca runtime reload`.
    """
    if active_backend() == "local":
        if target is not None:
            output.fail(
                "the local daemon builds its own package folder (ORCA_PACKAGE_DIR, or the "
                "folder `orca start` ran in); run `orca topology mount` with no argument",
                code=output.EXIT_USAGE,
            )
        client = daemon_client()
        state = client.mount_package()
        output.info(f"mounted the package folder; runtime is {state.name}")
        return

    if target is None:
        output.fail(
            "cloud topology mount expects a topology.py source file",
            code=output.EXIT_USAGE,
        )
    source = Path(target)
    if not source.is_file():
        output.fail(
            f"cloud topology mount expects a source file; not found: {target}",
            code=output.EXIT_USAGE,
        )
    if not message:
        output.fail(
            "cloud topology mount requires --message/-m (commit message)",
            code=output.EXIT_USAGE,
        )
    cloud = cloud_client()
    payload = cloud.topology_submit(source.read_text(encoding="utf-8"), message)
    if output.get_mode().value == "json":
        output.emit_json(payload.model_dump(mode="json"))
        return
    typer.echo(f"submitted topology as {(payload.commit_sha or '')[:8]}")


@app.command("get")
def topology_get(
    source: bool = typer.Option(
        False, "--source",
        help="Read the deployment_package source-of-truth instead of the live runtime snapshot.",
    ),
) -> None:
    """Fetch the deployment topology. Works against both backends."""
    client = get_client()
    payload = client.topology_get(source=source)
    if output.get_mode().value == "json":
        output.emit_json(payload.model_dump(mode="json"))
        return
    output.emit_kv(
        "topology",
        [(k, str(v)) for k, v in payload.model_dump(mode="json").items()],
    )

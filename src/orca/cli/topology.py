"""`orca topology` CLI verbs.

`mount` is backend-dispatched: against the local daemon it mounts a
`build_topology(stores)` factory spec to build an empty SystemRuntime; against
a cloud deployment it submits a topology.py source file (full system rebuild).
`get` works against both.

Pairs with `orca unload`, which tears the mounted runtime down.
"""

from pathlib import Path

import typer

from orca.cli import output
from orca.cli.backend import active_backend, cloud_backend_installed, cloud_client, cloud_help, get_client


app = typer.Typer(
    help="Mount + read the deployment topology (the physical lab).",
    no_args_is_help=True,
)


@app.command("mount")
def topology_mount(
    target: str = typer.Argument(
        ...,
        help="A 'module:build_topology' factory spec, which the daemon "
             "imports from the directory `orca start` ran in."
             + cloud_help(" On the cloud backend, a topology.py source file, submitted to trigger a rebuild."),
    ),
    sim: bool = typer.Option(
        False, "--sim",
        help="Local only: start the mounted runtime in simulation mode.",
    ),
    message: str = typer.Option(
        "", "--message", "-m",
        help="Cloud only: commit message for the submitted topology source.",
        hidden=not cloud_backend_installed(),
    ),
) -> None:
    """Mount the lab. Builds the instrument/config foundation a workflow runs on.

    The daemon builds an empty SystemRuntime from a `module:build_topology`
    spec. The daemon, not this command, imports the module, so it resolves
    from the directory `orca start` ran in or from installed packages.
    """
    if active_backend() == "local":
        from orca.cli.client import LocalDaemonClient
        client = LocalDaemonClient()
        state = client.mount_topology(target, sim=sim)
        output.info(
            f"mounted topology {target!r} (sim={sim}); runtime is {state.name}",
        )
        return

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

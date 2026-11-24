"""`orca plugin ...` noun sub-app: list the loaded plugins."""

import typer

from orca.cli import output
from orca.cli.backend import get_client


app = typer.Typer(help="List the loaded plugins.", no_args_is_help=True)


@app.command("list")
def list_plugins() -> None:
    """List the loaded plugins."""
    rows = get_client().plugins_list()
    if output.get_mode().value == "json":
        output.emit_json([r.model_dump(mode="json") for r in rows])
        return
    output.emit_table(
        "Plugins", ["type"],
        [[r.type_name] for r in rows],
    )

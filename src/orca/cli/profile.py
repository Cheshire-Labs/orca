"""`orca profiles ...` noun sub-app.

Saved, named bundles of variable values. `orca run --profile <name>` picks one.
"""

from typing import List, Optional

import typer

from orca.cli import output
from orca.cli.app import parse_vars
from orca.cli.backend import get_client
from orca.variables.deployment_profile import DeploymentProfile


app = typer.Typer(
    help="Saved variable values a run can pick by name.",
    no_args_is_help=True,
)


def _parse_computed(entries: Optional[List[str]]) -> dict[str, str]:
    computed: dict[str, str] = {}
    for entry in entries or []:
        name, separator, expression = entry.partition("=")
        if not separator or not name.strip():
            output.fail(
                f"invalid --computed entry {entry!r}; expected NAME=EXPRESSION",
                code=output.EXIT_USAGE,
            )
        computed[name.strip()] = expression.strip()
    return computed


def _build_profile(
    name: str, description: str, vars_: str, computed: Optional[List[str]],
) -> DeploymentProfile:
    return DeploymentProfile(
        name=name,
        description=description,
        variables=parse_vars(vars_),
        computed=_parse_computed(computed),
    )


def _joined(values: dict[str, str]) -> str:
    return ", ".join(f"{key}={value}" for key, value in values.items()) or "-"


@app.command("list")
def list_profiles() -> None:
    """List every saved profile."""
    items = get_client().profiles_list()
    if output.get_mode().value == "json":
        output.emit_json([p.model_dump(mode="json") for p in items])
        return
    output.emit_table(
        "Profiles",
        ["name", "description", "variables", "computed"],
        [
            [
                p.name, p.description,
                _joined({k: str(v) for k, v in p.variables.items()}),
                _joined(p.computed),
            ]
            for p in items
        ],
    )


@app.command("show")
def show_profile(name: str = typer.Argument(..., help="Profile name.")) -> None:
    """Show one profile's description, values and computed expressions."""
    profile = get_client().profiles_get(name)
    if output.get_mode().value == "json":
        output.emit_json(profile.model_dump(mode="json"))
        return
    output.emit_kv(
        f"profile {profile.name}",
        [
            ("description", profile.description),
            *((key, str(value)) for key, value in profile.variables.items()),
            *((f"{key} (computed)", value) for key, value in profile.computed.items()),
        ],
    )


_VARS_HELP = "KEY=VAL,KEY2=VAL2. Prefix a key with global. to set it deployment-wide."
_COMPUTED_HELP = "NAME=EXPRESSION. Repeat for more than one."


@app.command("create")
def create_profile(
    name: str = typer.Argument(..., help="Unique profile name."),
    description: str = typer.Option("", "--description"),
    vars_: str = typer.Option("", "--vars", help=_VARS_HELP),
    computed: Optional[List[str]] = typer.Option(None, "--computed", help=_COMPUTED_HELP),
    reason: str | None = typer.Option(None, "--reason", help="Why, for the audit trail."),
) -> None:
    """Save a new profile. 409 on a duplicate name."""
    created = get_client().profiles_add(
        _build_profile(name, description, vars_, computed), reason=reason,
    )
    if output.get_mode().value == "json":
        output.emit_json(created.model_dump(mode="json"))
        return
    output.info(f"created profile [cyan]{created.name}[/cyan]")


@app.command("update")
def update_profile(
    name: str = typer.Argument(..., help="Profile name to replace."),
    description: str = typer.Option("", "--description"),
    vars_: str = typer.Option("", "--vars", help=_VARS_HELP),
    computed: Optional[List[str]] = typer.Option(None, "--computed", help=_COMPUTED_HELP),
    reason: str | None = typer.Option(None, "--reason", help="Why, for the audit trail."),
) -> None:
    """Replace a profile's body. A running execution keeps the values it started with."""
    updated = get_client().profiles_update(
        _build_profile(name, description, vars_, computed), reason=reason,
    )
    if output.get_mode().value == "json":
        output.emit_json(updated.model_dump(mode="json"))
        return
    output.info(f"updated profile [cyan]{updated.name}[/cyan]")


@app.command("delete")
def delete_profile(
    name: str = typer.Argument(..., help="Profile name to delete."),
    reason: str | None = typer.Option(None, "--reason", help="Why, for the audit trail."),
) -> None:
    """Delete a profile. A running execution keeps the values it started with."""
    get_client().profiles_delete(name, reason=reason)
    output.info(f"deleted profile [cyan]{name}[/cyan]")

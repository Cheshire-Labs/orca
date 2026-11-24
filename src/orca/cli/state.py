"""Operator verbs over the state nobody has settled."""

import typer

from orca.cli import output
from orca.cli.backend import get_client

app = typer.Typer(help="State an operator has to settle.")

# The server names the operation; the worklist names the verb to type.
_CLI_VERB = {
    "set-well-volumes": "orca labware set-volume",
    "set-tip-state": "orca labware set-tips",
    "set-mounted-tips": "orca device set-mounted-tips",
}


@app.command("unsettled")
def unsettled_state() -> None:
    """Everything nobody has stated, and everything that went unwatched.

    Reads only. This is the worklist for walking up to a paused system: each
    line names the thing and the verb that settles it.
    """
    client = get_client()
    result = client.state_unsettled()
    output.emit_json(result.model_dump(mode="json"))
    if not result.unsettled:
        output.info("nothing is unsettled; the record can answer for everything it tracks")
        return
    for subject in result.unsettled:
        output.info(f"{subject.subject}: {subject.detail}")
        output.info(f"  settle with: {_CLI_VERB.get(subject.settle_with, subject.settle_with)}")

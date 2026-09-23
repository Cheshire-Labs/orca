"""Operator verbs over the state nobody has settled."""

import typer

from orca.cli import output
from orca.cli.backend import get_client

app = typer.Typer(help="State an operator has to settle.")


@app.command("unsettled")
def unsettled_state() -> None:
    """Everything nobody has stated, and everything that went unwatched.

    Reads only. This is the worklist for walking up to a paused system: each
    line names the thing and the verb that settles it.
    """
    client = get_client()
    result = client.state_unsettled()
    if not result.unsettled:
        output.info("nothing is unsettled; the record can answer for everything it tracks")
        return
    for subject in result.unsettled:
        output.info(f"{subject.subject}: {subject.detail}")
        if subject.settle_with is not None:
            output.info(f"  settle with: {subject.settle_with}")

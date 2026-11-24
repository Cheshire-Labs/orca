"""Every `orca ...` line a remedy step prints is a command the CLI accepts once its <blanks> are filled."""

import re
import shlex

import pytest
import typer
from typer._click import Command, Context
from typer._click.exceptions import ClickException
from typer.core import TyperGroup

from orca.cli.app import app
from orca.runtime.blockers import derive_blockers
from tests.test_every_remedy_step_is_a_call_its_route_accepts import _every_kind_of_blocker, _steps

_BLANK = re.compile(r"<[a-z_]+>")


def _why_the_cli_refuses(line: str) -> str | None:
    words = shlex.split(_BLANK.sub("filled", line))
    if words[:1] != ["orca"]:
        return f"{line!r} does not start with orca"
    command: Command = typer.main.get_command(app)
    words = words[1:]
    while isinstance(command, TyperGroup):
        name = words.pop(0) if words else ""
        found = command.get_command(Context(command), name)
        if found is None:
            return f"{line!r}: there is no {name!r} under {command.name!r}"
        command = found
    try:
        command.make_context(command.name or "", words)
    except ClickException as refused:
        return f"{line!r}: {refused.format_message()}"
    return None


@pytest.mark.asyncio
async def test_every_remedy_cli_line_parses_against_the_real_cli() -> None:
    lines = sorted({
        step.cli for sources in _every_kind_of_blocker()
        for step in _steps(await derive_blockers(sources)) if step.cli
    })

    assert lines
    assert [problem for line in lines if (problem := _why_the_cli_refuses(line))] == []

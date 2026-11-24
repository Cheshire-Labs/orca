"""The time the engine budgets for a command sent as wire params."""

from collections.abc import Mapping

from cheshire_drivers.command_table import COMMANDS
from pydantic import JsonValue, ValidationError

from orca.runtime.step_pacer import nominal_seconds_for


def wire_budget(interfaces: frozenset[str], command: str, params: Mapping[str, JsonValue]) -> float | None:
    """The seconds the engine budgets for a command sent as wire params, read through the command table's request model.

    A verb two advertised interfaces declare is read as the one whose arguments the params fit.
    Params no declaration fits get the first declaration's budget for no arguments.
    """
    declaring = [
        (cls, entry)
        for name in sorted(interfaces)
        if (entry := COMMANDS.commands_of(name).get(command)) is not None
        and not entry.is_property
        and (cls := COMMANDS.interface(name)) is not None
    ]
    for cls, entry in declaring:
        try:
            arguments = entry.driver_kwargs(params)
        except ValidationError:
            continue
        return nominal_seconds_for(cls, command, arguments)
    return nominal_seconds_for(declaring[0][0], command, {}) if declaring else None

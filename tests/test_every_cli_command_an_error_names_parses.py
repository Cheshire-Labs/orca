"""An error that tells the operator which `orca ...` command to run names one the CLI accepts once its blanks are filled."""

import re

import pytest

from orca.resource_models.device_error import MoverAlreadyHoldingError, TipsNotPresentError, TipsOnTheHeadError
from orca.runtime.errors import LocationReservedError
from tests.test_every_remedy_cli_hint_runs_as_printed import _why_the_cli_refuses

_COMMAND = re.compile(r"`(orca [^`]+)`")


@pytest.mark.parametrize("error", [
    LocationReservedError("deck/1", "res-1", "t-1", "plate", inbound_labware="plate-2", awaiting_operator=True),
    LocationReservedError("deck/1", "res-1", "t-1", "plate"),
    MoverAlreadyHoldingError("arm", "lw-1", "plate-1", "plate"),
    TipsNotPresentError("rack", "rack-1", ["A1", "B1"], 0),
    TipsOnTheHeadError("lh", "nothing has said the head is empty"),
], ids=lambda error: type(error).__name__)
def test_every_command_the_message_names_parses(error: Exception) -> None:
    commands = _COMMAND.findall(str(error))

    assert commands
    assert [why for line in commands if (why := _why_the_cli_refuses(line.replace("...", "filled")))] == []

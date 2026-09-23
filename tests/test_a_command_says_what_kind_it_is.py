"""What a command is gets named, not carried beside it as a flag.

`execute_command` branches five ways on what sort of command it was handed:
the faulted-device refusal, the busy-check, the `_pending` record, the command
timer, and the fault latch. All five ask the same question, whether this
command takes the device, so `CommandKind` answers it once and every site
reads that answer.

A boolean here would be that fact carried next to the command, leaving a
reader to find every site that reads it before they know what it means. The
second such boolean is worse than the first, because two flags then interact
and nothing says how.

Read as text (`cheshire_source_text`), because the repo bans parsing source
into a syntax tree. Positive and negative controls included: a scan over a
clean tree passes whether or not it works.
"""

import re
from pathlib import Path

import pytest

from cheshire_source_text import code_lines_of

from orca.gateway.controller.command_kind import CommandKind

CONTROLLER = (
    Path(__file__).resolve().parents[1]
    / "src/orca/gateway/controller/controller.py"
)

ALLOWED_BOOLEANS = frozenset({
    # A person answered this call. Not a property of the command.
    "confirm",
    # The driver class's own disconnect policy, plumbed down because the
    # controller dispatches by id and holds no driver to ask.
    "resend_on_reconnect",
})
"""Booleans `execute_command` may take, each with the reason it is not a kind.

Adding to this list is the review gate. A boolean that answers "what sort of
command is this" belongs in `CommandKind`, because the alternative is what
this test exists to stop: five behaviours keyed off one negated flag whose
name described neither.
"""


def _boolean_parameters_of_execute_command() -> set[str]:
    """Every `name: bool` in the signature, read as text."""
    lines = code_lines_of(CONTROLLER)
    for index, line in enumerate(lines):
        if line.text.strip().startswith("async def execute_command("):
            signature = " ".join(line.text for line in lines[index:index + 2])
            return set(re.findall(r"(\w+)\s*:\s*bool", signature))
    raise AssertionError("execute_command not found in the controller")


def test_the_kind_says_whether_the_command_takes_the_device() -> None:
    assert CommandKind.ACTUATION.occupies_the_device is True
    assert CommandKind.WORLD_SYNC.occupies_the_device is False


def test_execute_command_grows_no_new_boolean() -> None:
    """The rule, checkable at review time: a new boolean parameter here needs
    a reason, and "it is a kind of command" is not one, that is `kind`.

    Equality, not a subset. A subset check is satisfied by the empty set, so a
    scan that stopped finding parameters at all would pass forever.
    """
    booleans = _boolean_parameters_of_execute_command()

    assert booleans == ALLOWED_BOOLEANS, booleans ^ ALLOWED_BOOLEANS


def test_the_old_flag_is_gone_everywhere() -> None:
    """No transition shim: every caller moved in the same change."""
    root = Path(__file__).resolve().parents[1]
    named = [
        path for path in (root / "src").rglob("*.py")
        if "is_idempotent_state_sync" in path.read_text(encoding="utf-8")
    ]

    assert named == [], named


def test_the_signature_scan_finds_a_boolean_that_is_there() -> None:
    """Positive control: without it, a broken regex passes on a clean tree."""
    assert re.findall(r"(\w+)\s*:\s*bool", "def f(self, confirm: bool = False)") == [
        "confirm",
    ]


@pytest.mark.parametrize("shape", [
    "def f(self, kind: CommandKind = CommandKind.ACTUATION)",
    "def f(self, params: Dict[str, JsonValue])",
    "def f(self, timeout_seconds: Optional[float] = None)",
])
def test_the_signature_scan_leaves_a_non_boolean_alone(shape: str) -> None:
    """Negative control: a scan that matched every parameter would fail the
    guard above for reasons that have nothing to do with booleans."""
    assert re.findall(r"(\w+)\s*:\s*bool", shape) == []

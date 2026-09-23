"""`orca status` against a cloud backend prints what is stopping the run.

It used to print `built: True` and stop, which is the shape of the problem: the
runtime was up, the run was going nowhere, and the CLI said nothing was wrong.
"""

import pytest

from orca.cli import output
from orca.cli.control_plane import (
    BlockerDTO,
    RemedyDTO,
    RemedyStepDTO,
    RuntimeStatusResponseDTO,
)
from orca.cli.status import _render_blockers


CLEAR_FAULT = RemedyDTO(
    id="device.clear_fault",
    label="Clear the fault",
    explain="Clears the record, not the trouble.",
    recommended=True,
    steps=[RemedyStepDTO(
        verb="device.clear_fault",
        label="Clear the fault",
        cli="orca device clear-fault pf400_1",
    )],
)

FIND_THE_PLATE = RemedyDTO(
    id="correct_the_source_then_retry",
    label="Say where the plate is, then retry",
    recommended=True,
    steps=[
        RemedyStepDTO(verb="labware.edit_location", label="Say where it is",
                      cli="orca labware edit-location p1 pad_1"),
        RemedyStepDTO(verb="thread.recover.RETRY", label="Retry",
                      cli="orca thread recover e1 t1 --decision RETRY"),
    ],
)


def a_blocker(
    *,
    kind: str = "DEVICE_FAULT",
    remedies: list[RemedyDTO] | None = None,
    may_still_be_moving: bool = False,
) -> BlockerDTO:
    return BlockerDTO(
        id="device_fault:pf400_1",
        kind=kind,
        severity="error",
        headline="pf400_1: 'initialize' did not come back clean.",
        remedies=[CLEAR_FAULT] if remedies is None else remedies,
        may_still_be_moving=may_still_be_moving,
    )


def status(
    *,
    blockers: list[BlockerDTO] | None = None,
    blockers_known: bool = True,
) -> RuntimeStatusResponseDTO:
    rows = blockers or []
    return RuntimeStatusResponseDTO(
        built=True,
        blockers=rows,
        blocker_count=len(rows),
        blockers_known=blockers_known,
    )


@pytest.fixture(autouse=True)
def table_mode(monkeypatch):
    """Render wide. A narrow console wraps a cell and splits it across the
    columns, so an assertion would be testing the terminal, not the output."""
    output.set_mode(output.OutputMode.TABLE)
    monkeypatch.setenv("COLUMNS", "400")


def rendered(capsys) -> str:
    """The table's text with its borders taken out."""
    raw = capsys.readouterr().out
    stripped = "".join(" " if ch in "┌┐└┘├┤┬┴┼─│" else ch for ch in raw)
    return " ".join(stripped.split())


def test_a_faulted_device_prints_with_the_command_that_clears_it(capsys):
    _render_blockers(status(blockers=[a_blocker()]))
    printed = rendered(capsys)
    assert "did not come back clean" in printed
    assert "clear-fault" in printed


def test_a_multi_step_remedy_prints_its_commands_in_order(capsys):
    _render_blockers(status(blockers=[
        a_blocker(kind="THREAD_ERROR_PAUSE", remedies=[FIND_THE_PLATE]),
    ]))
    printed = rendered(capsys)
    assert "edit-location" in printed
    assert "RETRY" in printed
    assert printed.index("edit-location") < printed.index("RETRY")


def test_nothing_in_the_way_says_so(capsys):
    _render_blockers(status())
    assert "nothing is in the way" in rendered(capsys)


def test_an_unreadable_list_is_never_printed_as_an_all_clear(capsys):
    _render_blockers(status(blockers_known=False))
    printed = rendered(capsys)
    assert "not an all-clear" in printed
    assert "nothing is in the way" not in printed


def test_a_machine_that_may_still_be_moving_is_flagged_over_its_severity(capsys):
    _render_blockers(status(blockers=[a_blocker(may_still_be_moving=True)]))
    assert "!" in rendered(capsys)

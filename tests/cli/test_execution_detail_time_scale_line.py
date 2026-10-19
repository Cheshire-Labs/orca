"""The detail printout's time scale line: the number, or why there is none."""

from orca.cli.execution import _time_scale_cell


def test_a_scale_prints_as_a_plain_number() -> None:
    assert _time_scale_cell(0.5) == "0.5"
    assert _time_scale_cell(1.0) == "1"
    assert _time_scale_cell(0.0) == "0"


def test_no_scale_says_which_runs_have_none() -> None:
    assert _time_scale_cell(None) == "(hardware pace, or not kept after the run)"

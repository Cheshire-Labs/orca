"""The detail printout's sim speed-up line: the number, or why there is none."""

from orca.cli.output import sim_speedup_cell


def test_a_speedup_prints_as_a_plain_number() -> None:
    assert sim_speedup_cell(True, 2.5) == "2.5"
    assert sim_speedup_cell(True, 100.0) == "100"


def test_a_simulated_run_with_no_speedup_does_not_wait() -> None:
    assert sim_speedup_cell(True, None) == "(no waiting)"


def test_no_speedup_on_a_run_that_is_not_simulated_says_why() -> None:
    assert sim_speedup_cell(False, None) == "(hardware pace, or not kept after the run)"

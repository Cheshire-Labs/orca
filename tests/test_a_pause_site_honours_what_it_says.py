"""The table of what each pause site honours cannot drift from the sites.

`HONOURED_DECISIONS` is the one statement of which recovery verbs apply where,
and `resume_with_decision` refuses off it. Writing it as data was argued against
once, in `PauseSite`'s own docstring, on the grounds that a second copy would
drift from the code that enforces it. The argument was right about a copy. It is
not a copy: nothing else refuses a verb, so before this the enforcement was a
fallthrough that failed the thread.

One drift is left, and it is the one the objection was really about: the table
permits a verb at a site whose code does nothing with it, so the verb is accepted
and then falls through anyway. That is what these tests catch. They read the
source as text -- the repo bans parsing it into a syntax tree -- and each scan
carries a positive and a negative control, because a scan over a clean tree
passes whether or not it works.
"""

import re
from pathlib import Path

import pytest

from cheshire_source_text import code_lines_of
from orca.workflow_models.labware_threads.executing_labware_thread import (
    _ABORTS_THAT_END_AN_UNBOUND_THREAD,
)
from orca.workflow_models.status_enums import (
    HONOURED_DECISIONS,
    PauseSite,
    RecoveryDecision,
)

THREAD = (
    Path(__file__).resolve().parents[1]
    / "src/orca/workflow_models/labware_threads/executing_labware_thread.py"
)
METHOD = (
    Path(__file__).resolve().parents[1] / "src/orca/workflow_models/method.py"
)


def _verbs_named_in(path: Path) -> set[str]:
    """Every RecoveryDecision member the file branches on, read as text."""
    code = " ".join(line.text for line in code_lines_of(path))
    return set(re.findall(r"RecoveryDecision\.([A-Z_]+)", code))


def test_every_pause_site_says_what_it_honours() -> None:
    assert set(HONOURED_DECISIONS) == set(PauseSite)


def test_every_site_honours_a_way_to_try_again_and_a_way_out() -> None:
    """No site may strand a thread with nothing an operator can send it."""
    for site, recovery in HONOURED_DECISIONS.items():
        assert RecoveryDecision.RETRY in recovery.honours, site
        assert RecoveryDecision.ABORT_THREAD in recovery.honours, site


def test_retry_op_is_honoured_only_where_a_device_call_is_suspended() -> None:
    """RETRY_OP re-runs the suspended call, so it needs one to be suspended."""
    honours_it = {
        site for site, r in HONOURED_DECISIONS.items()
        if RecoveryDecision.RETRY_OP in r.honours
    }
    assert honours_it == {PauseSite.DEVICE_OP}


def test_the_action_level_verbs_are_honoured_where_something_acts_on_them() -> None:
    """Two shapes, and they are not the same shape.

    At ACTION_BODY and DEVICE_OP the verbs mean what they say: an action is
    bound and `ExecutingMethod.handle_recovery` discards it. At the two
    pre-binding sites there is no action, and they end the thread instead --
    deliberately, because the alternative was the owner and its contributors
    parked at PAUSED forever. Every other site fails the thread on them, which
    nobody chose, so those refuse.
    """
    for verb in (RecoveryDecision.ABORT_ACTION, RecoveryDecision.ABORT_METHOD):
        honours_it = {
            site for site, r in HONOURED_DECISIONS.items() if verb in r.honours
        }
        assert honours_it == {
            PauseSite.ACTION_BODY, PauseSite.DEVICE_OP,
            PauseSite.ACTION_RESOLUTION, PauseSite.DEADLOCK,
        }, verb


def test_continue_is_honoured_only_where_there_is_work_to_carry_on_from() -> None:
    """The verb with no second shape: it advances to the next action, so it
    needs a bound action or a move whose target the ledger confirms."""
    honours_it = {
        site for site, r in HONOURED_DECISIONS.items()
        if RecoveryDecision.CONTINUE in r.honours
    }

    assert honours_it == {
        PauseSite.ACTION_BODY, PauseSite.DEVICE_OP, PauseSite.MOVE,
    }


def test_no_verb_is_permitted_that_the_engine_never_branches_on() -> None:
    """Catches a new RecoveryDecision member added to the table with no code
    behind it anywhere.

    Deliberately a union across sites, because text cannot say WHICH site a
    verb is branched at, and that makes it weak: every current verb is named
    somewhere in these two files, so moving one between sites does not move
    this. The per-site guarantees are the three placement tests above, which
    do not scan text.
    """
    acted_on = _verbs_named_in(THREAD) | _verbs_named_in(METHOD)
    permitted = {d.name for r in HONOURED_DECISIONS.values() for d in r.honours}
    assert permitted <= acted_on, permitted - acted_on


def test_every_site_explains_itself_to_an_operator() -> None:
    """The refusal message carries `because`, and it is the whole explanation
    now that the knowledge base no longer keeps a table of its own."""
    for site, recovery in HONOURED_DECISIONS.items():
        assert len(recovery.because) > 20, site
        assert not recovery.because.endswith("."), site


def test_the_verb_scan_finds_a_verb_that_is_branched_on() -> None:
    """Positive control: without it a broken regex passes on a clean tree."""
    assert "RETRY" in _verbs_named_in(THREAD)


def test_the_verb_scan_ignores_a_verb_named_only_in_a_comment() -> None:
    """Negative control: `code_lines_of` blanks comments and strings, so a verb
    mentioned in prose is not mistaken for one the code acts on."""
    scratch = Path(__file__).with_name("_scan_control.py")
    scratch.write_text(
        "# RecoveryDecision.ABORT_METHOD is discussed here and nowhere else\n"
        'DOC = "RecoveryDecision.CONTINUE in a string"\n'
        "if decision == RecoveryDecision.RETRY:\n"
        "    pass\n",
        encoding="utf-8",
    )
    try:
        assert _verbs_named_in(scratch) == {"RETRY"}
    finally:
        scratch.unlink()


@pytest.mark.parametrize("site", [
    PauseSite.MOVE, PauseSite.THREAD_STEP, PauseSite.WAIT_EVENT_TIMEOUT,
    PauseSite.MOVE_RESOLUTION, PauseSite.SPAWN_CAPACITY,
])
def test_the_sites_that_only_failed_a_thread_now_refuse(site: PauseSite) -> None:
    """Why this exists. At each of these an action-level abort fell through to
    `raise error` and landed the thread FAILED, taking the execution with it at
    a move. Nobody chose that, so nothing honours it."""
    assert RecoveryDecision.ABORT_ACTION not in HONOURED_DECISIONS[site].honours
    assert RecoveryDecision.ABORT_METHOD not in HONOURED_DECISIONS[site].honours


def test_every_verb_a_pre_binding_site_honours_has_a_branch_that_ends_the_thread() -> None:
    """The table promised these end the thread; one handler let them fail it.

    `consume_next_unresolved_action`'s handler ran `_check_abort_thread`, which
    acts on ABORT_THREAD alone, and then re-raised -- so ABORT_ACTION and
    ABORT_METHOD failed the thread and took the execution down, at a site whose
    own entry says they end it instead. The sibling handler forty lines later
    aborted cleanly. This pins the mapping to the table, so a verb added to a
    pre-binding site cannot go back to falling through.
    """
    handled = _ABORTS_THAT_END_AN_UNBOUND_THREAD | {RecoveryDecision.ABORT_THREAD}
    for site in (PauseSite.ACTION_RESOLUTION, PauseSite.DEADLOCK):
        unhandled = HONOURED_DECISIONS[site].honours - handled - {RecoveryDecision.RETRY}
        assert unhandled == frozenset(), f"{site} honours {unhandled} with no branch"


def test_the_narrower_aborts_are_the_ones_mapped_and_nothing_else() -> None:
    """Mapping RETRY or ABORT_THREAD here would change what the operator asked."""
    assert _ABORTS_THAT_END_AN_UNBOUND_THREAD == frozenset({
        RecoveryDecision.ABORT_ACTION,
        RecoveryDecision.ABORT_METHOD,
    })

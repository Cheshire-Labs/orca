"""A named remedy's recover step uses the retry the pause honours, and a remedy the pause cannot take is not offered."""
from orca.runtime.remedies import RemedyAction, RemedyContext, remedies_for

_ACTION_BODY = frozenset({"RETRY_ACTION", "CONTINUE", "ABORT_ACTION", "ABORT_METHOD", "ABORT_THREAD"})
_DEVICE_OP_OR_WAIT = frozenset({"RETRY", "CONTINUE", "ABORT_ACTION", "ABORT_METHOD", "ABORT_THREAD"})


def _recover_decisions(error_type: str, honoured: frozenset[str]) -> list[list[str]]:
    ctx = RemedyContext(execution_id="e1", thread_id="t1", honoured_decisions=honoured)
    return [
        [str(step.args["decision"]) for step in remedy.steps if step.action is RemedyAction.RECOVER_THREAD]
        for remedy in remedies_for(error_type, ctx)
    ]


def test_a_plate_in_the_way_retries_the_step_inside_a_device_call() -> None:
    assert _recover_decisions("SlotOccupiedError", _DEVICE_OP_OR_WAIT) == [["RETRY"]]


def test_a_plate_in_the_way_reruns_the_action_where_the_actions_own_code_failed() -> None:
    assert _recover_decisions("SlotOccupiedError", _ACTION_BODY) == [["RETRY_ACTION"]]
    assert _recover_decisions("LocationReservedError", _ACTION_BODY) == [["RETRY_ACTION"]]


def test_reconciling_the_head_retries_the_failed_step_where_one_failed() -> None:
    assert _recover_decisions("HasTipError", _DEVICE_OP_OR_WAIT) == [["RETRY"]]
    assert _recover_decisions("HasTipError", _ACTION_BODY) == [["RETRY_ACTION"]]


def test_a_remedy_whose_recovery_the_pause_refuses_is_not_offered() -> None:
    assert _recover_decisions("SlotOccupiedError", frozenset({"ABORT_THREAD"})) == []

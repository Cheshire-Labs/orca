"""Sending a verb a pause site does not honour must not cost the thread.

On 2026-09-01 an operator at a failed move had two verbs that worked and four
that did not, and nothing said which. The four did not no-op: at a move they
failed the thread and took the execution with it, and at action resolution they
silently acted as ABORT_THREAD, which is a different answer from the one asked.

`resume_with_decision` now refuses off `HONOURED_DECISIONS` before anything is
delivered, so a wrong pick costs a message and the thread is still there to be
told something else.
"""

from unittest.mock import MagicMock

import pytest

from orca.workflow_models.labware_threads.executing_labware_thread import (
    ExecutingLabwareThread,
)
from orca.workflow_models.status_enums import (
    LabwareThreadStatus,
    PauseSite,
    RecoveryDecision,
)


def _pause_site_of(thread: MagicMock) -> PauseSite | None:
    """The real property body, run against a stub."""
    getter = ExecutingLabwareThread.pause_site.fget
    assert getter is not None
    return getter(thread)


def _honoured_decisions_of(thread: MagicMock) -> list[RecoveryDecision]:
    getter = ExecutingLabwareThread.honoured_decisions.fget
    assert getter is not None
    return getter(thread)


def _paused_at(site: PauseSite, move: MagicMock | None = None) -> MagicMock:
    """A thread error-paused at one site, with the real refusals bound.

    A `spec` mock stubs out every method, the refusals included, so a test of a
    refusal that leaves them mocked passes on a broken engine.
    """
    thread = MagicMock(spec=ExecutingLabwareThread)
    thread.name = "plate_1"
    thread.status = LabwareThreadStatus.PAUSED
    thread._last_error = RuntimeError("Stall or Collision Detected")
    thread._pause_site = site
    thread._move_action = move
    thread._recovery_decision = None
    thread._shared_action_group = MagicMock(return_value=None)
    thread._resume_event = MagicMock()
    thread._following_peer_action = False
    thread.pause_site = site
    thread.is_error_paused = True
    thread._refuse_a_verb_this_site_does_not_honour = (
        lambda decision: ExecutingLabwareThread
        ._refuse_a_verb_this_site_does_not_honour(thread, decision)
    )
    thread._refuse_continue_the_ledger_does_not_back = (
        lambda: ExecutingLabwareThread
        ._refuse_continue_the_ledger_does_not_back(thread)
    )
    return thread


def _move_to(target: str) -> MagicMock:
    move = MagicMock()
    move.labware.name = "r6_final"
    move.target.position_id = target
    return move


def _assert_refused(thread: MagicMock, verb: RecoveryDecision) -> str:
    with pytest.raises(ValueError) as refused:
        ExecutingLabwareThread.resume_with_decision(thread, verb)
    thread._resume_event.set.assert_not_called()
    assert thread._recovery_decision is not verb
    return str(refused.value)


@pytest.mark.parametrize(
    "verb", [RecoveryDecision.ABORT_ACTION, RecoveryDecision.ABORT_METHOD],
)
def test_an_action_level_abort_at_a_move_no_longer_fails_the_thread(
    verb: RecoveryDecision,
) -> None:
    """The bench case. A move is not a method action, so there was nothing to
    discard, and the verb fell through to `raise error`."""
    thread = _paused_at(PauseSite.MOVE, _move_to("flex_1/C1-slot"))

    message = _assert_refused(thread, verb)

    assert verb.name in message
    assert "MOVE" in message
    assert "RETRY" in message and "ABORT_THREAD" in message
    assert "still paused" in message


@pytest.mark.parametrize(
    "site", [PauseSite.ACTION_RESOLUTION, PauseSite.DEADLOCK],
)
def test_an_abort_before_binding_still_ends_the_rendezvous(
    site: PauseSite,
) -> None:
    """These two accept the narrower aborts and end the thread on them, which
    reads like a silent substitution and is not: with no bound action the
    alternative was leaving the owner and its contributors parked at PAUSED
    forever, and `test_pre_binding_abort_decision_tears_group_down_cleanly`
    pins the choice. The table honours what the site does."""
    thread = _paused_at(site)

    ExecutingLabwareThread.resume_with_decision(
        thread, RecoveryDecision.ABORT_ACTION,
    )

    assert thread._recovery_decision is RecoveryDecision.ABORT_ACTION


@pytest.mark.parametrize(
    "site", [PauseSite.ACTION_RESOLUTION, PauseSite.DEADLOCK],
)
def test_continue_before_binding_is_still_refused(site: PauseSite) -> None:
    """The verb that genuinely has nothing to act on: CONTINUE carries on to
    the next action, and there is no bound action to carry on from."""
    thread = _paused_at(site)

    message = _assert_refused(thread, RecoveryDecision.CONTINUE)

    assert "no bound action" in message


def test_retry_op_is_refused_where_no_device_call_is_suspended() -> None:
    thread = _paused_at(PauseSite.ACTION_BODY)

    message = _assert_refused(thread, RecoveryDecision.RETRY_OP)

    assert "no call is suspended here" in message


def test_continue_at_a_move_is_refused_until_the_ledger_agrees() -> None:
    """Legality and evidence are separate. The site honours CONTINUE; the
    operator has not yet recorded that they carried the plate to the target."""
    thread = _paused_at(PauseSite.MOVE, _move_to("flex_1/C1-slot"))
    thread._ledger_position = MagicMock(return_value=None)

    message = _assert_refused(thread, RecoveryDecision.CONTINUE)

    assert "nowhere recorded" in message
    assert "labware edit-location" in message


def test_continue_at_a_move_is_delivered_once_the_ledger_agrees() -> None:
    """The recovery the bench actually uses, and the reason the site table
    cannot be the only gate at a move."""
    move = _move_to("flex_1/C1-slot")
    thread = _paused_at(PauseSite.MOVE, move)
    at_target = MagicMock()
    at_target.position_id = "flex_1/C1-slot"
    thread._ledger_position = MagicMock(return_value=at_target)

    ExecutingLabwareThread.resume_with_decision(thread, RecoveryDecision.CONTINUE)

    assert thread._recovery_decision is RecoveryDecision.CONTINUE
    thread._resume_event.set.assert_called_once()


@pytest.mark.parametrize("verb", list(RecoveryDecision))
def test_every_verb_is_delivered_at_the_site_that_honours_them_all(
    verb: RecoveryDecision,
) -> None:
    """Negative control. A refusal that refused everything would pass every
    test above; this fails unless the honoured verbs still get through."""
    thread = _paused_at(PauseSite.DEVICE_OP)

    ExecutingLabwareThread.resume_with_decision(thread, verb)

    assert thread._recovery_decision is verb
    thread._resume_event.set.assert_called_once()


def test_a_paused_thread_that_cannot_say_where_it_stopped_is_not_guessed_at() -> None:
    thread = _paused_at(PauseSite.MOVE)
    thread._pause_site = None
    thread.pause_site = None

    message = _assert_refused(thread, RecoveryDecision.RETRY)

    assert "does not say where" in message


class TestAContributorFollowingAPeersAction:
    """A contributor stamps its own site once and the group moves under it.

    The owner can fail inside a device call, take a RETRY, and fail again in
    the action body around it. `reset_decision` says so outright: participants
    stay paused across the re-drive. So a stamp taken at the first pause is
    from a site the group has since left, and offering RETRY_OP off it offers
    to re-run a call that is no longer suspended.

    `paused_device_command` has always read the group live for this reason.
    The site has to be read the same way or the two disagree.
    """

    @staticmethod
    def _contributor(op_paused_command: str | None) -> MagicMock:
        group = MagicMock()
        group.action_paused.is_set.return_value = True
        group.op_paused_command = op_paused_command
        thread = _paused_at(PauseSite.DEVICE_OP)
        thread._following_peer_action = True
        thread._shared_action_group = MagicMock(return_value=group)
        del thread.pause_site
        return thread

    def test_the_site_follows_the_group_off_the_device_call(self) -> None:
        thread = self._contributor(op_paused_command=None)

        assert _pause_site_of(thread) is (
            PauseSite.ACTION_BODY
        )

    def test_the_site_follows_the_group_onto_one(self) -> None:
        thread = self._contributor(op_paused_command="aspirate")

        assert _pause_site_of(thread) is PauseSite.DEVICE_OP

    def test_a_thread_of_its_own_keeps_its_own_site(self) -> None:
        """Negative control: reading the group unconditionally would tell a
        contributor whose OWN move failed that it stopped in an action body."""
        site = PauseSite.MOVE
        thread = _paused_at(site, _move_to("flex_1/C1-slot"))
        thread._following_peer_action = False

        assert _pause_site_of(thread) is PauseSite.MOVE


class TestAThreadPausedOffTheGroupsAction:
    """A group's single decision belongs to the group's pause, not to a thread
    that happens to be in it.

    A contributor's own move can fail while the owner's action is also paused.
    The decision channel is shared, so without a discriminator that contributor
    consumes an ABORT_METHOD meant for the owner, fails itself on a verb its
    site does not honour, and leaves the owner waiting on a decision that was
    already eaten. Both ends pick the channel the same way.
    """

    @staticmethod
    def _in_a_paused_group(site: PauseSite) -> MagicMock:
        group = MagicMock()
        group.action_paused.is_set.return_value = True
        group.op_paused_command = None
        thread = _paused_at(site, _move_to("flex_1/C1-slot"))
        thread._shared_action_group = MagicMock(return_value=group)
        thread._ledger_position = MagicMock(return_value=None)
        return thread

    @pytest.mark.parametrize("site", [PauseSite.MOVE, PauseSite.SPAWN_CAPACITY])
    def test_its_decision_goes_to_its_own_event_not_the_group(
        self, site: PauseSite,
    ) -> None:
        thread = self._in_a_paused_group(site)

        ExecutingLabwareThread.resume_with_decision(
            thread, RecoveryDecision.RETRY,
        )

        thread._shared_action_group().submit_decision.assert_not_called()
        thread._resume_event.set.assert_called_once()

    def test_the_groups_own_pause_still_goes_to_the_group(self) -> None:
        """Negative control: routing everything to the thread's own event would
        break the rendezvous, where any participant may feed the decision."""
        thread = self._in_a_paused_group(PauseSite.ACTION_BODY)
        thread._following_peer_action = False

        ExecutingLabwareThread.resume_with_decision(
            thread, RecoveryDecision.ABORT_METHOD,
        )

        thread._shared_action_group().submit_decision.assert_called_once_with(
            RecoveryDecision.ABORT_METHOD,
        )
        thread._resume_event.set.assert_not_called()


class TestAnAbortedThreadOffersNothing:
    """An ABORT_THREAD keeps `last_error` and the site on purpose, so the
    thread can still say what aborted it. It honours nothing: every verb is
    refused on status, and five surfaces promise the list is empty."""

    def test_a_thread_no_longer_paused_honours_nothing(self) -> None:
        thread = _paused_at(PauseSite.MOVE, _move_to("flex_1/C1-slot"))
        thread.status = LabwareThreadStatus.ABORTED
        thread.is_error_paused = False

        assert _honoured_decisions_of(thread) == []

    def test_an_error_paused_thread_still_offers_its_verbs(self) -> None:
        """Negative control."""
        thread = _paused_at(PauseSite.MOVE, _move_to("flex_1/C1-slot"))

        assert _honoured_decisions_of(thread) != []

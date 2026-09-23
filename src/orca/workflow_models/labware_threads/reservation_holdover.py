"""``ReservationHoldover``: collaborator that owns the deferred device
reservation kept attached to the previous action so consecutive
same-device actions in the same method do not thrash the reservation.
"""
import logging
from typing import Collection

from orca.resource_models.location import Location
from orca.workflow_models.actions.executable_location_action import ExecutableLocationAction
from orca.workflow_models.actions.location_action import ResidencyCheck

orca_logger = logging.getLogger("orca")


class ReservationHoldover:
    """Holds the deferred device reservation from the prior action so
    consecutive same-device actions do not thrash the reservation. Owned
    by ``ExecutingLabwareThread``; released at boundary transitions.

    Release semantics split by whether the owner is DEPARTING or STAYING.
    Departure sites (method exhausted,
    next-action-elsewhere, thread completion) release WHEN DRAINED -- the
    mutex stays held until no non-resident occupant remains on owned
    sites, so leftovers' exit hops stay membership-sanctioned (without
    this a successor's member gate strands them: the batch livelock).
    Staying sites (cooperative pause, park/join yield) release
    IMMEDIATELY -- the owner is not leaving, re-acquires on resume, and a
    deferred release there over-holds the device (a paused thread would
    lock out siblings) or goes sticky (a join-status flip fires no PICK
    to re-check the drain).
    """

    def __init__(
        self, residency_check: ResidencyCheck | None = None
    ) -> None:
        self._previous_action: ExecutableLocationAction | None = None
        # Holds handed to the drain check but not yet released. A thread can
        # leave several behind, one device per method.
        self._draining_actions: list[ExecutableLocationAction] = []
        self._residency_check = residency_check

    def current(self) -> ExecutableLocationAction | None:
        return self._previous_action

    def has_current(self) -> bool:
        return self._previous_action is not None

    def release_current(self) -> None:
        if self._previous_action is None:
            return
        self._previous_action.action.release_reservation()
        self._previous_action = None

    def release_current_when_drained(self) -> None:
        """Hand the hold to the manager's drain check and stop treating it as
        current.

        The action is remembered until the thread has actually departed. A
        thread that dies on its way out would otherwise leave the device
        reserved behind a predicate that can never pass, because a dead thread
        is not resident and its plate is still on the deck. Remembered BEFORE
        the release call, which can raise.
        """
        if self._previous_action is None:
            return
        action = self._previous_action
        self._previous_action = None
        self._draining_actions.append(action)
        action.action.release_when_drained(self._residency_check)

    def settle_drains_on_departure(self) -> None:
        """The thread has reached its end location. Release every hold whose
        drain has since passed, rather than leaving a free device looking
        reserved until somebody happens to ask for it.

        A hold still waiting on a non-resident occupant is doing its job and is
        left to the manager. Either way the thread stops tracking it, so its
        terminal cleanup cannot cut a live drain short.

        One hold per device, each answerable on its own, so a check that cannot
        answer holds back only its own: keeping the rest would have terminal
        cleanup release them ungated, which is the thing this whole mechanism
        exists to avoid.
        """
        unanswerable: list[ExecutableLocationAction] = []
        for action in self._draining_actions:
            try:
                check = action.action.reservation.pending_drain_check
                if check is not None and check(None):
                    action.action.reservation.release_reservation()
            except Exception:
                orca_logger.exception(
                    "Drain check failed on departure; leaving that hold to the "
                    "thread's terminal cleanup"
                )
                unanswerable.append(action)
        self._draining_actions = unanswerable

    def maybe_release_for_next_action(
        self,
        valid_locations: Collection[Location],
    ) -> None:
        """Release (drain-gated: the owner is departing) if the held
        action's location is not among the candidates for the next
        action. Lets the deadlock detector break cross-device cycles
        when continuation is blocked.
        """
        if self._previous_action is None:
            return
        if self._previous_action.action.location not in valid_locations:
            self.release_current_when_drained()

    def acquire_after_action(
        self,
        assigned_action: ExecutableLocationAction,
        owns_reservation: bool,
    ) -> None:
        """Replace the holdover with ``assigned_action``. Non-owners
        (contributors in shared methods) don't hold.
        """
        if self._previous_action is not None:
            self._previous_action.action.release_reservation()
        self._previous_action = assigned_action if owns_reservation else None

    def release_after_move_if_drained(self) -> None:
        """Drop the held reservation once the move leaves none of the action's
        own outputs on the device except residents.

        Residents are labware that never comes off: reused reagents, immovable
        labware, a stayer holding its place for the next contribution. Waiting
        for them to leave waits for a departure that will never happen, so a
        device carrying one reagent trough never released here at all.
        """
        if self._previous_action is None:
            return
        action = self._previous_action.action
        action.set_residency_check(self._residency_check)
        if not action.only_residents_remain():
            return
        self.release_current()

    def force_release(self) -> None:
        """Terminal-cleanup variant: drops the live hold AND any hold left
        waiting to drain. A release the manager already ran is inert here
        (it neuters the callback on the way out). Swallows ``ValueError``
        from already-released reservations so one failed release doesn't
        block the others in the same teardown.
        """
        pending = list(self._draining_actions)
        if self._previous_action is not None:
            pending.append(self._previous_action)
        for action in pending:
            try:
                action.action.release_reservation()
            except ValueError:
                pass
        self._previous_action = None
        self._draining_actions.clear()

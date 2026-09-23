"""Unit tests for the structural stall detector.

The detector answers one question from a snapshot of live-thread statuses:
is the system in a state from which no forward progress is possible? It is
timing-independent -- a genuinely long action keeps its thread ``in-flight``,
so the only signal is structural (every live thread internally blocked), never
a wall-clock cap. See ``StallDetector`` for the classification rationale.
"""
import pytest

from orca.runtime.stall_detector import (
    StallDetector,
    ThreadStallSnapshot,
    is_live,
    is_stall_candidate_wait,
    snapshot_in_flight,
)
from orca.workflow_models.status_enums import LabwareThreadStatus as S


def _snap(thread_id: str, status: S, waiting_on: str | None = None) -> ThreadStallSnapshot:
    return ThreadStallSnapshot(thread_id=thread_id, status=status, waiting_on=waiting_on)


def _blocked(thread_id: str, waiting_on: str) -> ThreadStallSnapshot:
    return _snap(thread_id, S.AWAITING_CO_THREADS, waiting_on)


class TestClassification:
    def test_non_candidate_waits(self) -> None:
        # Reservation waits are the reservation manager's domain, not stall
        # candidates; in-flight / external / terminal are obviously not either.
        for status in (S.EXECUTING_ACTION, S.MOVING, S.STOPPING, S.CREATED,
                       S.ACTION_LOCATION_RESOLVED, S.PAUSED, S.AWAITING_MANUAL_PLACE,
                       S.AWAITING_MANUAL_REMOVE, S.AWAITING_EVENT, S.COMPLETED,
                       S.ABORTED, S.STOPPED, S.AWAITING_ACTION_RESERVATION,
                       S.AWAITING_MOVE_RESERVATION, S.AWAITING_MOVE_TARGET_AVAILABILITY,
                       S.RESOLVING_ACTION_LOCATION):
            assert not is_stall_candidate_wait(status), status

    def test_co_labware_is_the_only_stall_candidate(self) -> None:
        assert is_stall_candidate_wait(S.AWAITING_CO_THREADS)

    def test_terminal_statuses_are_not_live(self) -> None:
        for status in (S.COMPLETED, S.STOPPED, S.ABORTED):
            assert not is_live(status), status
        assert is_live(S.AWAITING_CO_THREADS)
        assert is_live(S.EXECUTING_ACTION)


class TestStallDecision:
    def test_no_threads_is_not_a_stall(self) -> None:
        d = StallDetector()
        assert d.evaluate([]) is None
        assert d.evaluate([]) is None

    def test_any_in_flight_thread_blocks_stall(self) -> None:
        d = StallDetector()
        snaps = [_blocked("a", "tips"), _snap("b", S.EXECUTING_ACTION)]
        assert d.evaluate(snaps) is None
        assert d.evaluate(snaps) is None

    def test_requires_stability_across_two_ticks(self) -> None:
        d = StallDetector()
        snaps = [_blocked("a", "plate_b"), _blocked("b", "plate_a")]
        assert d.evaluate(snaps) is None            # tick 1: candidate, not yet stable
        report = d.evaluate(snaps)                  # tick 2: stable -> stall
        assert report is not None
        assert set(report.thread_ids) == {"a", "b"}
        assert "a" in report.detail and "b" in report.detail

    def test_paused_thread_prevents_stall(self) -> None:
        d = StallDetector()
        snaps = [_blocked("a", "plate_b"), _snap("b", S.PAUSED)]
        assert d.evaluate(snaps) is None
        assert d.evaluate(snaps) is None

    def test_awaiting_event_prevents_stall(self) -> None:
        d = StallDetector()
        snaps = [_blocked("a", "plate_b"), _snap("b", S.AWAITING_EVENT, "sensor")]
        assert d.evaluate(snaps) is None
        assert d.evaluate(snaps) is None

    def test_progress_between_ticks_resets_stability(self) -> None:
        d = StallDetector()
        blocked = [_blocked("a", "plate_b"), _blocked("b", "plate_a")]
        assert d.evaluate(blocked) is None          # tick 1: candidate
        # tick 2: b advanced to EXECUTING_ACTION -> progress -> no stall, reset
        assert d.evaluate([_blocked("a", "plate_b"), _snap("b", S.EXECUTING_ACTION)]) is None
        # tick 3: all blocked again, but this is only the 1st stable tick post-reset
        assert d.evaluate(blocked) is None
        # tick 4: now stable -> stall
        assert d.evaluate(blocked) is not None

    def test_changed_wait_subject_resets_stability(self) -> None:
        d = StallDetector()
        assert d.evaluate([_blocked("a", "plate_b"), _blocked("b", "plate_a")]) is None
        # same threads still all-blocked, but what they wait on changed -> churn, not frozen
        assert d.evaluate([_blocked("a", "shaker_1"), _blocked("b", "plate_a")]) is None
        assert d.evaluate([_blocked("a", "shaker_1"), _blocked("b", "plate_a")]) is not None

    def test_terminal_threads_ignored_stall_on_blocked_remainder(self) -> None:
        d = StallDetector()
        snaps = [_blocked("a", "plate_b"), _blocked("b", "plate_a"), _snap("done", S.COMPLETED)]
        assert d.evaluate(snaps) is None
        assert d.evaluate(snaps) is not None

    def test_co_labware_orphan_single_thread_stalls(self) -> None:
        # One thread parked at co-labware for labware whose producer already died:
        # the sole live thread is internally blocked with nothing to unblock it.
        d = StallDetector()
        snaps = [_blocked("owner", "tips_detection")]
        assert d.evaluate(snaps) is None
        report = d.evaluate(snaps)
        assert report is not None
        assert report.thread_ids == ("owner",)
        assert "tips_detection" in report.detail

    def test_configurable_required_ticks(self) -> None:
        d = StallDetector(required_stable_ticks=3)
        snaps = [_blocked("a", "plate_b"), _blocked("b", "plate_a")]
        assert d.evaluate(snaps) is None
        assert d.evaluate(snaps) is None
        assert d.evaluate(snaps) is not None

    def test_reservation_contention_never_stalls(self) -> None:
        # Regression: concurrent submissions contending for shared devices sit in
        # reservation waits (the reservation manager's domain) -- never a stall.
        d = StallDetector()
        snaps = [
            _snap("a", S.AWAITING_ACTION_RESERVATION, "shaker1"),
            _snap("b", S.AWAITING_MOVE_RESERVATION, "pad1"),
        ]
        for _ in range(5):
            assert d.evaluate(snaps) is None

    def test_mixed_co_labware_and_reservation_stalls_when_stable(self) -> None:
        """A reservation is only released by a thread that acts. With every live
        thread waiting (co-labware or reservation) and none in flight, no release
        or delivery can ever occur, so the mixed stable state cannot self-resolve.
        The earlier assertion ("the reservation may yet grant") encoded the
        cross-mechanism blind spot: a co-labware waiter holding what a
        reservation waiter needs escaped both this detector and the
        reservation-layer deadlock detector."""
        d = StallDetector()
        snaps = [_blocked("a", "plate_b"), _snap("b", S.AWAITING_MOVE_RESERVATION, "pad1")]
        assert d.evaluate(snaps) is None
        report = d.evaluate(snaps)
        assert report is not None
        assert set(report.thread_ids) == {"a", "b"}

    def test_reports_once_per_episode(self) -> None:
        # Fires once when the stall settles, then stays silent (no incident spam)
        # until the stall clears and a distinct one settles again.
        d = StallDetector()
        snaps = [_blocked("a", "plate_b"), _blocked("b", "plate_a")]
        assert d.evaluate(snaps) is None            # tick 1
        assert d.evaluate(snaps) is not None        # tick 2: fires
        assert d.evaluate(snaps) is None            # tick 3: already reported
        assert d.evaluate(snaps) is None            # tick 4: still silent
        # a thread progresses, then everything re-stalls -> a fresh episode fires
        assert d.evaluate([_snap("a", S.EXECUTING_ACTION), _blocked("b", "plate_a")]) is None
        assert d.evaluate(snaps) is None            # new episode tick 1
        assert d.evaluate(snaps) is not None        # new episode tick 2: fires again


_RESERVATION_WAIT_STATUSES = (
    S.AWAITING_ACTION_RESERVATION,
    S.AWAITING_MOVE_RESERVATION,
    S.AWAITING_MOVE_TARGET_AVAILABILITY,
    S.RESOLVING_ACTION_LOCATION,
)


class TestMixedStallDecision:
    """Mixed rule: every live thread waiting (co-labware or reservation), at
    least one of each, stable -> stall. All-reservation shapes stay the
    reservation manager's domain and never stall here."""

    @pytest.mark.parametrize("res_status", _RESERVATION_WAIT_STATUSES)
    def test_mixed_stalls_for_every_reservation_wait_kind(self, res_status: S) -> None:
        d = StallDetector()
        snaps = [_blocked("a", "plate_b"), _snap("b", res_status, "device1")]
        assert d.evaluate(snaps) is None
        report = d.evaluate(snaps)
        assert report is not None, res_status
        assert set(report.thread_ids) == {"a", "b"}

    def test_all_reservation_waits_never_stall_without_a_co_waiter(self) -> None:
        # Without a co-labware waiter this is plain contention: the reservation
        # manager progresses on its own; genuine cycles are the deadlock detector's.
        d = StallDetector()
        snaps = [_snap(f"t{i}", status, "device1")
                 for i, status in enumerate(_RESERVATION_WAIT_STATUSES)]
        for _ in range(5):
            assert d.evaluate(snaps) is None

    def test_mixed_with_in_flight_thread_is_not_a_stall(self) -> None:
        d = StallDetector()
        snaps = [
            _blocked("a", "plate_b"),
            _snap("b", S.AWAITING_MOVE_RESERVATION, "pad1"),
            _snap("c", S.EXECUTING_ACTION),
        ]
        assert d.evaluate(snaps) is None
        assert d.evaluate(snaps) is None

    def test_mixed_with_paused_thread_is_not_a_stall(self) -> None:
        d = StallDetector()
        snaps = [
            _blocked("a", "plate_b"),
            _snap("b", S.AWAITING_MOVE_RESERVATION, "pad1"),
            _snap("c", S.PAUSED),
        ]
        assert d.evaluate(snaps) is None
        assert d.evaluate(snaps) is None

    def test_reservation_wait_churn_resets_stability(self) -> None:
        d = StallDetector()
        tick1 = [_blocked("a", "plate_b"), _snap("b", S.AWAITING_MOVE_RESERVATION, "pad1")]
        assert d.evaluate(tick1) is None
        # b's wait kind changed between ticks: the reservation layer is still
        # working the request -> churn, not frozen; stability restarts.
        tick2 = [_blocked("a", "plate_b"), _snap("b", S.AWAITING_ACTION_RESERVATION, "pad1")]
        assert d.evaluate(tick2) is None
        assert d.evaluate(tick2) is not None

    def test_mixed_report_details_name_each_wait(self) -> None:
        d = StallDetector()
        snaps = [_blocked("owner", "tips"), _snap("mover", S.AWAITING_MOVE_RESERVATION, "pad1")]
        d.evaluate(snaps)
        report = d.evaluate(snaps)
        assert report is not None
        assert "tips" in report.detail and "pad1" in report.detail
        assert "AWAITING_MOVE_RESERVATION" in report.detail


class TestParkedContributor:
    """A contributor in a shared action is parked on the OWNER, not acting.

    Its status reads EXECUTING_ACTION because the action it belongs to is the
    one running, but the thread itself only awaits the owner's outcome: it
    cannot deliver co-labware and cannot release a reservation. Reading it as
    in-flight let a real wedge sit undetected until an external timeout.
    """

    def test_parked_contributor_is_not_in_flight(self) -> None:
        parked = ThreadStallSnapshot(
            thread_id="trough", status=S.EXECUTING_ACTION,
            waiting_on="dilute", following_peer_action=True,
        )
        assert not snapshot_in_flight(parked)
        assert snapshot_in_flight(_snap("owner", S.EXECUTING_ACTION))

    def test_parked_contributor_does_not_mask_a_stall(self) -> None:
        d = StallDetector()
        snaps = [
            _blocked("owner", "tips"),
            _snap("mover", S.AWAITING_MOVE_RESERVATION, "pad1"),
            ThreadStallSnapshot(
                thread_id="trough", status=S.EXECUTING_ACTION,
                waiting_on="dilute", following_peer_action=True,
            ),
        ]
        assert d.evaluate(snaps) is None
        report = d.evaluate(snaps)
        assert report is not None
        assert set(report.thread_ids) == {"owner", "mover", "trough"}

    def test_owner_actually_executing_still_blocks_the_stall(self) -> None:
        # The contributor is parked, but its owner is genuinely driving the
        # device: the action will finish and release, so this is not a stall.
        d = StallDetector()
        snaps = [
            _snap("owner", S.EXECUTING_ACTION),
            ThreadStallSnapshot(
                thread_id="trough", status=S.EXECUTING_ACTION,
                waiting_on="dilute", following_peer_action=True,
            ),
        ]
        assert d.evaluate(snaps) is None
        assert d.evaluate(snaps) is None

    def test_a_paused_contributor_still_blocks_the_verdict(self) -> None:
        # A failed shared action pauses its whole group. The contributor is
        # still parked, but PAUSED is an outside wait: the operator's recovery
        # decision can move it, so it must not count toward a stall.
        d = StallDetector()
        snaps = [
            _blocked("owner", "tips"),
            ThreadStallSnapshot(
                thread_id="trough", status=S.PAUSED,
                waiting_on="dilute", following_peer_action=True,
            ),
        ]
        assert d.evaluate(snaps) is None
        assert d.evaluate(snaps) is None

    def test_parking_and_unparking_resets_stability(self) -> None:
        d = StallDetector()
        parked = ThreadStallSnapshot(
            thread_id="trough", status=S.EXECUTING_ACTION,
            waiting_on="dilute", following_peer_action=True,
        )
        acting = _snap("trough", S.EXECUTING_ACTION, "dilute")
        assert d.evaluate([_blocked("owner", "tips"), parked]) is None
        # The contributor started acting between ticks: progress, so the count
        # restarts rather than carrying a stale stable tick forward.
        assert d.evaluate([_blocked("owner", "tips"), acting]) is None
        assert d.evaluate([_blocked("owner", "tips"), parked]) is None
        assert d.evaluate([_blocked("owner", "tips"), parked]) is not None

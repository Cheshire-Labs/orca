"""``SharedActionCoordination``: the cohesive coordination object for the threads
sharing ONE shared action.

Scope boundary (do not blur with ``SharedMethodCoordination``):

- ``SharedMethodCoordination`` is **method-scoped** -- one per ``ExecutingMethod``.
  It owns the join contributor registry, the ``resolving_action_lock``, and
  ``current_action_resolved``. "Who joined this method."
- ``SharedActionCoordination`` is **action-scoped** -- one minted per shared-action
  slot. It owns the group operations (pause, op-pause context, recovery decision,
  outcome) that must apply to every thread converged on THAT action as a unit.

The two are siblings under ``ExecutingMethod``; neither references the other. The
group's cohesion is carried by broadcast signals -- an ``asyncio.Event`` every thread
awaits and a set-once outcome future every thread fans out to -- not a roster: there is
no membership list to keep consistent under concurrent joins.
"""
import asyncio
from dataclasses import dataclass

from orca.workflow_models.status_enums import PauseSite, RecoveryDecision


@dataclass(frozen=True)
class ActionResolution:
    """Terminal outcome of one shared action, published exactly once and read by
    every participant. ``decision is None`` means the action completed cleanly; a
    non-None decision is a terminal decision (CONTINUE / ABORT_ACTION /
    ABORT_METHOD / ABORT_THREAD), usually an operator's, but a cooperative stop
    publishes ABORT_THREAD with no operator involved. RETRY
    and RETRY_ACTION are never an outcome: they re-drive in place, so the outcome future stays
    pending."""

    decision: RecoveryDecision | None


class SharedActionCoordination:
    """Action-scoped coordination for the threads sharing ONE action.

    Minted per slot before the action binds so a pre-binding failure can still
    resolve it. Owns the group signals:

    - ``action_paused``: set when the owner fails the action, so every thread pauses
      as a unit (the operator sees the whole action stuck, not just one thread).
    - ``step_paused_site``: DEVICE_OP, COMMAND_SAVE or ACTION_WAIT while the group is suspended on one
      step inside the action, else None; ``op_paused_command`` names the device op at the first two.
      Set only on the step seam, so a RETRY decision fed via ANY participant is valid --
      not just the owner that physically drives the step.
    - ``decision`` (set-once via ``submit_decision``, first wins): the single
      authoritative recovery decision, fed by ANY participant and consumed by the owner.
      Recovering more than one paused participant is idempotent; none can half-resume.
    - ``outcome``: the terminal ``ActionResolution`` the owner publishes after it
      executes the decision; every participant fans out to it.
    """

    def __init__(self) -> None:
        self.action_paused: asyncio.Event = asyncio.Event()
        self.decision_ready: asyncio.Event = asyncio.Event()
        self._decision: RecoveryDecision | None = None
        self._op_paused_command: str | None = None
        self._step_paused_site: PauseSite | None = None
        self._outcome: asyncio.Future[ActionResolution] = (
            asyncio.get_running_loop().create_future()
        )

    def mark_paused(self) -> None:
        """Owner-driven: the action failed, so signal every participant to pause."""
        self.action_paused.set()

    def mark_step_paused(self, site: PauseSite, device_command: str | None) -> None:
        """Record the step inside the action the group is suspended on, so a RETRY decision fed via ANY
        participant (not only the owner) is judged against it."""
        self._step_paused_site = site
        self._op_paused_command = device_command

    def clear_step_paused(self) -> None:
        """Clear it once the owner leaves the step seam (a decision was applied or the step re-drives)."""
        self._step_paused_site = None
        self._op_paused_command = None

    @property
    def op_paused_command(self) -> str | None:
        return self._op_paused_command

    @property
    def step_paused_site(self) -> PauseSite | None:
        return self._step_paused_site

    def submit_decision(self, decision: RecoveryDecision) -> None:
        """Feed the single authoritative recovery decision from ANY participant.
        First wins; later calls are no-ops, so recovering multiple paused participants
        is idempotent and the owner is the sole executor of the decision."""
        if self.decision_ready.is_set():
            return
        self._decision = decision
        self.decision_ready.set()

    @property
    def decision(self) -> RecoveryDecision | None:
        return self._decision

    def reset_decision(self) -> None:
        """Clear the decision channel so a RETRY re-drive can take a fresh decision on
        the next failure. The outcome future is untouched: participants stay paused,
        awaiting the eventual resolution of this same action."""
        self._decision = None
        self.decision_ready.clear()

    @property
    def outcome(self) -> asyncio.Future[ActionResolution]:
        return self._outcome

    def publish_outcome(self, outcome: ActionResolution) -> None:
        """Resolve the action outcome exactly once; every participant fans out to it.
        Idempotent: overlapping owner-exit paths are safe and the first outcome wins."""
        if not self._outcome.done():
            self._outcome.set_result(outcome)

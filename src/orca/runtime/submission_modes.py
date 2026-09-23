"""Submission status and batch mode, on their own so the wire can read them.

Lives apart from `submission.py` because that module pulls in labware and
location models, and a caller that only names a status would pay for them.
"""

from enum import Enum


class SubmissionStatus(str, Enum):
    """Lifecycle states a Submission moves through.

    Forward path:
      PENDING -> ACCEPTED -> IN_PROGRESS -> {COMPLETED, FAILED, ABORTED}

    State semantics:
      PENDING: dataclass default; never observed on the wire because
        ``SystemRuntime.submit`` immediately overrides to ACCEPTED on
        successful validation.
      ACCEPTED: validated, queued. The execution task has been created
        but no entry thread has begun yet.
      IN_PROGRESS: the execution's workflow status is IN_PROGRESS;
        entry threads have been scheduled and are running. For injected
        (JOIN_EXISTING) submissions, this fires when the new entry
        threads register on the live executing workflow.
      COMPLETED: the owning execution reached ExecutionPhase.COMPLETED
        (every thread terminated cleanly).
      FAILED: the owning execution reached ExecutionPhase.FAILED
        (workflow task raised an exception).
      ABORTED: the owning execution reached ExecutionPhase.ABORTED
        (the run had begun, then was stopped: operator abort or task
        cancellation). A submission stopped before any thread begins is
        still ABORTED -- there is no separate pre-begin "cancelled" state.

    Terminal states (COMPLETED / FAILED / ABORTED) are sticky: once
    set, the submission status will not transition again.
    """
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


class BatchMode(str, Enum):
    """Operator's preference for how a submission interacts with in-flight receivers.

    STANDALONE: this submission creates its own receivers. Slot keys always
    include submission_id, so later submissions cannot discover these slots.
    JOIN_EXISTING: for BATCHABLE templates, this submission's callback uses
    slot keys without submission_id, discovering and joining any existing
    matching slot with room.
    """
    STANDALONE = "STANDALONE"
    JOIN_EXISTING = "JOIN_EXISTING"

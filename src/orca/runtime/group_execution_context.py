"""GroupExecutionContext: the context T6 passes through slot_key_for.

Separate from ExecutionContext (which is the workflow/thread/method hierarchy
used for event emission). GroupExecutionContext is the per-callback context
that tells the registry how to compose a slot key for a given group +
submission + batch mode.
"""

from dataclasses import dataclass

from orca.runtime.submission_modes import BatchMode


@dataclass(frozen=True)
class GroupExecutionContext:
    """Context a contributor thread carries that influences slot keying.

    group_id/submission_id come from the entry-thread tagging (or are
    inherited by auto-spawned threads from the firing contributor).
    batch_mode is the operator preference recorded on the Submission; T6f
    uses it together with the template's SubmissionBatching flag to decide
    whether a BATCHABLE receiver's slot key collapses submission_id to "*".
    """
    group_id: str | None      # None for SHARED_ACROSS_GROUPS threads
    submission_id: str
    batch_mode: BatchMode = BatchMode.STANDALONE

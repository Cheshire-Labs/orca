"""Which submissions a BATCHABLE receiver is shared with, and which it is not.

Semantics matrix:
- ISOLATED template + any BatchMode -> submission_id stays in the key.
- BATCHABLE template + any BatchMode -> submission_id collapses to '*'.

Batch mode decides whether a submission joins a live execution at all; once it
is inside one, a BATCHABLE template means one receiver for everybody there.
Each execution builds its own registry, so a STANDALONE submission -- which
always boots a fresh execution -- never meets anyone else's key.
"""

from unittest.mock import Mock

from orca.resource_models.labware import PlateTemplate
from orca.resource_models.sharing import GroupSharing, SubmissionBatching
from orca.runtime.group_aware_labware_registry import GroupAwareLabwareRegistry
from orca.runtime.group_execution_context import GroupExecutionContext
from orca.runtime.submission import BatchMode


def _make_template(
    name: str,
    group_sharing: GroupSharing,
    submission_batching: SubmissionBatching,
) -> Mock:
    labware_template = PlateTemplate(
        name,
        labware_type="_test_placeholder",
        group_sharing=group_sharing,
        submission_batching=submission_batching,
    )
    thread_template = Mock()
    thread_template.labware_template = labware_template
    thread_template.start_reuse_existing = False
    return thread_template


def test_batchable_join_existing_collapses_submission_id() -> None:
    registry = GroupAwareLabwareRegistry()
    template = _make_template(
        "final_plate",
        GroupSharing.SHARED_ACROSS_GROUPS,
        SubmissionBatching.BATCHABLE,
    )
    ctx_1 = GroupExecutionContext(
        group_id="g-A", submission_id="sub-1", batch_mode=BatchMode.JOIN_EXISTING,
    )
    ctx_2 = GroupExecutionContext(
        group_id="g-B", submission_id="sub-2", batch_mode=BatchMode.JOIN_EXISTING,
    )

    assert registry.slot_key_for(template, ctx_1) == "final_plate:*:*"
    assert registry.slot_key_for(template, ctx_2) == "final_plate:*:*"


def test_batchable_standalone_shares_the_one_receiver() -> None:
    """A BATCHABLE template is one receiver for every submission in the
    execution. STANDALONE is not isolation here: it means "boot me a fresh
    execution", and inside that execution the only other submission ids belong
    to joiners that came to share this very plate.
    """
    registry = GroupAwareLabwareRegistry()
    template = _make_template(
        "final_plate",
        GroupSharing.SHARED_ACROSS_GROUPS,
        SubmissionBatching.BATCHABLE,
    )
    ctx_1 = GroupExecutionContext(
        group_id="g-A", submission_id="sub-1", batch_mode=BatchMode.STANDALONE,
    )
    ctx_2 = GroupExecutionContext(
        group_id="g-B", submission_id="sub-2", batch_mode=BatchMode.STANDALONE,
    )

    assert registry.slot_key_for(template, ctx_1) == "final_plate:*:*"
    assert registry.slot_key_for(template, ctx_2) == "final_plate:*:*"


def test_isolated_template_ignores_join_existing() -> None:
    registry = GroupAwareLabwareRegistry()
    template = _make_template(
        "sample_plate",
        GroupSharing.PER_GROUP,
        SubmissionBatching.ISOLATED,
    )
    ctx = GroupExecutionContext(
        group_id="g-A", submission_id="sub-1", batch_mode=BatchMode.JOIN_EXISTING,
    )

    # ISOLATED wins: submission_id stays in the key even under JOIN_EXISTING.
    assert registry.slot_key_for(template, ctx) == "sample_plate:g-A:sub-1"


def test_a_joiner_reaches_the_receiver_a_standalone_submission_started() -> None:
    """The whole point of joining: one physical plate, one receiver. The joiner
    resolves to the key the first submission already composed, so the two do not
    end up with a receiver each and deadlock over the plate's single pad.
    """
    registry = GroupAwareLabwareRegistry()
    template = _make_template(
        "final_plate",
        GroupSharing.SHARED_ACROSS_GROUPS,
        SubmissionBatching.BATCHABLE,
    )
    first = GroupExecutionContext(
        group_id=None, submission_id="sub-1", batch_mode=BatchMode.STANDALONE,
    )
    later_join = GroupExecutionContext(
        group_id=None, submission_id="sub-2", batch_mode=BatchMode.JOIN_EXISTING,
    )

    assert registry.slot_key_for(template, later_join) == registry.slot_key_for(template, first)


def test_the_shared_key_does_not_depend_on_who_resolves_first() -> None:
    """Whichever submission's thread spawns first, both land on the same key.

    A rule that read the live slots instead made the answer depend on the spawn
    race: the joiner could resolve before anything was in flight to join.
    """
    registry = GroupAwareLabwareRegistry()
    template = _make_template(
        "final_plate",
        GroupSharing.SHARED_ACROSS_GROUPS,
        SubmissionBatching.BATCHABLE,
    )
    join_first = GroupExecutionContext(
        group_id=None, submission_id="sub-2", batch_mode=BatchMode.JOIN_EXISTING,
    )
    standalone_second = GroupExecutionContext(
        group_id=None, submission_id="sub-1", batch_mode=BatchMode.STANDALONE,
    )

    joiner_key = registry.slot_key_for(template, join_first)
    registry.get_or_create_slot(joiner_key, "final_plate")
    assert registry.slot_key_for(template, standalone_second) == joiner_key


def test_an_isolated_template_gives_a_joining_submission_its_own_receiver() -> None:
    """Joining an execution does not make an ISOLATED template shared: the
    template, not the submission, decides whether the plate is one or many.
    """
    registry = GroupAwareLabwareRegistry()
    template = _make_template(
        "sample_plate",
        GroupSharing.SHARED_ACROSS_GROUPS,
        SubmissionBatching.ISOLATED,
    )
    first = GroupExecutionContext(
        group_id=None, submission_id="sub-1", batch_mode=BatchMode.STANDALONE,
    )
    later_join = GroupExecutionContext(
        group_id=None, submission_id="sub-2", batch_mode=BatchMode.JOIN_EXISTING,
    )

    assert registry.slot_key_for(template, first) == "sample_plate:*:sub-1"
    assert registry.slot_key_for(template, later_join) == "sample_plate:*:sub-2"

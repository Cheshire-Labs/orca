"""Group-aware labware registry.

Extends InMemoryLabwareRegistry with a composite slot_key_for that includes
group_id and submission_id components, keyed off the thread template's
LabwareTemplate sharing flags and the current GroupExecutionContext.

Key format: "{labware_template_name}:{group_id_or_*}:{submission_id_or_*}".

Resolution rules:
- A start_reuse_existing (deck-resident) thread is one physical singleton, so
  its key is the bare labware name -- every group and submission shares the one
  receiver slot. This precedes the sharing-flag rules below.
- GroupSharing.SHARED_ACROSS_GROUPS collapses the group component to "*"
  so all groups in a submission share one slot. (T6d)
- GroupSharing.PER_GROUP keeps the group_id component (each group gets its
  own slot). (T6d)
- SubmissionBatching.BATCHABLE collapses the submission component to "*", so
  every submission sharing the execution reaches the one receiver. Batch mode
  does not enter the key: a STANDALONE submission always boots a fresh
  execution and each execution builds its own registry, so the only way two
  submission ids meet in one registry is a JOIN_EXISTING that came to share.
  Keying by submission there gave the joiner a second receiver for what is one
  physical plate, and the two deadlocked over its single pad.
- SubmissionBatching.ISOLATED keeps submission_id in the key, so a later
  submission gets its own receiver whatever mode it submitted under.
"""

from orca.resource_models.labware_state import InMemoryLabwareRegistry
from orca.resource_models.sharing import GroupSharing, SubmissionBatching
from orca.runtime.group_execution_context import GroupExecutionContext
from orca.workflow_models.thread_template import ThreadTemplate


_WILDCARD = "*"
"""Sentinel for missing group_id/submission_id in slot-key composition.

The slot_key is an internal dict-key for the labware registry; group_id and
submission_id are legitimately Optional in the domain (legacy single-shot
submissions, no-group registrations). A composite string-key requires SOME
representation of "no group" / "no submission"; ``*`` is the wildcard
convention. Centralized here so the value isn't reinvented at call sites.

A follow-up refactor would replace the str slot_key with a tuple key
end-to-end (``tuple[str, str | None, str | None]``), eliminating the
sentinel entirely. The current str-keyed protocol crosses too many
modules to fold into the strict-strings cleanup.
"""


def _compose_slot_key(labware_name: str, group_id: str | None, submission_id: str | None) -> str:
    group_part = group_id if group_id is not None else _WILDCARD
    submission_part = submission_id if submission_id is not None else _WILDCARD
    for component in (labware_name, group_part, submission_part):
        if ":" in component:
            raise ValueError(
                f"slot-key component {component!r} must not contain ':'; it "
                "delimits the labware:group:submission slot key."
            )
    return f"{labware_name}:{group_part}:{submission_part}"


def _decompose_slot_scope(slot_key: str) -> tuple[str | None, str | None]:
    """Recover (group_scope, submission_scope) from a composed slot key.

    A ``*`` component (or an unscoped bare labware-name key) yields None,
    meaning "matches any group/submission" -- a shared receiver is held open
    by every contributor. A concrete component scopes the slot to that group/
    submission so a sibling submission's threads cannot hold it open.
    """
    parts = slot_key.split(":")
    if len(parts) != 3:
        return None, None
    _, group_part, submission_part = parts
    group_scope = None if group_part == _WILDCARD else group_part
    submission_scope = None if submission_part == _WILDCARD else submission_part
    return group_scope, submission_scope


class GroupAwareLabwareRegistry(InMemoryLabwareRegistry):
    """LabwareRegistry that composes a per-(template, group, submission) slot key."""

    def slot_key_for(self, template: ThreadTemplate,
                     context: object | None = None) -> str:
        labware_template = template.labware_template
        labware_name = labware_template.name

        # One physical deck-resident instance is shared by every group/submission;
        # a per-group key would double-start the singleton resident thread.
        if template.start_reuse_existing:
            return labware_name

        if not isinstance(context, GroupExecutionContext):
            return labware_name

        if labware_template.group_sharing is GroupSharing.SHARED_ACROSS_GROUPS:
            group_component: str | None = None
        else:
            group_component = context.group_id

        if labware_template.submission_batching is SubmissionBatching.BATCHABLE:
            submission_component: str | None = None
        else:
            submission_component = context.submission_id

        return _compose_slot_key(
            labware_name,
            group_component,
            submission_component,
        )

    def slot_scope(self, slot_key: str) -> tuple[str | None, str | None]:
        return _decompose_slot_scope(slot_key)

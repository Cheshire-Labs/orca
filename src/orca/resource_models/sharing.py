"""Labware sharing semantics: two orthogonal axes declared on LabwareTemplate.

GroupSharing: within a single submission, is the labware per-group or shared?
SubmissionBatching: across submissions, may a receiver be reused?

Together they determine how slot_key_for composes a slot key. See the T6 plan
for the full matrix; these flags are the author's design-time declaration.
"""

from enum import Enum


class GroupSharing(str, Enum):
    """Within one submission, how is this labware shared across groups?"""

    PER_GROUP = "PER_GROUP"
    """Each LabwareGroup in the submission gets its own instance of this labware.
    Example: every sample plate has its own tip rack -- groups never share."""

    SHARED_ACROSS_GROUPS = "SHARED_ACROSS_GROUPS"
    """One instance of this labware is shared by every group in the submission.
    Example: a single 384-well final plate receives eluate from 4 sample groups."""


class SubmissionBatching(str, Enum):
    """Across submissions, may an in-flight receiver be joined by a later submission?"""

    ISOLATED = "ISOLATED"
    """This labware's receiver slot is closed when its submission terminates;
    a later submission cannot contribute to an in-flight instance. Use this
    when cross-submission mixing would be wrong (e.g. per-run control plates)."""

    BATCHABLE = "BATCHABLE"
    """An in-flight receiver may accept contributions from submissions that
    arrive after it spawned. Use this when a partially filled receiver should
    keep collecting feed from newly submitted groups (e.g. a 384-well final
    plate that fills over multiple 96-well sample submissions)."""

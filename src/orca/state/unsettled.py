"""What the record does not know, gathered in one place.

Every fact this module owns can be in a state that needs a person: nobody has
ever said, or something happened while nobody was watching. Scattered across
surfaces those read as unrelated warnings. Together they are a worklist, which
is what an operator walking up to a paused system actually wants.
"""

from dataclasses import dataclass
from typing import Literal

from orca.state.provenance import Provenance
from orca.state.records import GAPS_THAT_LOST_WORK, ObservationGapCause

SettleNoun = Literal["tip-state", "well-volumes", "mounted-tips"]
"""What the verb is about. The half of a settle verb the subject decides."""


@dataclass(frozen=True)
class UnsettledSubject:
    """One thing nobody has settled, and the verb that settles it."""

    subject: str
    """What it is about: a labware name, or a device name for a head."""

    subject_id: str | None
    """The id the settle verb takes, when that is not the subject itself.

    A labware verb takes `labware_id`, and two live instances may share a name,
    so a row naming only the name could not be acted on without a second read
    that might answer ambiguously. A head verb takes the device name, which is
    already the subject, so this is None there."""

    subject_kind: str
    """`labware` or `device_head`, so a surface can group without parsing."""

    provenance: Provenance
    """Why it is unsettled: UNKNOWN (nobody has ever said) or STALE (it was
    known, then a stretch passed with nobody watching). There used to be a
    second enum here spelling those two out again under different names, which
    meant two vocabularies for one distinction."""

    detail: str
    settle_with: str | None
    """The verb that answers it, named as an operator would type it. None when
    nothing does: an unfinished action is holding operations, and the record
    catches up only when that action ends."""


def is_unsettled(provenance: Provenance) -> bool:
    """Whether this state wants a person. KNOWN is settled; nothing is owed."""
    return provenance is not Provenance.KNOWN


def settle_verb(
    noun: SettleNoun, provenance: Provenance, *,
    unrecorded: bool,
    gaps: frozenset[ObservationGapCause],
    can_be_agreed_with: bool,
) -> str | None:
    """The verb that settles this subject, or None when no verb does.

    Confirm agrees with what the record holds, so it is the answer wherever the
    record is still the best value there is. Naming `set-tip-state` for a rack
    that merely went unwatched sends an operator to retype ninety-six positions
    the record already has right.

    Three cases take the `set` verb instead. Nobody has ever said, where there
    is nothing to agree with. An abort that lost work, where the record is
    short by an amount nothing can state and agreeing marks it checked. And a
    record with nothing the confirm verb can back, where confirm is refused
    outright even though the provenance looks agreeable -- an undeclared trough
    reads as having a baseline while its volumes fold to nothing.

    An unfinished action holding operations is the case neither verb answers.
    Confirming freezes a number that is behind. Stating one is worse, because
    the action's own operations are folded on top of it when it ends. Only
    ending the action helps, so this names no verb and the detail says why.
    """
    if unrecorded:
        return None
    if (
        provenance is Provenance.UNKNOWN
        or not can_be_agreed_with
        or gaps & GAPS_THAT_LOST_WORK
    ):
        return f"set-{noun}"
    return f"confirm-{noun}"

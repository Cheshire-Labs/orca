"""Why a confirm was refused. Each is a ValueError, so a plain catch still works."""

from typing import ClassVar


class ConfirmRefusedError(ValueError):
    wire_code: ClassVar[str] = "invalid_input"

    def __init__(self, message: str, *, settle_with: str | None = None) -> None:
        super().__init__(message)
        self.settle_with = settle_with
        """The verb that settles it, spelled as the unsettled worklist spells it
        (`set-tip-state`), or None when no single verb does."""


class NothingToConfirmError(ConfirmRefusedError):
    """Nothing has ever said what is there, so agreeing would assert an empty one."""
    wire_code = "nothing_to_confirm"


class HeldByUnfinishedActionError(ConfirmRefusedError):
    """An unfinished action holds operations the record has not folded yet."""
    wire_code = "held_by_unfinished_action"


class WorkLostError(ConfirmRefusedError):
    """An aborted action threw away work it really did; the read is wrong."""
    wire_code = "work_lost"

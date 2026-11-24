"""Why a confirm was refused. Each is a ValueError, so a plain catch still works."""


class ConfirmRefusedError(ValueError):
    def __init__(self, message: str, *, settle_with: str | None = None) -> None:
        super().__init__(message)
        self.settle_with = settle_with
        """The verb that settles it, spelled as the unsettled worklist spells it
        (`set-tip-state`), or None when no single verb does."""


class NothingToConfirmError(ConfirmRefusedError):
    """Nothing has ever said what is there, so agreeing would assert an empty one."""


class WorkLostError(ConfirmRefusedError):
    """A stop, a timeout or a failure cut a command off at the device, so the read may be wrong by an amount nothing can state."""

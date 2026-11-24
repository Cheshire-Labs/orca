"""What a server reports while it has no runtime: one blocker, and the last build error.

Both servers send the same words. Each passes its own remedy and its own hint for a build error.
"""

from collections.abc import Callable
from dataclasses import dataclass

from orca.runtime.blockers import Blocker, BlockerKind, BlockerSeverity
from orca.runtime.remedies import Remedy

BuildErrorHint = Callable[[BaseException], str]


@dataclass(frozen=True)
class NoRuntime:
    remedy: Remedy
    hint: BuildErrorHint

    def blocker(self, build_error: BaseException | None) -> Blocker:
        if build_error is None:
            headline = "There is no runtime. Nothing can run until one is built."
            detail = "No runtime is built, and no build error was recorded. The remedy says how to build one."
        else:
            headline = f"The runtime is not built: {type(build_error).__name__}: {_message(build_error)}"
            detail = self.hint(build_error)
        return Blocker(
            id="runtime_not_built",
            kind=BlockerKind.RUNTIME_NOT_BUILT,
            severity=BlockerSeverity.ERROR,
            since=None,
            headline=headline,
            detail=detail,
            remedies=(self.remedy,),
        )

    def build_error(self, build_error: BaseException | None) -> dict[str, str] | None:
        if build_error is None:
            return None
        return {
            "type": type(build_error).__name__,
            "message": _message(build_error),
            "hint": self.hint(build_error),
        }


def _message(error: BaseException) -> str:
    return str(error) or repr(error)

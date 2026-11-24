"""The `OperationError` exception family.

- All Operations raise the same `OperationError`, whose `ErrorCode` fixes the
  code and the HTTP status; `mount_api` sends it as the error envelope.
- Service injection is compositional: services arrive via `__init__`, and
  `run(req)` takes only the Request.
"""

from dataclasses import dataclass

from pydantic import JsonValue
from typing_extensions import Self

from orca.operations.error_codes import ErrorCode, WireErrorCode
from orca.state.confirm_refusals import ConfirmRefusedError, NothingToConfirmError, WorkLostError


def message_of(exc: Exception) -> str:
    """The text an operator should read off a caught exception.

    `str()` on a KeyError is a repr, so a message raised as
    `KeyError("no thread 't1'")` reaches the operator wrapped in another
    layer of quotes. Everything else stringifies as written.
    """
    if isinstance(exc, KeyError) and exc.args and isinstance(exc.args[0], str):
        return exc.args[0]
    return str(exc)


@dataclass
class OperationError(Exception):
    """The single typed exception every Operation raises.

    `code` is the discriminator and fixes the HTTP status. `message` is the
    human-readable reason. `extras` carries structured fields callers
    or surface envelopes may need (e.g. `{"execution_id": "..."}`).

    Not frozen because Python's exception machinery writes to
    `__traceback__` and `__cause__` on raise; a frozen dataclass refuses
    those writes and breaks `raise ... from ...`.
    """

    code: WireErrorCode
    message: str
    extras: dict[str, JsonValue] | None = None

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def surface_code(self) -> str:
        """The code every surface sends for this error."""
        return self.code

    @property
    def http_status(self) -> int:
        return self.code.http_status

    @classmethod
    def of(cls, code: WireErrorCode, message: str, **extras: JsonValue) -> Self:
        return cls(code, message, extras or None)

    @classmethod
    def validation_error(cls, message: str, **extras: JsonValue) -> Self:
        """A refused request body or value."""
        return cls(ErrorCode.VALIDATION_ERROR, message, extras or None)

    @classmethod
    def runtime_not_ready(cls, message: str) -> Self:
        """No runtime is loaded. Every surface sends this one code for it."""
        return cls(ErrorCode.RUNTIME_NOT_READY, message)


_CONFIRM_REFUSALS: dict[type[ConfirmRefusedError], ErrorCode] = {
    NothingToConfirmError: ErrorCode.NOTHING_TO_CONFIRM,
    WorkLostError: ErrorCode.WORK_LOST,
}


def confirm_refused(exc: ConfirmRefusedError, **subject: str) -> OperationError:
    """A refused confirm, under the code that says why."""
    extras: dict[str, JsonValue] = dict(subject)
    if exc.settle_with is not None:
        extras["settle_with"] = exc.settle_with
    code = next(
        (_CONFIRM_REFUSALS[cls] for cls in type(exc).__mro__ if cls in _CONFIRM_REFUSALS), ErrorCode.VALIDATION_ERROR,
    )
    return OperationError.of(code, str(exc), **extras)

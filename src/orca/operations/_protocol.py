"""The `Operation` Protocol + `OperationError` exception family.

- One Protocol covers reads and writes.
- Plain classes implement the Protocol structurally.
- All Operations raise the same `OperationError` typed by an
  `OperationErrorCode` discriminator; binders reshape per surface envelope.
- Service injection is compositional: services arrive via `__init__`, and
  `run(req)` takes only the Request.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Generic, Protocol, TypeVar, runtime_checkable

from pydantic import JsonValue
from typing_extensions import Self

Req = TypeVar("Req", contravariant=True)
Resp = TypeVar("Resp", covariant=True)


class OperationErrorCode(str, Enum):
    """Stable, surface-agnostic error codes.

    Binders translate these to per-surface envelope codes. New codes
    land here; per-surface envelopes pick them up via mapping tables.
    """

    NOT_FOUND = "not_found"
    INVALID_INPUT = "invalid_input"
    CONFLICT = "conflict"
    SERVICE_UNAVAILABLE = "service_unavailable"
    UNAUTHORIZED = "unauthorized"
    INTERNAL_ERROR = "internal_error"


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

    `code` is the surface-agnostic discriminator. `message` is the
    human-readable reason. `extras` carries structured fields callers
    or surface envelopes may need (e.g. `{"execution_id": "..."}`).

    `wire_code` and `status_code` are optional overrides Operations use
    when a runtime exception class has a stable typed-envelope shape
    that maps to a specific wire code + HTTP status the generic
    OperationErrorCode does not capture (e.g. pre-submit errors:
    `LIVE_SUBMISSION_WITH_SIM_OVERRIDES_UNACKNOWLEDGED` -> 422,
    `START_LOCATION_OCCUPIED` -> 409, `SPAWN_INCOMPATIBLE` -> 409).
    Binders honor both fields: `wire_code` replaces `code.value` on the
    envelope, `status_code` replaces the binder's default status
    mapping.

    Not frozen because Python's exception machinery writes to
    `__traceback__` and `__cause__` on raise; a frozen dataclass refuses
    those writes and breaks `raise ... from ...`.
    """

    code: OperationErrorCode
    message: str
    extras: dict[str, JsonValue] | None = None
    wire_code: str | None = None
    status_code: int | None = None

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    @classmethod
    def not_found(cls, message: str, **extras: JsonValue) -> Self:
        return cls(OperationErrorCode.NOT_FOUND, message, extras or None)

    @classmethod
    def invalid_input(cls, message: str, **extras: JsonValue) -> Self:
        return cls(OperationErrorCode.INVALID_INPUT, message, extras or None)

    @classmethod
    def conflict(cls, message: str, **extras: JsonValue) -> Self:
        return cls(OperationErrorCode.CONFLICT, message, extras or None)

    @classmethod
    def service_unavailable(cls, message: str, **extras: JsonValue) -> Self:
        return cls(OperationErrorCode.SERVICE_UNAVAILABLE, message, extras or None)

    @classmethod
    def unauthorized(cls, message: str, **extras: JsonValue) -> Self:
        return cls(OperationErrorCode.UNAUTHORIZED, message, extras or None)

    @classmethod
    def typed(
        cls,
        code: OperationErrorCode,
        message: str,
        wire_code: str,
        status_code: int,
        **extras: JsonValue,
    ) -> Self:
        """Factory for typed-envelope errors with specific wire code + status.

        Use when an Operation catches a runtime exception class with a
        stable typed-envelope shape (pre-submit errors, run-mode errors). The Operation chooses the surface-agnostic
        ``code`` (NOT_FOUND / CONFLICT / INVALID_INPUT / ...) so non-
        envelope-aware callers still get a reasonable category; the
        ``wire_code`` and ``status_code`` carry the specific contract for
        envelope-aware callers.
        """
        return cls(
            code=code,
            message=message,
            extras=extras or None,
            wire_code=wire_code,
            status_code=status_code,
        )


@runtime_checkable
class Operation(Protocol, Generic[Req, Resp]):
    """Structural contract every Operation satisfies.

    Plain classes implement this via duck typing: implement
    `async def run(self, req: Req) -> Resp`. Binders accept anything
    matching this shape.

    Uses `TypeVar` + `Generic` (not PEP 695 `class Op[Req, Resp]:`) so
    the module imports cleanly on Python 3.10 -- a hosted deployment's runtime is
    3.10 while orca-core is 3.12, and this module is shared.

    Concrete Operations conventionally declare `Request` and `Response`
    class attributes (pointing at their Pydantic models) as inline
    documentation, but the Protocol does NOT pin those fields -- the
    binders take `request_model=` / `response_model=` explicitly, and there
    is no implicit registration. Earlier drafts declared
    `Request: ClassVar[type]` here; review item L1 surfaced that those
    fields were decorative. No binder read them, and the weak `type`
    annotation gave the false impression of static enforcement. Removed.
    """

    async def run(self, req: Req, /) -> Resp: ...

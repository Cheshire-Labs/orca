"""Which transporter, if any, is reaching into a device in this task."""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

ARM_REACH_IN_COMMANDS = frozenset({"pick_at_coords", "place_at_coords"})

_reaching_in: ContextVar[str | None] = ContextVar("reaching_in", default=None)


@contextmanager
def reaching_in(mover_name: str) -> Iterator[None]:
    token = _reaching_in.set(mover_name)
    try:
        yield
    finally:
        _reaching_in.reset(token)


def mover_reaching_in() -> str | None:
    """The transporter reaching into a device in this task, or None."""
    return _reaching_in.get()

"""The one rule for an author's event name: ASCII letters, digits, `_` and `-`, at least one of them.

A dot would split the bus's `CUSTOM.<name>.EMITTED` grammar, and slashes, spaces and the like break paths,
URLs and filters downstream, so they are refused where the name is written rather than mangled later.
"""

import re

_VALID = re.compile(r"[A-Za-z0-9_-]+")


class InvalidEventNameError(ValueError):
    """An event name holds a character outside letters, digits, `_` and `-`, or is empty."""


def checked_event_name(name: str) -> str:
    """Return ``name`` unchanged, or raise InvalidEventNameError saying what the rule is."""
    if _VALID.fullmatch(name) is None:
        raise InvalidEventNameError(
            f"event name {name!r} is not allowed: use only letters, digits, '_' and '-', "
            f"with at least one character (for example 'qc_done' rather than 'qc.done')"
        )
    return name


def checked_handler_event_name(name: str) -> str:
    """An event a ``wf.on`` handler listens for, such as ``ACTION.COMPLETED`` or ``CUSTOM.qc_done.EMITTED``.

    Dots separate its parts, and no part is empty or holds a space. Engine parts may hold other characters
    (a slot key holds ``:``); the name in ``CUSTOM.<name>.EMITTED`` follows the event-name rule.
    """
    parts = name.split(".")
    if any(not part or any(c.isspace() for c in part) for part in parts):
        raise InvalidEventNameError(
            f"handler event {name!r} is not allowed: its parts are separated by '.', and no part is empty "
            f"or holds a space (for example 'ACTION.COMPLETED' or 'CUSTOM.qc_done.EMITTED')"
        )
    if parts[0] == "CUSTOM":
        if len(parts) != 3 or parts[2] != "EMITTED":
            raise InvalidEventNameError(
                f"handler event {name!r} is not allowed: a custom event is 'CUSTOM.<name>.EMITTED', "
                f"and <name> holds no '.'"
            )
        checked_event_name(parts[1])
    return name

"""The lifecycle phase of a SystemRuntime, on its own so the wire can read it.

Lives apart from `system_runtime.py` because that module is the engine, and a
caller that only reports the state would pay for all of it.
"""

from enum import Enum


class RuntimeState(str, Enum):
    """Lifecycle phase of a `SystemRuntime`.

    Inheriting from ``str`` (with explicit string values that match each
    member's ``.name``) makes the JSON wire shape intrinsic: Pydantic v2's
    default ``model_dump(mode="json")`` emits ``.value`` (now identical to
    ``.name``) and the daemon schema's coercer accepts the string directly
    via the standard string-to-enum-value lookup. Matches the pattern used
    by every other status enum in ``orca/workflow_models/status_enums.py``.
    """
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"

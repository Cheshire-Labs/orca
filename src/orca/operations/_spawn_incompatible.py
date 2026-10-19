"""The typed refusal for labware of the wrong template at an awaited slot.

Kept out of ``_protocol`` because it imports the runtime interface, and the CLI
imports ``_protocol`` without the engine.
"""

from orca.operations._protocol import OperationError, OperationErrorCode
from orca.runtime.runtime_interface import SpawnIncompatibleError


def spawn_incompatible(exc: SpawnIncompatibleError) -> OperationError:
    """Labware of the wrong template at a slot a thread depends on: 409."""
    return OperationError.typed(
        OperationErrorCode.CONFLICT,
        str(exc),
        wire_code="spawn_incompatible",
        status_code=409,
        location=exc.location,
        expected_template=exc.expected_template,
        actual_template=exc.actual_template,
    )

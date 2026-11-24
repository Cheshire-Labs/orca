"""The typed refusal for labware of the wrong template at an awaited slot.

Kept out of ``_protocol`` because it imports the runtime interface, and the CLI
imports ``_protocol`` without the engine.
"""

from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError
from orca.workflow_models.errors import SpawnIncompatibleError


def spawn_incompatible(exc: SpawnIncompatibleError) -> OperationError:
    """Labware of the wrong template at a slot a thread depends on: 409."""
    return OperationError.of(
        ErrorCode.SPAWN_INCOMPATIBLE,
        str(exc),
        location=exc.location,
        expected_template=exc.expected_template,
        actual_template=exc.actual_template,
    )

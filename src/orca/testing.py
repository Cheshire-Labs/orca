"""Testing utilities for @orca.method functions.

Provides mock_context() for unit testing code methods without
building a full System or SystemRuntime.
"""

import asyncio
import uuid

from orca.resource_models.labware import LabwareInstance
from orca.variables.variable_store import IVariableResolver, NullVariableResolver
from orca.workflow_models.device_handle import ActionRequest
from orca.workflow_models.method_context import MethodContext


def mock_context(
    labware: dict[str, LabwareInstance] | None = None,
    variable_store: IVariableResolver | None = None,
    execution_id: str | None = None,
) -> MethodContext:
    """Create a MethodContext for unit testing @orca.method functions.

    Device calls (ctx.device("name").method()) will put ActionRequests
    on the internal queue but nothing will process them. For tests that
    need device interaction, use the full integration test pattern with
    SystemRuntime instead.
    """
    queue: asyncio.Queue[ActionRequest | None] = asyncio.Queue()
    return MethodContext(
        action_queue=queue,
        assigned_labware=labware or {},
        variable_store=variable_store or NullVariableResolver(),
        execution_id=execution_id if execution_id is not None else f"mock-{uuid.uuid4()}",
    )

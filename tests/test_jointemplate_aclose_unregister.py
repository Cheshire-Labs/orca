"""``JoinTemplate.schedule`` unregisters its contributor on ``aclose()``.

The body wraps ``yield method_to_join`` in ``try / finally`` so that a
``GeneratorExit`` raised by the consumer calling ``aclose()`` -- a
mid-flight abort, a fast-shutdown sweep, etc -- still routes through
the finally and removes the thread from the shared method's
contributor set. Without that, an aborted contributor would leak as a
phantom contributor and stall the owner's completion check.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from orca.workflow_models.method_template import JoinTemplate


pytestmark = pytest.mark.asyncio


async def test_aclose_removes_contributor_via_finally() -> None:
    template = JoinTemplate()

    method_to_join = MagicMock()
    method_to_join.name = "shared_method"
    method_to_join.shared_coord = MagicMock()

    slot = MagicMock()
    slot.await_next_method = AsyncMock(return_value=method_to_join)

    ctx = MagicMock()
    ctx.release_holdover = MagicMock()
    ctx.my_slot = MagicMock(return_value=slot)
    ctx.stop_event = MagicMock()
    ctx.shared_executing_method = None
    ctx.bind_method = MagicMock()
    ctx.thread_id = "thread-aborted"

    registry = MagicMock()
    gen = template.schedule(ctx, registry)

    yielded = await gen.__anext__()
    assert yielded is method_to_join
    method_to_join.shared_coord.add_contributor.assert_called_once_with("thread-aborted")
    method_to_join.shared_coord.remove_contributor.assert_not_called()

    await gen.aclose()

    method_to_join.shared_coord.remove_contributor.assert_called_once_with("thread-aborted")

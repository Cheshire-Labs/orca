"""Focused unit tests for branches inside ``IMethodTemplate.schedule``
bodies that integration coverage exercises only incidentally.

Three branches that historically harbor regressions:

- ``ParkTemplate.schedule()``: the "slot already has queued work" path
  that short-circuits and skips the physical park move.
- ``JoinTemplate.schedule()``: the slot-closed-and-drained path that
  ``await_next_method`` reports by returning ``None``.
- ``BranchStepTemplate.schedule()``: the else-fallback and the
  missing-branch ``ValueError``.
"""
import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from orca.events.event_channel import EventChannelRegistry
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.event_step import BranchStepTemplate
from orca.workflow_models.labware_threads.i_thread_context import IThreadContext
from orca.workflow_models.method import ExecutingMethod
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import JoinTemplate, MethodTemplate
from orca.workflow_models.park_template import ParkTemplate


pytestmark = pytest.mark.asyncio


async def test_park_skips_physical_move_when_slot_queue_has_work() -> None:
    """ParkTemplate.schedule must short-circuit before
    ``mark_my_labware_parked`` / ``location`` / ``fire_and_execute_move_to``
    when the slot already has queued work."""

    template = ParkTemplate("park_slot")

    slot = MagicMock()
    slot.queue_empty = MagicMock(return_value=False)

    ctx = MagicMock()
    ctx.release_holdover = MagicMock()
    ctx.my_slot = MagicMock(return_value=slot)
    ctx.mark_my_labware_parked = MagicMock()
    ctx.location = MagicMock()
    ctx.fire_and_execute_move_to = AsyncMock()

    registry = MagicMock()
    yielded = [em async for em in template.schedule(ctx, registry)]

    assert yielded == []
    ctx.release_holdover.assert_called_once()
    ctx.my_slot.assert_called_once()
    slot.queue_empty.assert_called_once()
    ctx.mark_my_labware_parked.assert_not_called()
    ctx.location.assert_not_called()
    ctx.fire_and_execute_move_to.assert_not_called()


async def test_join_returns_when_slot_drained_and_closed() -> None:
    """JoinTemplate.schedule must return without yielding when the
    thread's slot reports no more work (await_next_method returns None)."""

    template = JoinTemplate()

    slot = MagicMock()
    slot.await_next_method = AsyncMock(return_value=None)

    ctx = MagicMock()
    ctx.release_holdover = MagicMock()
    ctx.my_slot = MagicMock(return_value=slot)
    ctx.stop_event = asyncio.Event()
    ctx.shared_executing_method = MagicMock()
    ctx.bind_method = MagicMock()
    ctx.thread_id = "thread-1"

    registry = MagicMock()
    yielded = [em async for em in template.schedule(ctx, registry)]

    assert yielded == []
    slot.await_next_method.assert_awaited_once_with(ctx.stop_event)
    ctx.bind_method.assert_not_called()


async def test_branch_step_takes_else_branch_on_unmatched_value() -> None:
    """BranchStepTemplate.schedule must route to the ``else`` branch
    when the published value matches no explicit branch key."""

    else_method = MethodTemplate(
        name="else_method", func=_dummy_method_func,
    )
    template = BranchStepTemplate(
        event_name="route",
        branches={"a": [], "else": [else_method]},
    )

    sentinel_em = MagicMock()
    captured: dict[str, object] = {}

    async def _fake_schedule(
        ctx: IThreadContext, registry: EventChannelRegistry
    ) -> AsyncIterator[ExecutingMethod]:
        captured["ctx"] = ctx
        captured["registry"] = registry
        yield sentinel_em

    else_method.schedule = _fake_schedule

    channel = MagicMock()
    channel.wait = AsyncMock(return_value=(1, "no_match", {}))
    registry = MagicMock()
    registry.get_or_create = MagicMock(return_value=channel)

    ctx = MagicMock()
    yielded = [em async for em in template.schedule(ctx, registry)]

    assert yielded == [sentinel_em]
    assert captured["ctx"] is ctx
    assert captured["registry"] is registry


async def test_branch_step_raises_when_no_match_and_no_else() -> None:
    """BranchStepTemplate.schedule must raise ValueError when the
    published value matches neither an explicit branch nor ``else``."""

    template = BranchStepTemplate(
        event_name="route",
        branches={"a": []},
    )

    channel = MagicMock()
    channel.wait = AsyncMock(return_value=(1, "no_match", {}))
    registry = MagicMock()
    registry.get_or_create = MagicMock(return_value=channel)

    ctx = MagicMock()
    with pytest.raises(ValueError, match="no matching branch and no 'else' fallback"):
        async for _ in template.schedule(ctx, registry):
            pass


async def _dummy_method_func(
    ctx: MethodContext,
) -> AsyncGenerator[ActionTemplate, None]:
    del ctx
    if False:
        yield  # pragma: no cover

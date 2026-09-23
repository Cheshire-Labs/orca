"""Event coordination step templates for thread-level control flow.

WaitStep: thread blocks until a named event is published (latch semantics).
BranchStep: thread waits for event, then selects a method branch by value.

These are NOT actions or methods. They have no device, location, or reservation.
They live at the thread level as coordination primitives whose ``schedule()``
materializes one or more ``ExecutingMethod`` instances for the thread loop.
"""

from collections.abc import AsyncIterator

from orca.events.event_channel import EventChannelRegistry
from orca.workflow_models.labware_threads.i_thread_context import IThreadContext
from orca.workflow_models.method import ExecutingMethod, MethodInstance
from orca.workflow_models.method_template import IMethodTemplate, MethodTemplate


class WaitStepTemplate(IMethodTemplate):
    """Thread waits for a named event before continuing to next method.

    Uses latch semantics: sees the latest publish regardless of timing.
    """

    def __init__(self, event_name: str, timeout: float | None = None) -> None:
        self._event_name = event_name
        self._timeout = timeout

    @property
    def name(self) -> str:
        return f"on:{self._event_name}"

    @property
    def event_name(self) -> str:
        return self._event_name

    @property
    def timeout(self) -> float | None:
        return self._timeout

    async def schedule(
        self,
        ctx: IThreadContext,
        registry: EventChannelRegistry,
    ) -> AsyncIterator[ExecutingMethod]:
        wait_method = MethodInstance(self.name)
        ctx.bind_method(wait_method)
        ctx.add_method(wait_method)
        em = ctx.create_executing_method(wait_method)
        em.set_wait_step(self.event_name, self.timeout)
        yield em


class BranchStepTemplate(IMethodTemplate):
    """Thread waits for event, then selects method branch by value.

    Use ``"else"`` key as fallback for unmatched values.
    """

    def __init__(
        self,
        event_name: str,
        branches: dict[str, list[IMethodTemplate]],
        timeout: float | None = None,
    ) -> None:
        self._event_name = event_name
        self._branches = branches
        self._timeout = timeout

    @property
    def name(self) -> str:
        return f"branch:{self._event_name}"

    @property
    def event_name(self) -> str:
        return self._event_name

    @property
    def branches(self) -> dict[str, list[IMethodTemplate]]:
        return self._branches

    @property
    def timeout(self) -> float | None:
        return self._timeout

    async def schedule(
        self,
        ctx: IThreadContext,
        registry: EventChannelRegistry,
    ) -> AsyncIterator[ExecutingMethod]:
        channel = registry.get_or_create(self.event_name)
        _, value, _ = await channel.wait(seen_counter=0, timeout=self.timeout)
        str_value = str(value)
        if str_value in self._branches:
            branch_methods = self._branches[str_value]
        elif "else" in self._branches:
            branch_methods = self._branches["else"]
        else:
            raise ValueError(
                f"Branch event '{self.event_name}' received '{value}' "
                f"with no matching branch and no 'else' fallback."
            )
        for bt in branch_methods:
            assert isinstance(bt, MethodTemplate), (
                f"Branch template must be MethodTemplate, got {type(bt)}"
            )
            async for em in bt.schedule(ctx, registry):
                yield em

"""A thread made by the test builder is a working thread: it runs a method and completes."""

from collections.abc import AsyncGenerator

from orca.events.event_bus import EventBus
from orca.events.event_channel import EventChannelRegistry
from orca.events.execution_context import ExecutionContext
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.labware_threads.executing_labware_thread import ExecutingLabwareThread
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate, MethodTemplate
from orca.workflow_models.status_enums import LabwareThreadStatus
from orca.workflow_models.thread_context import ThreadContext
from tests.thread_builder import a_labware_thread_instance, build_executing_thread


async def _no_actions(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
    return
    yield


async def _a_thread_that_runs_one_empty_method(
    *, event_bus: EventBus | None = None,
) -> ExecutingLabwareThread:
    pad = Location("pad", resource=PlatePad("pad"))
    system_map = SystemMap(ResourceRegistry())
    await system_map.add_location(pad)
    instance = a_labware_thread_instance(start_location=pad)
    method = MethodTemplate("rinse", func=_no_actions)

    async def run_the_method(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield method

    instance.set_yield_func(run_the_method)
    thread = build_executing_thread(thread=instance, system_map=system_map, event_bus=event_bus)
    thread.publish_initial_status()
    thread.set_event_channel_registry(EventChannelRegistry())
    return thread


async def test_a_built_thread_starts_created_and_completes_after_running_its_method() -> None:
    thread = await _a_thread_that_runs_one_empty_method()
    assert thread.status is LabwareThreadStatus.CREATED

    await thread.start()

    assert thread.status is LabwareThreadStatus.COMPLETED
    assert thread.has_completed()
    assert [method.name for method in thread.completed_methods] == ["rinse"]


async def test_a_built_thread_publishes_its_status_on_the_event_bus_it_was_given() -> None:
    bus = EventBus()
    thread = await _a_thread_that_runs_one_empty_method(event_bus=bus)
    seen: list[str] = []

    def record(event: str, context: ExecutionContext) -> None:
        seen.append(event)

    bus.subscribe(f"THREAD.{thread.id}.COMPLETED", record)

    await thread.start()

    assert seen == [f"THREAD.{thread.id}.COMPLETED"]

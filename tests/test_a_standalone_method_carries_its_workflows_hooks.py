"""A method run on its own fires its workflow's `wf.on` handlers: all by default, none for `[]`, or only the events named."""
import pytest

from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.errors import EventHooksNotInTheWorkflowError
from tests.standalone_method_hooks_world import (
    ACTION_COMPLETED,
    METHOD,
    READ_DONE,
    Heard,
    a_built_reading_workflow,
    a_started_runtime,
)


async def _run_the_method(event_hooks: list[str] | None) -> Heard:
    heard = Heard()
    build = await a_built_reading_workflow(heard)
    runtime = await a_started_runtime(build.system, build)
    try:
        record = await runtime.submit_method(**METHOD, mode=WorkflowRunMode.SIM, event_hooks=event_hooks)
        ended = await runtime.wait(record.id)
        assert ended.status.value == "completed", ended
        return heard
    finally:
        await runtime.shutdown(confirm=True)


async def test_with_no_choice_every_handler_of_the_workflow_fires() -> None:
    heard = await _run_the_method(event_hooks=None)
    assert heard.read_done == [READ_DONE]
    assert heard.action_completed


async def test_an_empty_list_fires_no_handler() -> None:
    heard = await _run_the_method(event_hooks=[])
    assert heard == Heard()


async def test_naming_events_fires_only_their_handlers() -> None:
    heard = await _run_the_method(event_hooks=[READ_DONE])
    assert heard.read_done == [READ_DONE]
    assert heard.action_completed == []


async def test_naming_an_event_the_workflow_has_no_handler_for_is_refused_with_the_events_it_has() -> None:
    heard = Heard()
    build = await a_built_reading_workflow(heard)
    runtime = await a_started_runtime(build.system, build)
    try:
        with pytest.raises(EventHooksNotInTheWorkflowError) as refused:
            await runtime.submit_method(**METHOD, mode=WorkflowRunMode.SIM, event_hooks=["CUSTOM.read_dne.EMITTED"])
        assert READ_DONE in str(refused.value) and ACTION_COMPLETED in str(refused.value)
    finally:
        await runtime.shutdown(confirm=True)


async def test_a_run_with_no_handlers_hears_nothing_after_a_run_that_had_them() -> None:
    heard = Heard()
    build = await a_built_reading_workflow(heard)
    runtime = await a_started_runtime(build.system, build)
    try:
        first = await runtime.submit_method(**METHOD, mode=WorkflowRunMode.SIM, event_hooks=None)
        await runtime.wait(first.id)
        assert heard.read_done == [READ_DONE]

        second = await runtime.submit_method(**METHOD, mode=WorkflowRunMode.SIM, event_hooks=[])
        ended = await runtime.wait(second.id)
        assert ended.status.value == "completed", ended
        assert heard.read_done == [READ_DONE]
    finally:
        await runtime.shutdown(confirm=True)


async def test_each_run_fires_its_handlers_once() -> None:
    heard = Heard()
    build = await a_built_reading_workflow(heard)
    runtime = await a_started_runtime(build.system, build)
    try:
        for _ in range(2):
            record = await runtime.submit_method(**METHOD, mode=WorkflowRunMode.SIM, event_hooks=[READ_DONE])
            await runtime.wait(record.id)
        assert heard.read_done == [READ_DONE, READ_DONE]
    finally:
        await runtime.shutdown(confirm=True)

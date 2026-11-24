"""A member names one thread. When one thread's function name is another thread's labware name, the labware name wins."""

from collections.abc import AsyncGenerator

from orca.workflow_models.labware_group import LabwareGroup, LabwareGroupMember
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.thread_template import ThreadTemplate, thread_named, threads_a_group_names
from tests.test_helpers import create_test_plate_template


async def x(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
    return
    yield


async def b_journey(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
    return
    yield


def _a_thread_whose_function_name_is_the_other_threads_labware_name() -> tuple[ThreadTemplate, ThreadTemplate]:
    a = ThreadTemplate(labware_template=create_test_plate_template("a"), start="pad1", end="pad1", func=x)
    b = ThreadTemplate(labware_template=create_test_plate_template("x"), start="pad2", end="pad2", func=b_journey)
    return a, b


def test_a_name_that_is_one_threads_labware_name_and_another_threads_function_name_picks_the_labware_name() -> None:
    a, b = _a_thread_whose_function_name_is_the_other_threads_labware_name()

    assert thread_named([a, b], "x") is b
    assert thread_named([b, a], "x") is b
    assert thread_named([a, b], "a") is a
    assert thread_named([a, b], "b_journey") is b
    assert thread_named([a, b], "nothing") is None


def test_a_group_naming_the_colliding_name_names_only_the_thread_with_that_labware_name() -> None:
    a, b = _a_thread_whose_function_name_is_the_other_threads_labware_name()
    group = LabwareGroup(id="g1", members=(LabwareGroupMember("x"),))

    assert threads_a_group_names(group, [a, b]) == {"x": "x"}


def test_a_group_naming_a_thread_by_both_names_keeps_the_labware_name() -> None:
    a, _ = _a_thread_whose_function_name_is_the_other_threads_labware_name()
    for members in (("a", "x"), ("x", "a")):
        group = LabwareGroup(id="g1", members=tuple(LabwareGroupMember(m) for m in members))

        assert threads_a_group_names(group, [a]) == {"a": "a"}

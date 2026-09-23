"""Tests for Topic 1: Spawn-Join Refactor.

JoinTemplate (orca.join()) replaces SharedMethodTemplate for declaring
that a thread joins a parent's shared method at spawn time.
"""

import asyncio
import inspect

import pytest
from unittest.mock import MagicMock

from orca.workflow_models.method_template import (
    IMethodTemplate,
    JoinTemplate,
    MethodTemplate,
)
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method import ExecutingMethod
from orca.workflow_models.method_context import MethodContext

from collections.abc import AsyncGenerator


async def _dummy_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
    return
    yield


class TestJoinTemplateProperties:
    """JoinTemplate is a pure descriptor with no mutable state."""

    def test_no_args_defaults(self) -> None:
        jt = JoinTemplate()
        assert jt.method is None
        assert jt.allows is None
        assert jt.name == "join:auto"

    def test_with_method_reference(self) -> None:
        m = MethodTemplate("sample_to_bead_plate", func=_dummy_method)
        jt = JoinTemplate(method=m)
        assert jt.method is m
        assert jt.name == "join:sample_to_bead_plate"

    def test_with_allows_list(self) -> None:
        m1 = MethodTemplate("add_elution_buffer_b", func=_dummy_method)
        m2 = MethodTemplate("add_buffer_d", func=_dummy_method)
        m3 = MethodTemplate("combine_plates", func=_dummy_method)
        jt = JoinTemplate(allows=[m1, m2, m3])
        assert jt.allows is not None
        assert len(jt.allows) == 3

    def test_fulfills_imethod_template_contract(self) -> None:
        """JoinTemplate must actually satisfy the IMethodTemplate surface.

        isinstance alone is a tautology (JoinTemplate declares the base);
        this pins that the abstract members resolve to real implementations
        the thread loop can use: a stable ``name`` and an async-gen
        ``schedule``.
        """
        jt = JoinTemplate()
        assert isinstance(jt.name, str) and jt.name
        assert inspect.isasyncgenfunction(JoinTemplate.schedule)

    def test_validate_spawn_context_passes_for_allowed_method(self) -> None:
        m1 = MethodTemplate("shake_it", func=_dummy_method)
        m2 = MethodTemplate("mix_it", func=_dummy_method)
        jt = JoinTemplate(allows=[m1, m2])
        jt.validate_spawn_context("shake_it")  # should not raise

    def test_validate_spawn_context_rejects_disallowed_method(self) -> None:
        m1 = MethodTemplate("shake_it", func=_dummy_method)
        jt = JoinTemplate(allows=[m1])
        with pytest.raises(ValueError, match="only allows"):
            jt.validate_spawn_context("unknown_method")

    def test_validate_spawn_context_none_allows_always_passes(self) -> None:
        jt = JoinTemplate()
        jt.validate_spawn_context("anything")  # should not raise

    def test_validate_spawn_context_explicit_method_rejects_mismatch(self) -> None:
        m = MethodTemplate("expected_method", func=_dummy_method)
        jt = JoinTemplate(method=m)
        with pytest.raises(ValueError, match="expected: 'expected_method'"):
            jt.validate_spawn_context("wrong_method")

    def test_validate_spawn_context_explicit_method_accepts_match(self) -> None:
        m = MethodTemplate("expected_method", func=_dummy_method)
        jt = JoinTemplate(method=m)
        jt.validate_spawn_context("expected_method")  # should not raise


class TestJoinTemplateResolvesToSharedMethod:
    """When a thread is spawned with a shared_method, the instance stores that reference."""

    @pytest.mark.asyncio
    async def test_shared_method_stored_on_instance(self) -> None:
        from orca.resource_models.labware import PlateTemplate
        from orca.resource_models.location import Location
        from orca.state.ops_history import OpsHistory
        from orca.resource_models.plate_pad import PlatePad
        from orca.workflow_models.thread_template import ThreadTemplate
        from orca.workflow_models.thread_context import ThreadContext
        from orca.workflow_models.workflows.workflow_factories import (
            MethodFactory,
            ThreadFactory,
        )

        mock_method = MagicMock(spec=ExecutingMethod)
        mock_method.name = "shared_shake"
        mock_method.id = "test-id"
        mock_method.actions = []

        from orca.runtime.sim_labware import SimPlateTemplate
        plate = SimPlateTemplate("test_plate")
        loc = Location("loc", PlatePad("pad"))
        jt = JoinTemplate()

        async def _gen(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            yield jt

        tt = ThreadTemplate(plate, loc, loc, func=_gen)
        factory = ThreadFactory(MethodFactory(), OpsHistory())
        from orca.runtime.run_modes import WorkflowRunMode
        instance = await factory.create_instance(
            tt, run_mode=WorkflowRunMode.PURE_SIM, shared_method=mock_method,
        )

        assert instance.shared_executing_method is mock_method


class TestThreadFactoryWithJoinTemplate:
    """ThreadFactory.create_instance() wires shared_method for JoinTemplate threads."""

    @pytest.mark.asyncio
    async def test_create_instance_with_shared_method(self) -> None:
        from collections.abc import AsyncGenerator
        from orca.resource_models.labware import PlateTemplate
        from orca.resource_models.location import Location
        from orca.state.ops_history import OpsHistory
        from orca.resource_models.plate_pad import PlatePad
        from orca.workflow_models.thread_template import ThreadTemplate
        from orca.workflow_models.thread_context import ThreadContext
        from orca.workflow_models.workflows.workflow_factories import (
            MethodFactory,
            ThreadFactory,
        )

        from orca.runtime.sim_labware import SimPlateTemplate
        plate = SimPlateTemplate("test_plate")
        loc_start = Location("start", PlatePad("start_pad"))
        loc_end = Location("end", PlatePad("end_pad"))

        jt = JoinTemplate()

        async def _gen(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            yield jt

        thread_template = ThreadTemplate(plate, loc_start, loc_end, func=_gen)

        mock_method = MagicMock(spec=ExecutingMethod)
        mock_method.name = "some_method"
        mock_method.id = "test-id"
        mock_method.actions = []
        mock_method.is_code_method = False

        factory = ThreadFactory(MethodFactory(), OpsHistory())
        from orca.runtime.run_modes import WorkflowRunMode
        thread_instance = await factory.create_instance(
            thread_template, run_mode=WorkflowRunMode.PURE_SIM,
            shared_method=mock_method,
        )
        assert thread_instance.shared_executing_method is mock_method
        mock_method.assign_thread.assert_called_once_with(
            plate, thread_instance,
        )


class TestYieldAdapterRejectsOrphanJoin:
    """A thread yielding orca.join() without being spawned with a shared_method raises."""

    @pytest.mark.asyncio
    async def test_join_without_shared_method_has_none(self) -> None:
        from collections.abc import AsyncGenerator
        from orca.resource_models.labware import PlateTemplate
        from orca.resource_models.location import Location
        from orca.state.ops_history import OpsHistory
        from orca.resource_models.plate_pad import PlatePad
        from orca.workflow_models.thread_template import ThreadTemplate
        from orca.workflow_models.thread_context import ThreadContext
        from orca.workflow_models.workflows.workflow_factories import (
            MethodFactory,
            ThreadFactory,
        )

        from orca.runtime.sim_labware import SimPlateTemplate
        plate = SimPlateTemplate("test_plate")
        loc_start = Location("start", PlatePad("start_pad"))
        loc_end = Location("end", PlatePad("end_pad"))

        jt = JoinTemplate()

        async def _gen(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            yield jt

        thread_template = ThreadTemplate(plate, loc_start, loc_end, func=_gen)

        factory = ThreadFactory(MethodFactory(), OpsHistory())
        from orca.runtime.run_modes import WorkflowRunMode
        instance = await factory.create_instance(
            thread_template, run_mode=WorkflowRunMode.PURE_SIM,
        )
        assert instance.shared_executing_method is None

        from orca.workflow_models.labware_threads.i_thread_context import (
            IThreadContext,
        )
        from orca.events.event_channel import EventChannelRegistry

        ctx = MagicMock(spec=IThreadContext)
        ctx.my_slot.return_value = None
        ctx.shared_executing_method = None
        ctx.stop_event = asyncio.Event()

        with pytest.raises(ValueError, match="no method available"):
            async for _ in jt.schedule(ctx, EventChannelRegistry()):
                pass


class TestThreadTemplateNoSetWrappedMethod:
    """ThreadTemplate.set_wrapped_method() is removed."""

    def test_no_set_wrapped_method(self) -> None:
        from orca.workflow_models.thread_template import ThreadTemplate

        assert not hasattr(ThreadTemplate, "set_wrapped_method")

"""Tests for Action template and @orca.action decorator.

Action is a new ActionTemplate subclass for code-first actions.
It stores an async function, device/pool, and labware declarations.
The decorator @orca.action produces an Action template.
"""

import asyncio

import pytest

import orca.orca as orca
from orca.resource_models.labware import AnyLabwareTemplate
from orca.resource_models.resource_pool import ResourcePool
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import Action, ActionTemplate
from orca.workflow_models.status_enums import FailurePolicy
from orca.workflow_models.workflows.workflow_factories import MethodActionFactory
from tests.mock import UniversalMockDevice
from tests.test_helpers import create_test_plate_template


class TestActionTemplate:

    def test_action_template_stores_func_and_pool(self) -> None:
        """Action stores the user function and resource pool."""
        device = UniversalMockDevice("shaker_1")
        pool = ResourcePool("shaker_1", [device])
        plate = create_test_plate_template("plate_96")

        async def my_action(ctx: ActionContext) -> None:
            pass

        action = Action(
            func=my_action,
            resource=pool,
            inputs=[plate],
        )
        assert action.func is my_action
        assert action.resource_pool is pool
        assert action.inputs == [plate]

    def test_action_template_outputs_default_to_inputs(self) -> None:
        """When outputs is not specified, it defaults to same as inputs."""
        device = UniversalMockDevice("shaker_1")
        pool = ResourcePool("shaker_1", [device])
        plate = create_test_plate_template("plate_96")

        async def my_action(ctx: ActionContext) -> None:
            pass

        action = Action(func=my_action, resource=pool, inputs=[plate])
        assert action.outputs == [plate]

    def test_action_template_outputs_empty_list(self) -> None:
        """outputs=[] means labware is consumed (not output)."""
        device = UniversalMockDevice("shaker_1")
        pool = ResourcePool("shaker_1", [device])
        plate = create_test_plate_template("plate_96")

        async def my_action(ctx: ActionContext) -> None:
            pass

        action = Action(func=my_action, resource=pool, inputs=[plate], outputs=[])
        assert action.outputs == []

    async def test_action_creates_unresolved_with_full_templates(self) -> None:
        """MethodActionFactory produces UnresolvedLocationAction with declared templates."""
        device = UniversalMockDevice("shaker_1")
        pool = ResourcePool("shaker_1", [device])
        plate = create_test_plate_template("plate_96")

        async def my_action(ctx: ActionContext) -> None:
            pass

        action = Action(func=my_action, resource=pool, inputs=[plate])
        factory = MethodActionFactory(action)
        unresolved = factory.create_instance()

        assert unresolved.expected_input_templates == [plate]
        assert unresolved.expected_output_templates == [plate]

    async def test_action_with_any_labware_template(self) -> None:
        """Action with AnyLabwareTemplate creates wildcard slots."""
        device = UniversalMockDevice("delidder")
        pool = ResourcePool("delidder", [device])
        any_lw = AnyLabwareTemplate()

        async def delid_action(ctx: ActionContext) -> None:
            pass

        action = Action(func=delid_action, resource=pool, inputs=[any_lw])
        factory = MethodActionFactory(action)
        unresolved = factory.create_instance()

        assert len(unresolved.expected_input_templates) == 1
        assert isinstance(unresolved.expected_input_templates[0], AnyLabwareTemplate)

    def test_action_failure_policy_parameter(self) -> None:
        """Failure policy can be set on Action."""
        device = UniversalMockDevice("shaker_1")
        pool = ResourcePool("shaker_1", [device])
        plate = create_test_plate_template("plate_96")

        async def my_action(ctx: ActionContext) -> None:
            pass

        action = Action(
            func=my_action, resource=pool, inputs=[plate],
            failure_policy=FailurePolicy.ABORT,
        )
        assert action.failure_policy == FailurePolicy.ABORT

class TestOrcaActionDecorator:

    def test_decorator_produces_action_template(self) -> None:
        """@orca.action produces an Action instance."""
        device = UniversalMockDevice("shaker_1")
        pool = ResourcePool("shaker_1", [device])
        plate = create_test_plate_template("plate_96")

        @orca.action(device=pool, inputs=[plate])
        async def shake_it(ctx: ActionContext) -> None:
            pass

        assert isinstance(shake_it, Action)
        assert shake_it.func is not None
        assert shake_it.resource_pool is pool
        assert shake_it.inputs == [plate]

    def test_decorator_with_device_object(self) -> None:
        """@orca.action accepts a Device directly (wraps in ResourcePool)."""
        device = UniversalMockDevice("shaker_1")
        plate = create_test_plate_template("plate_96")

        @orca.action(device=device, inputs=[plate])
        async def shake_it(ctx: ActionContext) -> None:
            pass

        assert isinstance(shake_it, Action)
        assert device in shake_it.resource_pool.resources

    def test_decorator_with_failure_policy(self) -> None:
        """@orca.action passes failure_policy through."""
        device = UniversalMockDevice("shaker_1")
        pool = ResourcePool("shaker_1", [device])
        plate = create_test_plate_template("plate_96")

        @orca.action(device=pool, inputs=[plate], failure_policy=FailurePolicy.ABORT)
        async def shake_it(ctx: ActionContext) -> None:
            pass

        assert shake_it.failure_policy == FailurePolicy.ABORT

    def test_decorator_name_from_function(self) -> None:
        """Action name comes from the decorated function's __name__."""
        device = UniversalMockDevice("shaker_1")
        pool = ResourcePool("shaker_1", [device])
        plate = create_test_plate_template("plate_96")

        @orca.action(device=pool, inputs=[plate])
        async def my_custom_action(ctx: ActionContext) -> None:
            pass

        assert my_custom_action.operation_name == "my_custom_action"

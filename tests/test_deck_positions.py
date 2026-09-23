"""Tests for deck_positions propagation through the action chain."""

import pytest

from orca.resource_models.labware import PlateTemplate
from orca.resource_models.resource_pool import ResourcePool
from orca.workflow_models.action_template import Action
from orca.workflow_models.workflows.workflow_factories import MethodActionFactory
from tests.mock import UniversalMockDevice


def _make_plate(name: str) -> PlateTemplate:
    from orca.runtime.sim_labware import SimPlateTemplate
    return SimPlateTemplate(name)


class TestDeckPositionsOnAction:

    def test_action_stores_deck_positions(self) -> None:
        plate = _make_plate("plate")
        device = UniversalMockDevice("lh")
        deck_pos = {plate: "pos1"}

        async def my_action(ctx: object) -> None:
            pass

        a = Action(
            func=my_action,
            resource=device,
            inputs=[plate],
            deck_positions=deck_pos,
        )
        assert a.deck_positions == deck_pos

    def test_action_deck_positions_defaults_empty(self) -> None:
        plate = _make_plate("plate")
        device = UniversalMockDevice("lh")

        async def my_action(ctx: object) -> None:
            pass

        a = Action(func=my_action, resource=device, inputs=[plate])
        assert a.deck_positions == {}


class TestDeckPositionsPropagation:

    async def test_factory_propagates_deck_positions(self) -> None:
        plate = _make_plate("plate")
        device = UniversalMockDevice("lh")
        deck_pos = {plate: "pos1"}

        async def my_action(ctx: object) -> None:
            pass

        a = Action(
            func=my_action,
            resource=device,
            inputs=[plate],
            deck_positions=deck_pos,
        )
        factory = MethodActionFactory(a)
        unresolved = factory.create_instance()
        assert unresolved.deck_positions == deck_pos

    async def test_factory_propagates_empty_deck_positions(self) -> None:
        plate = _make_plate("plate")
        device = UniversalMockDevice("lh")

        async def my_action(ctx: object) -> None:
            pass

        a = Action(func=my_action, resource=device, inputs=[plate])
        factory = MethodActionFactory(a)
        unresolved = factory.create_instance()
        assert unresolved.deck_positions == {}

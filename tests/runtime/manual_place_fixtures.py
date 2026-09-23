"""Shared build for a workflow whose entry labware is placed by hand.

`start=pad1, end=pad1` with a one-device method, in both spellings of a
hand-placed entry: bare `start=pad_loc` and explicit
`start=(pad_loc, MANUAL_PLACE)`. Several suites need this exact shape, so
it lives here rather than in whichever test file happened to build it first.
"""

from collections.abc import AsyncGenerator
from datetime import datetime, timezone

import orca.orca as orca
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.status_models import ConnectionCard
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, WorkflowTemplate
from orca.spawn import MANUAL_PLACE
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system_interface import ISystem
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.thread_template import StartArg
from tests.mock import TRANSPORTER_MOCK_INTERFACES, UNIVERSAL_MOCK_INTERFACES
from tests.runtime.registries.test_device_registry import FakeConnectionSource
from tests.test_helpers import (
    create_test_device,
    create_test_plate_template,
    create_test_transporter,
    wire_system_map,
)


# -- Fixture helpers --------------------------------------------------------


def _connection_card(
    name: str,
    *,
    interfaces: frozenset[str] = UNIVERSAL_MOCK_INTERFACES,
    advertised_kind: str = "UniversalMockDevice",
) -> ConnectionCard:
    return ConnectionCard(
        name=name,
        client_id=f"client-{name}",
        connection_id=f"conn-{name}",
        last_heartbeat=datetime.now(timezone.utc),
        advertised_kind=advertised_kind,
        advertised_interfaces=interfaces,
    )


def _live_connection_source(
    extra_device_names: tuple[str, ...] = (),
) -> FakeConnectionSource:
    """Connection source reporting shaker1 + robot1 (+ any extras) as
    live-connected so `assert_runnable` lets LIVE submissions through to
    the typed-error path under test."""
    now = datetime.now(timezone.utc)
    cards = [
        _connection_card("shaker1"),
        _connection_card(
            "robot1",
            interfaces=TRANSPORTER_MOCK_INTERFACES,
            advertised_kind="SimTransporterDriver",
        ),
    ]
    for name in extra_device_names:
        cards.append(_connection_card(name))
    return FakeConnectionSource(cards, now=now)


async def _build_manual_place_system(
    workflow_name: str = "wf_manual_place",
    plate_name: str | None = None,
    explicit_manual_place: bool = False,
) -> ISystem:
    """One-device workflow with a `start=pad1, end=pad1` ManualPlace entry thread.

    `plate_name` lets the caller pick a unique labware-template name
    when multiple systems are built in one test (the SDK registry
    rejects duplicates with `KeyError: Labware ... is already defined`).

    `explicit_manual_place` switches the entry between the two spellings of
    the same intent: bare `start=pad_loc` and `start=(pad_loc, MANUAL_PLACE)`.
    """
    if plate_name is None:
        plate_name = f"plate_{workflow_name}"
    plate = create_test_plate_template(plate_name)
    registry = ResourceRegistry()
    shaker = create_test_device("shaker1", sim_override=None)
    registry.add_resource(shaker)
    transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
    registry.add_resource(transporter)
    pool = ResourcePool("shaker1", [shaker])
    registry.add_resource_pool(pool)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": shaker}, pads=["pad1"])

    @orca.action(device=pool, inputs=[plate])
    async def shake_action(ctx: ActionContext) -> None:
        del ctx

    @orca.method
    async def shake_method(
        ctx: MethodContext,
    ) -> AsyncGenerator[ActionTemplate, None]:
        del ctx
        yield shake_action

    pad_loc = system_map.get_location("pad1")
    start_arg: StartArg = (
        (pad_loc, MANUAL_PLACE) if explicit_manual_place else pad_loc
    )

    @orca.thread(labware=plate, start=start_arg, end=pad_loc)
    async def plate_thread(
        ctx: ThreadContext,
    ) -> AsyncGenerator[MethodTemplate, None]:
        del ctx
        yield shake_method

    workflow = WorkflowTemplate(workflow_name)
    workflow.add_thread(plate_thread, is_start=True)

    builder = SdkToSystemBuilder(
        name=f"{workflow_name}_system",
        description="",
        labwares=[plate],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=EventBus(),
    )
    await builder.bind_labwares()
    return builder.get_system()

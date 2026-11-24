"""Submit-time validation: LiquidHandlers must declare a resolvable
``deck_layout`` in every run mode.

A check run runs the same simulator a sim run does, so it needs the same deck:

* Every ``LiquidHandler`` in the topology MUST declare
  a non-None ``deck_layout`` AND that name must resolve to an entry in
  the device's ``deck_layout_store``. Failing either check raises
  ``DeckLayoutRequiredError`` at submit-time, before the execution
  boots.

The validator runs INSIDE the same flow as ``unreachable``: tests
that pass connection cards but omit deck-layout configuration must
still fail.
"""

from orca.config import OrcaConfig
from collections.abc import AsyncGenerator
from datetime import datetime, timezone

import pytest

import orca.orca as orca
from tests.mock import TRANSPORTER_MOCK_INTERFACES
from orca.devices.devices import LiquidHandler
from orca.resource_models.resource_pool import ResourcePool
from tests.no_devices import NullGatewayRegistry
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.errors import DeckLayoutRequiredError
from orca.runtime.status_models import ConnectionCard
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import (
    ActionTemplate,
    MethodTemplate,
    WorkflowTemplate,
)
from orca.system.deck_sites import enumerate_deck_sites
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system_interface import ISystem
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from cheshire_drivers.liquid_handler_models import DeckLayoutConfig, DeckResourceConfig

from tests.runtime.registries.test_device_registry import FakeConnectionSource
from tests.test_helpers import (
    create_test_plate_template,
    create_test_transporter,
    wire_system_map,
)
from tests.setup_package import deck_layout_file, with_deck_layouts
from tests.memory_activity import memory_ops_store, memory_runtime, memory_variable_store
from tests.memory_activity import memory_unsaved_positions
from tests.labware_catalog import bundled_labware_catalog
from tests.memory_activity import memory_labware_service


def _layout(deck_type: str = "STARlet") -> DeckLayoutConfig:
    """Minimal valid layout for store seeding: one carrier so the LH derives
    real deck sites the arm can teach a site-qualified point to."""
    return DeckLayoutConfig(
        deck_type=deck_type,
        resources=[
            DeckResourceConfig(name="carrier-1", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
        ],
    )


async def build_system_with_a_laid_out_lh() -> ISystem:
    """One liquid handler whose deck layout resolves, so any run mode accepts it."""
    return await _build_system_with_lh(deck_layout="smc_v1", store_layouts={"smc_v1": _layout("STAR")})


async def _build_system_with_lh(
    *,
    deck_layout: str | None,
    store_layouts: dict[str, DeckLayoutConfig] | None = None,
) -> ISystem:
    """Build a System with one LiquidHandler + one transporter.

    The handler's ``deck_layout`` and the store contents are wired
    independently so tests can probe missing-declaration vs
    unresolved-name independently.
    """
    plate = create_test_plate_template("plate_96")

    store = deck_layout_file(store_layouts or {})
    lh = with_deck_layouts(LiquidHandler(
        "lh_1",
        deck_layout=deck_layout,
    ), store)
    # A multi-site LH has no bare-name teachpoint alias, so teach a
    # site-qualified point; a None layout keeps "lh_1/slot" (bare name works).
    resolved = await lh.resolve_deck_config_async()
    if resolved is not None:
        first_site = next(iter(enumerate_deck_sites(resolved)))[0]
        lh_teachpoint = f"lh_1/{first_site}"
    else:
        lh_teachpoint = "lh_1"
    transporter = create_test_transporter("robot1", [lh_teachpoint, "pad1"])

    registry = ResourceRegistry()
    registry.add_resource(lh)
    registry.add_resource(transporter)
    pool = ResourcePool("lh_1", [lh])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"lh_1": lh}, pads=["pad1"])

    @orca.action(device=pool, inputs=[plate])
    async def lh_action(ctx: ActionContext) -> None:
        del ctx

    @orca.method
    async def lh_method(
        ctx: MethodContext,
    ) -> AsyncGenerator[ActionTemplate, None]:
        del ctx
        yield lh_action

    pad_loc = system_map.get_location("pad1")

    @orca.thread(labware=plate, start=pad_loc, end=pad_loc)
    async def plate_thread(
        ctx: ThreadContext,
    ) -> AsyncGenerator[MethodTemplate, None]:
        del ctx
        yield lh_method

    workflow = WorkflowTemplate("deck_layout_test_wf")
    workflow.add_thread(plate_thread, is_start=True)

    builder = SdkToSystemBuilder(
        name="deck_layout_test",
        description="",
        labwares=[plate],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=EventBus(), config=OrcaConfig(), ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(),
        unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    return builder.get_system()


def _connection_card_for_lh(name: str = "lh_1") -> ConnectionCard:
    """Connection card claiming every interface the topology declares for a
    LiquidHandler, so ``unreachable`` passes when we only want to probe the
    deck-layout check; the superset check refuses anything narrower."""
    return ConnectionCard(
        name=name,
        client_id=f"client-{name}",
        connection_id=f"conn-{name}",
        last_heartbeat=datetime.now(timezone.utc),
        advertised_kind="SimLiquidHandlerDriver",
        advertised_interfaces=frozenset({"IGantryParking", "ILiquidHandler", "IProtocolRunner"}),
    )


async def test_a_check_run_rejects_lh_with_no_deck_layout_as_a_sim_run_does() -> None:
    system = await _build_system_with_lh(deck_layout=None)
    runtime = await memory_runtime(
        system,
        gateway_registry=NullGatewayRegistry(),
    )
    await runtime.start()
    try:
        with pytest.raises(DeckLayoutRequiredError) as excinfo:
            await runtime.submit_workflow("deck_layout_test_wf", mode=WorkflowRunMode.CHECK_RUN)
        assert "lh_1" in str(excinfo.value)
    finally:
        await runtime.shutdown()


async def test_sim_rejects_lh_with_no_deck_layout() -> None:
    """SIM must reject a LiquidHandler whose constructor
    omitted ``deck_layout``. The error names the offending device."""
    system = await _build_system_with_lh(deck_layout=None)
    transporter_card = ConnectionCard(
        name="robot1",
        client_id="client-robot1",
        connection_id="conn-robot1",
        last_heartbeat=datetime.now(timezone.utc),
        advertised_kind="SimTransporterDriver",
        advertised_interfaces=TRANSPORTER_MOCK_INTERFACES,
    )
    source = FakeConnectionSource(
        [_connection_card_for_lh("lh_1"), transporter_card],
    )
    runtime = await memory_runtime(
        system,
        gateway_registry=NullGatewayRegistry(),
        connection_source=source,
    )
    await runtime.start()
    try:
        with pytest.raises(DeckLayoutRequiredError) as excinfo:
            await runtime.submit_workflow(
                "deck_layout_test_wf",
                mode=WorkflowRunMode.SIM,
            )
        err = excinfo.value
        assert err.mode is WorkflowRunMode.SIM
        assert err.missing_declaration == ["lh_1"]
        assert err.unresolved_layout == []
        assert "lh_1" in str(err)
        assert "SIM" in str(err)
    finally:
        await runtime.shutdown()


async def test_live_rejects_lh_with_no_deck_layout() -> None:
    """LIVE submissions enforce the same contract as SIM."""
    system = await _build_system_with_lh(deck_layout=None)
    transporter_card = ConnectionCard(
        name="robot1",
        client_id="client-robot1",
        connection_id="conn-robot1",
        last_heartbeat=datetime.now(timezone.utc),
        advertised_kind="SimTransporterDriver",
        advertised_interfaces=TRANSPORTER_MOCK_INTERFACES,
    )
    source = FakeConnectionSource(
        [_connection_card_for_lh("lh_1"), transporter_card],
    )
    runtime = await memory_runtime(
        system,
        gateway_registry=NullGatewayRegistry(),
        connection_source=source,
    )
    await runtime.start()
    try:
        with pytest.raises(DeckLayoutRequiredError) as excinfo:
            await runtime.submit_workflow(
                "deck_layout_test_wf",
                mode=WorkflowRunMode.LIVE,
            )
        assert excinfo.value.mode is WorkflowRunMode.LIVE
    finally:
        await runtime.shutdown()


async def test_sim_rejects_lh_with_unresolved_deck_layout() -> None:
    """A handler with ``deck_layout='missing'`` and no such entry in
    the store must fail with an ``unresolved_layout`` entry."""
    system = await _build_system_with_lh(
        deck_layout="missing",
        store_layouts={"other_layout": _layout()},
    )
    transporter_card = ConnectionCard(
        name="robot1",
        client_id="client-robot1",
        connection_id="conn-robot1",
        last_heartbeat=datetime.now(timezone.utc),
        advertised_kind="SimTransporterDriver",
        advertised_interfaces=TRANSPORTER_MOCK_INTERFACES,
    )
    source = FakeConnectionSource(
        [_connection_card_for_lh("lh_1"), transporter_card],
    )
    runtime = await memory_runtime(
        system,
        gateway_registry=NullGatewayRegistry(),
        connection_source=source,
    )
    await runtime.start()
    try:
        with pytest.raises(DeckLayoutRequiredError) as excinfo:
            await runtime.submit_workflow(
                "deck_layout_test_wf",
                mode=WorkflowRunMode.SIM,
            )
        err = excinfo.value
        assert err.missing_declaration == []
        assert err.unresolved_layout == [("lh_1", "missing")]
        assert "missing" in str(err)
        assert "lh_1" in str(err)
    finally:
        await runtime.shutdown()


async def test_sim_accepts_lh_with_resolvable_deck_layout() -> None:
    """A handler with a deck_layout that resolves in its store passes
    the deck-layout check and submits cleanly under SIM."""
    system = await _build_system_with_lh(
        deck_layout="smc_v1",
        store_layouts={"smc_v1": _layout("STAR")},
    )
    transporter_card = ConnectionCard(
        name="robot1",
        client_id="client-robot1",
        connection_id="conn-robot1",
        last_heartbeat=datetime.now(timezone.utc),
        advertised_kind="SimTransporterDriver",
        advertised_interfaces=TRANSPORTER_MOCK_INTERFACES,
    )
    source = FakeConnectionSource(
        [_connection_card_for_lh("lh_1"), transporter_card],
    )
    runtime = await memory_runtime(
        system,
        gateway_registry=NullGatewayRegistry(),
        connection_source=source,
    )
    await runtime.start()
    try:
        record = await runtime.submit_workflow(
            "deck_layout_test_wf",
            mode=WorkflowRunMode.SIM,
        )
        assert record.workflow_name == "deck_layout_test_wf"
    finally:
        await runtime.shutdown()

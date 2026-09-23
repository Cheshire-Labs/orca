"""SystemRuntime.shutdown disposes the SQLite store engines it OWNS, so their
aiosqlite worker threads do not outlive the event loop and error at teardown
(``RuntimeError: Event loop is closed``). Injected stores are the injector's to
close and must survive the runtime's shutdown.
"""

import asyncio
import inspect
import threading
from typing import get_type_hints
from collections.abc import Sequence

import pytest

from orca.devices.devices import LiquidHandler
from orca.resource_models.resource_pool import ResourcePool
from orca.resource_models.resources import IResource
from orca.runtime.db import create_memory_engine
from orca.runtime.execution_record_service import ExecutionRecordService
from orca.runtime.deck_layout_service import seeded_deck_layout_service
from orca.runtime.labware_catalog_service import seeded_labware_catalog_service
from orca.runtime.runtime_interface import ISystemRuntime
from orca.runtime.sqlite_execution_record_store import SqliteExecutionRecordStore
from orca.runtime.sqlite_incident_store import SqliteIncidentStore
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system_interface import ISystem
from tests.test_helpers import (
    create_test_device,
    create_test_plate_template,
    create_test_transporter,
    wire_system_map,
)


def _worker_idents() -> set[int]:
    return {
        t.ident
        for t in threading.enumerate()
        if "_connection_worker" in (t.name or "") and t.ident is not None
    }


async def _settle_gone(idents: set[int]) -> bool:
    for _ in range(100):
        await asyncio.sleep(0.02)
        if idents.isdisjoint(_worker_idents()):
            return True
    return False


async def _build_minimal_system(
    extra_resources: Sequence[IResource] = (),
) -> ISystem:
    device = create_test_device("shaker1")
    transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    for resource in extra_resources:
        registry.add_resource(resource)
    registry.add_resource_pool(ResourcePool("shaker1", [device]))

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1"])

    builder = SdkToSystemBuilder(
        name="t", description="", labwares=[plate],
        resources_registry=registry, system_map=system_map,
        workflows=[WorkflowTemplate("wf")], event_bus=EventBus(),
    )
    await builder.bind_labwares()
    return builder.get_system()


async def test_sqlite_store_aclose_reclaims_worker_thread() -> None:
    """Dropping the StaticPool skip: a store's aclose disposes its in-memory
    engine so the aiosqlite worker thread does not outlive the loop."""
    pre = _worker_idents()
    store = SqliteIncidentStore(create_memory_engine())
    await store.create_schema()
    mine = _worker_idents() - pre
    assert mine, "expected a new aiosqlite worker thread while the engine is live"

    await store.aclose()

    assert await _settle_gone(mine), (
        "store.aclose did not reclaim its aiosqlite worker thread"
    )


async def test_shutdown_disposes_transporter_teachpoint_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default per-transporter TeachpointService is SQLite-backed; shutdown
    must aclose it, else its aiosqlite worker thread posts to a closed loop."""
    system = await _build_minimal_system()
    runtime = SystemRuntime(system)
    await runtime.start()

    teachpoint_store = system.transporters[0].teachpoint_store
    closed = {"value": False}
    original_aclose = teachpoint_store.aclose

    async def _tracked_aclose() -> None:
        closed["value"] = True
        await original_aclose()

    monkeypatch.setattr(teachpoint_store, "aclose", _tracked_aclose)

    await runtime.shutdown()

    assert closed["value"], (
        "SystemRuntime.shutdown must dispose the transporter's teachpoint store engine"
    )


async def test_shutdown_disposes_owned_lazy_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lazily minted catalog is the runtime's; shutdown disposes it."""
    system = await _build_minimal_system()
    runtime = SystemRuntime(system)
    await runtime.start()

    catalog = runtime.labware_catalog_store  # lazy mint -> runtime-owned
    closed = {"value": False}
    original_aclose = catalog.aclose

    async def _tracked_aclose() -> None:
        closed["value"] = True
        await original_aclose()

    monkeypatch.setattr(catalog, "aclose", _tracked_aclose)

    await runtime.shutdown()

    assert closed["value"], "runtime must dispose the catalog it lazily minted"


async def test_shutdown_does_not_dispose_injected_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An injected catalog is the injector's (e.g. the daemon factory / a hosted deployment
    Postgres). The runtime must NOT dispose it, or a rebuild reuses a dead engine
    and operator catalog edits are lost."""
    system = await _build_minimal_system()
    catalog = seeded_labware_catalog_service()
    runtime = SystemRuntime(system, labware_catalog_store=catalog)
    await runtime.start()

    closed = {"value": False}
    original_aclose = catalog.aclose

    async def _tracked_aclose() -> None:
        closed["value"] = True
        await original_aclose()

    monkeypatch.setattr(catalog, "aclose", _tracked_aclose)

    await runtime.shutdown()

    assert not closed["value"], (
        "runtime must NOT dispose an injected catalog (the injector owns its lifecycle)"
    )


async def test_shutdown_disposes_liquid_handler_deck_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A LiquidHandler with a SQLite deck store: shutdown must aclose it (the
    deck branch of the device-store walk, distinct from the teachpoint branch)."""
    lh = LiquidHandler(
        name="lh1",
        sim=True,
        deck_layout_store=seeded_deck_layout_service({}),
    )
    system = await _build_minimal_system(extra_resources=[lh])
    runtime = SystemRuntime(system)
    await runtime.start()

    closed = {"value": False}
    original_aclose = lh.deck_layout_store.aclose

    async def _tracked_aclose() -> None:
        closed["value"] = True
        await original_aclose()

    monkeypatch.setattr(lh.deck_layout_store, "aclose", _tracked_aclose)

    await runtime.shutdown()

    assert closed["value"], (
        "SystemRuntime.shutdown must dispose the liquid handler's deck store engine"
    )


async def test_factory_aclose_disposes_owned_stores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The daemon lifespan relies on factory.aclose to release its daemon-lifetime
    engines: the access-config service it built and the catalog it owns."""
    factory = InMemoryRuntimeStoreFactory()
    access_configs = factory.access_configs()
    catalog = factory.labware_catalog_store()

    ac_closed = {"value": False}
    catalog_closed = {"value": False}
    ac_original = access_configs.aclose
    catalog_original = catalog.aclose

    async def _ac_aclose() -> None:
        ac_closed["value"] = True
        await ac_original()

    async def _catalog_aclose() -> None:
        catalog_closed["value"] = True
        await catalog_original()

    monkeypatch.setattr(access_configs, "aclose", _ac_aclose)
    monkeypatch.setattr(catalog, "aclose", _catalog_aclose)

    await factory.aclose()

    assert ac_closed["value"], "factory.aclose must dispose the access-config engine it built"
    assert catalog_closed["value"], "factory.aclose must dispose the catalog it owns"


async def test_factory_aclose_does_not_dispose_injected_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An injected catalog (the daemon shares ONE across mount/unload) is the
    injector's; factory.aclose must not dispose it (the _owns_catalog guard)."""
    injected = seeded_labware_catalog_service()
    factory = InMemoryRuntimeStoreFactory(catalog_service=injected)

    closed = {"value": False}
    original_aclose = injected.aclose

    async def _tracked_aclose() -> None:
        closed["value"] = True
        await original_aclose()

    monkeypatch.setattr(injected, "aclose", _tracked_aclose)

    await factory.aclose()

    assert not closed["value"], (
        "factory.aclose must NOT dispose an injected catalog (owns_catalog is False)"
    )


async def test_operator_contract_exposes_the_injected_execution_record_service() -> None:
    """A surface typed on ``ISystemRuntime`` reads terminal executions through
    the SAME service the deployment injected.

    The get/list/detail execution Operations take their terminal-record
    fallbacks from here. If the contract hid this Service, a surface holding
    only the protocol would report a completed execution as missing the moment
    the runtime evicts it -- and a hosted deployment sourcing the Service some
    other way could read a DIFFERENT store than the runtime writes to.
    """
    # hasattr alone would pass on a member declared with the wrong type, and
    # this repo's CI runs no type checker, so pin the annotation too.
    prop = inspect.getattr_static(ISystemRuntime, "execution_records")
    declared = get_type_hints(prop.fget).get("return")
    assert declared is ExecutionRecordService, (
        "the operator contract must declare execution_records as an "
        f"ExecutionRecordService; got {declared!r}. A surface typed on the "
        "protocol cannot wire the terminal fallback without it."
    )

    injected = ExecutionRecordService(
        SqliteExecutionRecordStore(create_memory_engine())
    )
    system = await _build_minimal_system()
    runtime = SystemRuntime(system, execution_record_service=injected)

    assert runtime.execution_records is injected

    await runtime.shutdown(confirm=True)
    await injected.aclose()


async def test_runtime_mints_an_execution_record_service_when_none_injected() -> None:
    """The contract's Service is never absent: a runtime built without one
    mints its own, so surfaces can always wire the terminal fallbacks."""
    system = await _build_minimal_system()
    runtime = SystemRuntime(system)

    minted = runtime.execution_records
    assert isinstance(minted, ExecutionRecordService)

    await runtime.shutdown(confirm=True)

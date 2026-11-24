"""Every daemon route in front of an audited verb carries an optional reason
onto the audit row. A body route takes it in the body; a delete, or a route
whose body is the resource itself, takes it as a query parameter."""

from collections.abc import AsyncIterator
from contextlib import AbstractContextManager
from unittest.mock import AsyncMock, patch

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from orca.daemon.app import create_app
from orca.runtime.db import create_memory_engine
from orca.runtime.incident_store import (
    IncidentCategory,
    IncidentSeverity,
    OtherIncidentDetail,
    RecoveryAction,
)
from orca.runtime.incident_service import IncidentService
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.sqlite_incident_store import SqliteIncidentStore
from orca.runtime.system_runtime import SystemRuntime

from tests.daemon.test_routes_calibration_writes import _deck_layout_body
from tests.daemon.test_routes_device_write import _connected
from tests.daemon.test_routes_teachpoint_labware import (
    CREATE_ON_ROBOT1,
    _access_config_body,
    _create_body,
)
from tests.test_system_runtime import _build_simple_system
from tests.audit_capture import AuditCapture
from tests.memory_activity import memory_daemon_system


def _reason_of(audit: AuditCapture, action_name: str) -> str | None:
    rows = [r for r in audit.rows() if r.action_name == action_name]
    assert len(rows) == 1, f"expected one {action_name} row, got {rows}"
    return rows[0].reason


async def test_variable_routes(audit: AuditCapture, client: AsyncClient) -> None:
    put = await client.put("/api/variables/global/speed", json={"value": 3, "reason": "set"})
    delete = await client.delete("/api/variables/global/speed", params={"reason": "unset"})

    assert put.status_code == 200, put.text
    assert delete.status_code == 200, delete.text
    assert _reason_of(audit, "variables.set_global") == "set"
    assert _reason_of(audit, "variables.unset_global") == "unset"


async def test_access_config_routes(audit: AuditCapture, client: AsyncClient) -> None:
    body = _access_config_body("vert_b")
    created = await client.post("/api/access-configs", json={**body, "reason": "add"})
    updated = await client.put(
        "/api/access-configs/vert_b", json={"gripper_offset": 30.0, "reason": "edit"},
    )
    deleted = await client.delete("/api/access-configs/vert_b", params={"reason": "drop"})

    assert created.status_code == 201, created.text
    assert updated.status_code == 200, updated.text
    assert deleted.status_code == 204, deleted.text
    assert _reason_of(audit, "access_configs.add") == "add"
    assert _reason_of(audit, "access_configs.update") == "edit"
    assert _reason_of(audit, "access_configs.delete") == "drop"


async def test_teachpoint_routes(audit: AuditCapture, client: AsyncClient) -> None:
    await client.post("/api/access-configs", json=_access_config_body())
    created = await client.post(CREATE_ON_ROBOT1, json=_create_body(reason="taught"))
    updated = await client.put(
        "/api/teachpoints/robot1/hotel_3",
        json={"coords": {"x": 5.0, "y": 2.0, "z": 3.0, "yaw": 0.0, "pitch": 90.0, "roll": 180.0},
              "reason": "re-taught"},
    )
    override = await client.patch(
        "/api/teachpoint-overrides/robot1/costar_96/hotel_3",
        json={"set": {"speed": 20.0}, "reason": "slow for this plate"},
    )
    cleared = await client.delete(
        "/api/teachpoint-overrides/robot1/costar_96/hotel_3", params={"reason": "back to normal"},
    )
    deleted = await client.delete("/api/teachpoints/robot1/hotel_3", params={"reason": "gone"})

    for resp in (created, updated, override, cleared, deleted):
        assert resp.status_code < 300, resp.text
    assert _reason_of(audit, "teachpoints.add") == "taught"
    assert _reason_of(audit, "teachpoints.update") == "re-taught"
    assert _reason_of(audit, "teachpoints.apply_labware_override") == "slow for this plate"
    assert _reason_of(audit, "teachpoints.clear_labware_override") == "back to normal"
    assert _reason_of(audit, "teachpoints.delete") == "gone"


async def test_deck_layout_routes(audit: AuditCapture, lh_daemon_client: AsyncClient) -> None:
    body = _deck_layout_body()
    created = await lh_daemon_client.put(
        "/api/deck-layouts/lh1/primary", json={"config": body, "reason": "add"},
    )
    updated = await lh_daemon_client.put(
        "/api/deck-layouts/lh1/primary", json={"config": body, "reason": "edit"},
    )
    deleted = await lh_daemon_client.delete(
        "/api/deck-layouts/lh1/primary", params={"reason": "drop"},
    )

    assert created.status_code == 200, created.text
    assert updated.status_code == 200, updated.text
    assert deleted.status_code == 204, deleted.text
    assert _reason_of(audit, "deck_layouts.add") == "add"
    assert _reason_of(audit, "deck_layouts.update") == "edit"
    assert _reason_of(audit, "deck_layouts.delete") == "drop"


def _sent_through_the_runner() -> AbstractContextManager[AsyncMock]:
    """The runner's send stage, stood in for below its audit row: (device, command, params) per call."""
    return patch(
        "orca.gateway.operator_commands.OperatorDeviceCommands._send",
        new=AsyncMock(return_value=(None, 0.0)),
    )


async def test_clear_all_and_connect_routes(audit: AuditCapture, client: AsyncClient) -> None:
    cleared = await client.post(
        "/api/labware-instances/clear-all", json={"force": False, "reason": "bench emptied"},
    )
    with _sent_through_the_runner():
        connected = await client.post(
            "/api/devices/shaker1/connect", json={"reason": "bring up"},
        )

    assert cleared.status_code == 200, cleared.text
    assert connected.status_code == 200, connected.text
    assert _reason_of(audit, "labware.clear_all") == "bench emptied"
    assert _reason_of(audit, "device.connect") == "bring up"


async def test_clear_submission_and_discharge_routes(
    audit: AuditCapture,
    client: AsyncClient, runtime: SystemRuntime,
) -> None:
    snap = await runtime.submissions.submit_group(
        workflow_name="simple_workflow", mode=WorkflowRunMode.CHECK_RUN, confirm=True,
    )
    await runtime.wait(snap.execution_id)
    plate = await runtime.labware.register("plate_96", location="pad1", confirm=True)

    cleared = await client.post(
        f"/api/submissions/{snap.id}/clear-labware",
        json={"reason": "run done"},
    )
    discharged = await client.post(
        f"/api/labware-instances/{plate.id}/discharge",
        json={"reason": "picked it up"},
    )

    assert cleared.status_code == 200, cleared.text
    assert discharged.status_code == 200, discharged.text
    assert _reason_of(audit, "labware.clear_submission") == "run done"
    assert _reason_of(audit, "labware.discharge") == "picked it up"


async def test_device_routes(audit: AuditCapture, client: AsyncClient) -> None:
    with _sent_through_the_runner():
        initialized = await client.post(
            "/api/devices/shaker1/initialize", json={"reason": "bring up"},
        )
        with _connected("shaker1"):
            batched = await client.post(
                "/api/devices/shaker1/commands",
                json={"commands": [{"command": "shaker.shake", "params": {"speed": 500, "duration": 1}}],
                      "reason": "test shake"},
            )
        disconnected = await client.post(
            "/api/devices/shaker1/disconnect", json={"reason": "done"},
        )

    for resp in (initialized, batched, disconnected):
        assert resp.status_code == 200, resp.text
    assert _reason_of(audit, "device.initialize") == "bring up"
    assert _reason_of(audit, "device.execute") == "test shake"
    assert _reason_of(audit, "device.disconnect") == "done"


async def test_incident_routes(audit: AuditCapture, client: AsyncClient) -> None:
    acked = await client.post("/api/incidents/ack-all", json={"reason": "reviewed"})

    assert acked.status_code == 200, acked.text
    assert _reason_of(audit, "incidents.acknowledge_all") == "reviewed"


async def test_execution_and_submission_variable_routes(
    audit: AuditCapture,
    client: AsyncClient, runtime: SystemRuntime,
) -> None:
    runtime.system.variable_store.create_execution("e1", "simple_workflow")
    set_exec = await client.put("/api/executions/e1/variables/speed", json={"value": 3, "reason": "set"})
    set_sub = await client.put(
        "/api/executions/e1/submissions/s1/variables/speed", json={"value": 4, "reason": "set one"},
    )
    unset_sub = await client.delete(
        "/api/executions/e1/submissions/s1/variables/speed", params={"reason": "unset one"},
    )
    unset_exec = await client.delete("/api/executions/e1/variables/speed", params={"reason": "unset"})

    for resp in (set_exec, set_sub, unset_sub, unset_exec):
        assert resp.status_code == 200, resp.text
    assert _reason_of(audit, "variables.set") == "set"
    assert _reason_of(audit, "variables.set_submission") == "set one"
    assert _reason_of(audit, "variables.unset_submission") == "unset one"
    assert _reason_of(audit, "variables.unset") == "unset"


@pytest_asyncio.fixture
async def incident_client() -> AsyncIterator[tuple[AsyncClient, str]]:
    """A daemon whose runtime holds one unacknowledged incident."""
    service = IncidentService(SqliteIncidentStore(create_memory_engine()))
    system, _ = await _build_simple_system()
    injected = await memory_daemon_system(system, incident_service=service)
    rt = injected.runtime
    await rt.start()
    incident = service.record(
        category=IncidentCategory.OTHER, severity=IncidentSeverity.INFO,
        message="m", detail=OtherIncidentDetail(message_extra=""),
        recovery_action=RecoveryAction.NONE,
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=create_app(injected=injected)),
            base_url="http://daemon.test",
        ) as c:
            yield c, incident.id
    finally:
        await rt.shutdown(confirm=True)


async def test_single_incident_ack_route(audit: AuditCapture, incident_client: tuple[AsyncClient, str]) -> None:
    client, incident_id = incident_client
    acked = await client.post(f"/api/incidents/{incident_id}/ack", json={"reason": "seen it"})

    assert acked.status_code == 200, acked.text
    assert _reason_of(audit, "incidents.acknowledge") == "seen it"

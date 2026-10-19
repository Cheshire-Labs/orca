"""Every daemon route in front of an audited verb carries an optional reason
onto the audit row. A body route takes it in the body; a delete, or a route
whose body is the resource itself, takes it as a query parameter."""

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from orca.daemon.app import create_app
from orca.gateway.adhoc import AdhocResult
from orca.runtime.danger import clear_audit_trail, list_audit_entries
from orca.runtime.db import create_memory_engine
from orca.runtime.incident_store import (
    IncidentCategory,
    IncidentSeverity,
    OtherIncidentDetail,
    RecoveryAction,
)
from orca.runtime.incident_service import IncidentService
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.sqlite_incident_store import SqliteIncidentStore
from orca.runtime.system_runtime import SystemRuntime

from tests.daemon.test_routes_calibration_writes import _deck_layout_body
from tests.daemon.test_routes_device_write import _connected_card
from tests.daemon.test_routes_mount import _TOPOLOGY_SPEC, _WORKFLOW_SPEC
from tests.daemon.test_routes_teachpoint_labware import (
    _access_config_body,
    _create_body,
)
from tests.test_system_runtime import _build_simple_system


def _reason_of(action_name: str) -> str | None:
    rows = [r for r in list_audit_entries() if r.action_name == action_name]
    assert len(rows) == 1, f"expected one {action_name} row, got {rows}"
    return rows[0].reason


@pytest.fixture(autouse=True)
def _fresh_trail() -> None:
    clear_audit_trail()


async def test_variable_routes(client: AsyncClient) -> None:
    put = await client.put("/variables/global/speed", json={"value": 3, "reason": "set"})
    delete = await client.delete("/variables/global/speed", params={"reason": "unset"})

    assert put.status_code == 200, put.text
    assert delete.status_code == 200, delete.text
    assert _reason_of("variables.set_global") == "set"
    assert _reason_of("variables.unset_global") == "unset"


async def test_access_config_routes(client: AsyncClient) -> None:
    body = _access_config_body("vert_b")
    created = await client.post("/access-configs", json=body, params={"reason": "add"})
    body["gripper_offset"] = 30.0
    updated = await client.put("/access-configs/vert_b", json=body, params={"reason": "edit"})
    deleted = await client.delete("/access-configs/vert_b", params={"reason": "drop"})

    assert created.status_code == 201, created.text
    assert updated.status_code == 200, updated.text
    assert deleted.status_code == 204, deleted.text
    assert _reason_of("access_configs.add") == "add"
    assert _reason_of("access_configs.update") == "edit"
    assert _reason_of("access_configs.delete") == "drop"


async def test_teachpoint_routes(client: AsyncClient) -> None:
    await client.post("/access-configs", json=_access_config_body())
    created = await client.post("/teachpoints", json=_create_body(reason="taught"))
    updated = await client.put(
        "/teachpoints/robot1/hotel_3",
        json={"coords": {"x": 5.0, "y": 2.0, "z": 3.0, "yaw": 0.0, "pitch": 90.0, "roll": 180.0},
              "reason": "re-taught"},
    )
    override = await client.patch(
        "/teachpoints/robot1/hotel_3/labware/costar_96",
        json={"set": {"speed": 20.0}, "reason": "slow for this plate"},
    )
    cleared = await client.delete(
        "/teachpoints/robot1/hotel_3/labware/costar_96", params={"reason": "back to normal"},
    )
    deleted = await client.delete("/teachpoints/robot1/hotel_3", params={"reason": "gone"})

    for resp in (created, updated, override, cleared, deleted):
        assert resp.status_code < 300, resp.text
    assert _reason_of("teachpoints.add") == "taught"
    assert _reason_of("teachpoints.update") == "re-taught"
    assert _reason_of("teachpoints.apply_labware_override") == "slow for this plate"
    assert _reason_of("teachpoints.clear_labware_override") == "back to normal"
    assert _reason_of("teachpoints.delete") == "gone"


async def test_deck_layout_routes(lh_daemon_client: AsyncClient) -> None:
    body = _deck_layout_body()
    created = await lh_daemon_client.post(
        "/deck-layouts/lh1/primary", json=body, params={"reason": "add"},
    )
    updated = await lh_daemon_client.put(
        "/deck-layouts/lh1/primary", json=body, params={"reason": "edit"},
    )
    deleted = await lh_daemon_client.delete(
        "/deck-layouts/lh1/primary", params={"reason": "drop"},
    )

    assert created.status_code == 201, created.text
    assert updated.status_code == 200, updated.text
    assert deleted.status_code == 204, deleted.text
    assert _reason_of("deck_layouts.add") == "add"
    assert _reason_of("deck_layouts.update") == "edit"
    assert _reason_of("deck_layouts.delete") == "drop"


async def test_clear_all_and_connect_routes(client: AsyncClient) -> None:
    cleared = await client.post(
        "/labware/runtime/clear-all", json={"force": False, "reason": "bench emptied"},
    )
    connected = await client.post("/devices/shaker1/connect", json={"reason": "bring up"})

    assert cleared.status_code == 200, cleared.text
    assert connected.status_code == 200, connected.text
    assert _reason_of("labware.clear_all") == "bench emptied"
    assert _reason_of("device.connect") == "bring up"


async def test_submit_route(client: AsyncClient, runtime: SystemRuntime) -> None:
    submitted = await client.post("/executions", json={
        "workflow_name": "simple_workflow", "run_mode": "PURE_SIM", "reason": "first plate",
    })

    assert submitted.status_code == 200, submitted.text
    await runtime.wait(submitted.json()["id"])
    assert _reason_of("submission.submit_workflow") == "first plate"


async def test_clear_submission_and_discharge_routes(
    client: AsyncClient, runtime: SystemRuntime,
) -> None:
    snap = await runtime.submissions.submit_group(
        workflow_name="simple_workflow", mode=WorkflowRunMode.PURE_SIM, confirm=True,
    )
    await runtime.wait(snap.execution_id)
    plate = await runtime.labware.register("plate_96", location="pad1", confirm=True)

    cleared = await client.post(
        "/labware/runtime/clear-submission",
        params={"submission_id": snap.id}, json={"reason": "run done"},
    )
    discharged = await client.post(
        "/labware/runtime/discharge",
        params={"labware_id": plate.id}, json={"reason": "picked it up"},
    )

    assert cleared.status_code == 200, cleared.text
    assert discharged.status_code == 200, discharged.text
    assert _reason_of("labware.clear_submission") == "run done"
    assert _reason_of("labware.discharge") == "picked it up"


async def test_device_routes(client: AsyncClient) -> None:
    initialized = await client.post("/devices/shaker1/initialize", json={"reason": "bring up"})
    invoked = await client.post(
        "/devices/shaker1/invoke",
        json={"capability": "shake", "kwargs": {"speed": 500, "duration": 1}, "reason": "test shake"},
    )
    disconnected = await client.post("/devices/shaker1/disconnect", json={"reason": "done"})

    for resp in (initialized, invoked, disconnected):
        assert resp.status_code == 200, resp.text
    assert _reason_of("device.initialize") == "bring up"
    assert _reason_of("device.invoke") == "test shake"
    assert _reason_of("device.disconnect") == "done"


async def test_incident_routes(client: AsyncClient) -> None:
    acked = await client.post("/incidents/ack-all", params={"reason": "reviewed"})

    assert acked.status_code == 200, acked.text
    assert _reason_of("incidents.acknowledge_all") == "reviewed"


async def test_execution_and_submission_variable_routes(
    client: AsyncClient, runtime: SystemRuntime,
) -> None:
    runtime.system.variable_store.create_execution("e1", "simple_workflow")
    set_exec = await client.put("/variables/e1/speed", json={"value": 3, "reason": "set"})
    set_sub = await client.put(
        "/variables/submissions/e1/s1/speed", json={"value": 4, "reason": "set one"},
    )
    unset_sub = await client.delete(
        "/variables/submissions/e1/s1/speed", params={"reason": "unset one"},
    )
    unset_exec = await client.delete("/variables/e1/speed", params={"reason": "unset"})

    for resp in (set_exec, set_sub, unset_sub, unset_exec):
        assert resp.status_code == 200, resp.text
    assert _reason_of("variables.set") == "set"
    assert _reason_of("variables.set_submission") == "set one"
    assert _reason_of("variables.unset_submission") == "unset one"
    assert _reason_of("variables.unset") == "unset"


async def test_device_execute_route(client: AsyncClient) -> None:
    with patch(
        "orca.runtime.facades.devices.device_connection_tracker.peek_snapshot",
        return_value=_connected_card(),
    ), patch(
        "orca.runtime.facades.devices.adhoc.execute_adhoc_command",
        new=AsyncMock(return_value=AdhocResult(raw=None, interfaces=frozenset())),
    ):
        resp = await client.post(
            "/devices/flex_remote/execute",
            json={"command": "gripper.ungrip", "reason": "let go"},
        )

    assert resp.status_code == 200, resp.text
    assert _reason_of("device.execute") == "let go"


async def test_workflow_register_route(empty_client: AsyncClient) -> None:
    await empty_client.post("/mount-topology", json={"spec": _TOPOLOGY_SPEC, "sim": True})
    resp = await empty_client.post(
        "/workflows", json={"spec": _WORKFLOW_SPEC, "reason": "new assay"},
    )
    await empty_client.post("/unload")

    assert resp.status_code == 200, resp.text
    assert _reason_of("workflow_template.add") == "new assay"


@pytest_asyncio.fixture
async def incident_client() -> AsyncIterator[tuple[AsyncClient, str]]:
    """A daemon whose runtime holds one unacknowledged incident."""
    service = IncidentService(SqliteIncidentStore(create_memory_engine()))
    system, _ = await _build_simple_system()
    rt = SystemRuntime(system, incident_service=service)
    await rt.start()
    incident = service.record(
        category=IncidentCategory.OTHER, severity=IncidentSeverity.INFO,
        message="m", detail=OtherIncidentDetail(message_extra=""),
        recovery_action=RecoveryAction.NONE,
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=create_app(initial_system_runtime=rt)),
            base_url="http://daemon.test",
        ) as c:
            yield c, incident.id
    finally:
        await rt.shutdown(confirm=True)


async def test_single_incident_ack_route(incident_client: tuple[AsyncClient, str]) -> None:
    client, incident_id = incident_client
    acked = await client.post(f"/incidents/{incident_id}/ack", params={"reason": "seen it"})

    assert acked.status_code == 200, acked.text
    assert _reason_of("incidents.acknowledge") == "seen it"

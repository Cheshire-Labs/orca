"""The daemon's hand-written submit, clear, discharge and connect routes write an
audit row carrying the optional reason, the same as their Operation versions."""

from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from orca.daemon.app import create_app
from orca.runtime.danger import clear_audit_trail, list_audit_entries
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import SystemRuntime

from tests.test_system_runtime import _build_simple_system


@pytest_asyncio.fixture
async def runtime() -> AsyncIterator[SystemRuntime]:
    system, _ = await _build_simple_system()
    rt = SystemRuntime(system, labware_store=InMemoryLabwareStore())
    await rt.start()
    try:
        yield rt
    finally:
        await rt.shutdown(confirm=True)


@pytest_asyncio.fixture
async def daemon(runtime: SystemRuntime) -> AsyncIterator[AsyncClient]:
    app = create_app(initial_system_runtime=runtime)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://daemon.test",
    ) as c:
        yield c


def _reason_of(action_name: str) -> str | None:
    [row] = [r for r in list_audit_entries() if r.action_name == action_name]
    return row.reason


async def test_clear_all_and_connect_record_the_reason(daemon: AsyncClient) -> None:
    clear_audit_trail()

    cleared = await daemon.post(
        "/labware/runtime/clear-all", json={"force": False, "reason": "bench emptied"},
    )
    connected = await daemon.post(
        "/devices/shaker1/connect", json={"reason": "bring up"},
    )

    assert cleared.status_code == 200, cleared.text
    assert connected.status_code == 200, connected.text
    assert _reason_of("labware.clear_all") == "bench emptied"
    assert _reason_of("device.connect") == "bring up"


async def test_submit_writes_a_row_carrying_the_reason(
    daemon: AsyncClient, runtime: SystemRuntime,
) -> None:
    clear_audit_trail()

    submitted = await daemon.post("/executions", json={
        "workflow_name": "simple_workflow", "run_mode": "PURE_SIM", "reason": "first plate",
    })

    assert submitted.status_code == 200, submitted.text
    await runtime.wait(submitted.json()["id"])
    assert _reason_of("submission.submit_workflow") == "first plate"


async def test_clear_submission_and_discharge_record_the_reason(
    daemon: AsyncClient, runtime: SystemRuntime,
) -> None:
    snap = await runtime.submissions.submit_group(
        workflow_name="simple_workflow", mode=WorkflowRunMode.PURE_SIM, confirm=True,
    )
    await runtime.wait(snap.execution_id)
    plate = await runtime.labware.register("plate_96", location="pad1", confirm=True)
    clear_audit_trail()

    cleared = await daemon.post(
        "/labware/runtime/clear-submission",
        params={"submission_id": snap.id}, json={"reason": "run done"},
    )
    discharged = await daemon.post(
        "/labware/runtime/discharge",
        params={"labware_id": plate.id}, json={"reason": "picked it up"},
    )

    assert cleared.status_code == 200, cleared.text
    assert discharged.status_code == 200, discharged.text
    assert _reason_of("labware.clear_submission") == "run done"
    assert _reason_of("labware.discharge") == "picked it up"

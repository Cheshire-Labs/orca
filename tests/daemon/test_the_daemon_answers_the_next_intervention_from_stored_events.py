"""GET /api/interventions/next on the daemon reads the events it stored, as Orca AI does."""

from pathlib import Path

from orca.daemon.app import create_app
from orca.daemon.daemon_state import DaemonState
from orca.events.execution_context import IncidentContext, WorkflowExecutionContext
from orca.events.runtime_event import RuntimeEvent
from tests.daemon.started_daemon import started_daemon


def _incident() -> RuntimeEvent:
    return RuntimeEvent(
        event_name="INCIDENT.inc-1.ACTION_FAILED", execution_id="exec-1", timestamp=0.0,
        entity_type="INCIDENT", entity_id="inc-1", status="ACTION_FAILED",
        context=IncidentContext(
            incident_id="inc-1", category="ACTION_FAILED", severity="ERROR",
            message="dispense failed", recovery_action="THREAD_RECOVER_RETRY",
            execution_id="exec-1", thread_id="t1",
        ),
    )


def _ordinary() -> RuntimeEvent:
    return RuntimeEvent(
        event_name="THREAD.t1.MOVING", execution_id="exec-1", timestamp=0.0,
        entity_type="THREAD", entity_id="t1", status="MOVING",
        context=WorkflowExecutionContext(execution_id="exec-1", workflow_name="wf"),
    )


async def test_the_daemon_answers_the_next_intervention_from_its_stored_events(tmp_path: Path) -> None:
    app = create_app(package_root=tmp_path)
    daemon: DaemonState = app.state.daemon
    async with started_daemon(app) as client:
        daemon.events.emit(_ordinary())
        daemon.events.emit(_incident())

        resp = await client.get("/api/interventions/next", params={"after_seq": 0, "timeout_seconds": 5})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["intervention"]["kind"] == "INCIDENT"
    assert body["intervention"]["incident_id"] == "inc-1"
    assert body["next_cursor"] == body["intervention"]["seq"]


async def test_an_intervention_stored_before_a_restart_is_still_found(tmp_path: Path) -> None:
    first = create_app(package_root=tmp_path)
    async with started_daemon(first) as client:
        first.state.daemon.events.emit(_incident())
        stored = (await client.get("/api/interventions/next", params={"after_seq": 0, "timeout_seconds": 5})).json()

    async with started_daemon(create_app(package_root=tmp_path)) as client:
        after_restart = (await client.get("/api/interventions/next", params={"after_seq": 0, "timeout_seconds": 1})).json()

    assert after_restart == stored

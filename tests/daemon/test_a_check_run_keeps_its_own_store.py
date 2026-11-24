"""A check run runs over a store of its own, seeded from the real record and kept after it ends."""

import asyncio
import json
import time
from datetime import datetime, timezone
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from httpx import AsyncClient
from pydantic import JsonValue

from orca.daemon.app import create_app
from orca.daemon.check_run_stores import SqliteCheckRunStores, check_runs_folder
from orca.daemon.daemon_state import DaemonState
from orca.events.execution_context import WorkflowExecutionContext
from orca.events.runtime_event import RuntimeEvent
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.check_runs import CheckRuns, _Running
from orca.runtime.execution_record import ExecutionRecord, ExecutionState
from orca.runtime.runtime_interface import ISystemRuntime
from orca.runtime.runtime_state import RuntimeState
from orca.runtime.sinks import CollectorSink
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import Topology
from orca.spawn import LEAVE_IN_PLACE
from orca.state.ops_store import OpsHistorySearchQuery
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from orca.workflow_models.workflow_templates import WorkflowTemplate
from tests.daemon.fixture_package import SIMPLE_WORKFLOW, write_package
from tests.daemon.held_action import HOLD
from tests.daemon.started_daemon import started_daemon
from tests.event_subscriber import RecordingSubscriber, sending
from tests.test_helpers import create_test_device, create_test_plate_template, create_test_transporter
import orca.orca as orca

_HERE = "tests.daemon.test_a_check_run_keeps_its_own_store"
_CHECK_RUN = {"workflow_name": "left_on_system", "run_mode": "CHECK_RUN"}
_WORKFLOWS = {
    **SIMPLE_WORKFLOW,
    "left_on_system": f"{_HERE}:build_workflow",
    "fails_on_the_shaker": f"{_HERE}:build_failing_workflow",
    "slow_left_on_system": f"{_HERE}:build_slow_workflow",
}
_RESIDENT_VOLUMES = {"A1": 50.0, "B2": 75.0}


def build_topology() -> Topology:
    """The daemon fixture's shaker, transporter and pads, plus a shelf for a plate that is already there.

    The transporter does not reach the shelf: no orca-client serves the daemon's own runtime, and a location a
    transporter reaches is mirrored into that transporter's world when an operator registers a plate there.
    """
    shaker = create_test_device("shaker1")
    transporter = create_test_transporter("robot1", ["shaker1", "pad1", "pad2"])
    return Topology(
        locations={
            "shaker1": shaker, "pad1": PlatePad("pad1"), "pad2": PlatePad("pad2"),
            "shelf": PlatePad("shelf", supports_deadlock_resolution=False),
        },
        transporters=[transporter],
        pools=[ResourcePool("shaker1", [shaker])],
    )


def build_workflow(topology: Topology) -> WorkflowTemplate:
    """Shakes a plate and leaves it on pad1, so the run ends with a plate still on the system."""
    plate = create_test_plate_template("left_plate")

    @orca.action(device=topology.pool("shaker1"), inputs=[plate])
    async def shake_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def shake_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake_action

    @orca.thread(labware=plate, start="pad1", end=("pad1", LEAVE_IN_PLACE))
    async def plate_journey(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield shake_method

    @orca.workflow(name="left_on_system")
    def left_on_system(wf: WorkflowContext) -> None:
        wf.start(plate_journey)

    return left_on_system


def build_slow_workflow(topology: Topology) -> WorkflowTemplate:
    """Holds its plate on the shaker until the test releases it, so a test can act while the check run runs."""
    plate = create_test_plate_template("slow_plate")

    @orca.action(device=topology.pool("shaker1"), inputs=[plate])
    async def hold_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)
        while not HOLD["released"]:
            await asyncio.sleep(0.05)

    @orca.method
    async def hold_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield hold_action

    @orca.thread(labware=plate, start="pad1", end=("pad1", LEAVE_IN_PLACE))
    async def plate_journey(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield hold_method

    @orca.workflow(name="slow_left_on_system")
    def slow_left_on_system(wf: WorkflowContext) -> None:
        wf.start(plate_journey)

    return slow_left_on_system


def build_failing_workflow(topology: Topology) -> WorkflowTemplate:
    """Its one action raises, so the run records an incident."""
    plate = create_test_plate_template("failing_plate")

    @orca.action(device=topology.pool("shaker1"), inputs=[plate])
    async def fail_action(ctx: ActionContext) -> None:
        raise RuntimeError("the check run's shaker failed")

    @orca.method
    async def fail_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield fail_action

    @orca.thread(labware=plate, start="pad1", end="pad1")
    async def plate_journey(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield fail_method

    @orca.workflow(name="fails_on_the_shaker")
    def fails_on_the_shaker(wf: WorkflowContext) -> None:
        wf.start(plate_journey)

    return fails_on_the_shaker


@dataclass
class _Daemon:
    client: AsyncClient
    state: DaemonState
    root: Path

    @property
    def stores(self) -> SqliteCheckRunStores:
        return SqliteCheckRunStores(check_runs_folder(self.root))

    async def real_positions(self) -> dict[str, str]:
        runtime = self.state.holder.built_runtime
        assert runtime is not None
        return dict(await self.state.activity.labware.list_active_locations())

    async def real_ops_count(self) -> int:
        return len(await self.state.activity.ops_history.search(OpsHistorySearchQuery()))


@asynccontextmanager
async def _daemon() -> AsyncIterator[_Daemon]:
    # The default package folder: the test transporter writes its teachpoints there.
    root = write_package(topology=f"{_HERE}:build_topology", workflows=_WORKFLOWS)
    app = create_app(package_root=root)
    async with started_daemon(app) as client:
        mounted = await client.post("/api/runtime/mount-package", json={})
        assert mounted.status_code == 200, mounted.text
        daemon = _Daemon(client, app.state.daemon, root)
        try:
            yield daemon
        finally:
            await client.post("/api/runtime/unload")


async def _resident_plate(daemon: _Daemon) -> str:
    """A plate already on the real record on the shelf, with volumes."""
    registered = await daemon.client.post(
        "/api/labware-instances",
        json={"template_name": "left_plate", "location": "shelf", "reason": "test"},
    )
    assert registered.status_code == 200, registered.text
    labware_id: str = registered.json()["labware"]["id"]
    volumes = await daemon.client.put(
        f"/api/labware-instances/{labware_id}/well-volumes",
        json={"well_volumes": _RESIDENT_VOLUMES, "reason": "test"},
    )
    assert volumes.status_code == 200, volumes.text
    return labware_id


async def _check_run(daemon: _Daemon) -> str:
    """Submit a check run and wait until it has ended and its runtime is gone."""
    submitted = await daemon.client.post("/api/executions", json=_CHECK_RUN)
    assert submitted.status_code == 200, submitted.text
    execution_id: str = submitted.json()["execution_id"]
    deadline = time.monotonic() + 60
    while daemon.state.holder.check_runs is not None and daemon.state.holder.check_runs.active is not None:
        assert time.monotonic() < deadline, "the check run did not end"
        await asyncio.sleep(0.05)
    record = await daemon.client.get(f"/api/executions/{execution_id}")
    assert record.status_code == 200, record.text
    assert record.json()["status"] == "completed", record.text
    return execution_id


async def _run_id_of(daemon: _Daemon, execution_id: str) -> str:
    (run_id,) = [e.run_id for e in await daemon.stores.entries() if execution_id in e.execution_ids]
    return run_id


@pytest.mark.timeout(120)
async def test_a_check_run_leaves_the_real_plates_and_volumes_unchanged() -> None:
    async with _daemon() as daemon:
        resident = await _resident_plate(daemon)
        positions_before = await daemon.real_positions()
        ops_before = await daemon.real_ops_count()

        await _check_run(daemon)

        assert await daemon.real_positions() == positions_before == {resident: "shelf"}
        assert await daemon.real_ops_count() == ops_before
        contents = await daemon.client.get(f"/api/labware-instances/{resident}/contents")
        assert {k: v for k, v in contents.json()["volumes"].items() if k in _RESIDENT_VOLUMES} == _RESIDENT_VOLUMES
        listed = await daemon.client.get("/api/labware-instances")
        assert [lw["current_location"] for lw in listed.json()["labware"]] == ["shelf"]


@pytest.mark.timeout(120)
async def test_a_check_runs_store_is_kept_with_the_copy_and_what_the_run_did() -> None:
    async with _daemon() as daemon:
        resident = await _resident_plate(daemon)
        ops_before = await daemon.real_ops_count()

        execution_id = await _check_run(daemon)

        store = await daemon.stores.open(await _run_id_of(daemon, execution_id))
        assert store is not None
        try:
            positions = dict(await store.labware.list_active_locations())
            assert positions[resident] == "shelf"
            assert sorted(positions.values()) == ["pad1", "shelf"]
            assert len(await store.ops_history.search(OpsHistorySearchQuery())) > ops_before
            assert await store.ops_history.search(OpsHistorySearchQuery(execution_id=execution_id))
        finally:
            await store.aclose()


@pytest.mark.timeout(120)
async def test_a_plate_added_after_a_check_run_is_not_in_its_store() -> None:
    async with _daemon() as daemon:
        execution_id = await _check_run(daemon)
        later = await _resident_plate(daemon)

        store = await daemon.stores.open(await _run_id_of(daemon, execution_id))
        assert store is not None
        try:
            assert await store.labware.get_by_id(later) is None
            assert later in await daemon.real_positions()
        finally:
            await store.aclose()


@pytest.mark.timeout(180)
async def test_back_to_back_check_runs_each_start_from_the_real_record() -> None:
    async with _daemon() as daemon:
        first = await _check_run(daemon)
        second = await _check_run(daemon)

        stores = [await daemon.stores.open(await _run_id_of(daemon, e)) for e in (first, second)]
        try:
            first_plates, second_plates = [
                set(dict(await s.labware.list_active_locations())) for s in stores if s is not None
            ]
            assert len(first_plates) == len(second_plates) == 1
            assert first_plates.isdisjoint(second_plates)
            assert await daemon.real_positions() == {}
        finally:
            for s in stores:
                if s is not None:
                    await s.aclose()


@pytest.mark.timeout(120)
async def test_a_check_run_is_in_the_run_list_after_it_ends() -> None:
    async with _daemon() as daemon:
        execution_id = await _check_run(daemon)

        listed = await daemon.client.get("/api/executions")
        assert [e["id"] for e in listed.json()["executions"]] == [execution_id]


@pytest.mark.timeout(120)
async def test_a_check_runs_events_are_stored_and_read_by_its_execution() -> None:
    """Its log goes in the normal run list, so `GET /api/events` reads it as Orca AI's does."""
    async with _daemon() as daemon:
        execution_id = await _check_run(daemon)

        async def stored() -> list[dict[str, JsonValue]]:
            body: list[dict[str, JsonValue]] = (await daemon.client.get(f"/api/events?execution_id={execution_id}")).json()
            return body

        async def its_end_is_stored() -> bool:
            return f"EXECUTION.{execution_id}.COMPLETED" in [e["event_name"] for e in await stored()]

        await _until(its_end_is_stored, "the check run's events to be stored")
        events = await stored()

    assert all(e["execution_id"] == execution_id for e in events)
    assert len(events) > 1


@pytest.mark.timeout(120)
async def test_a_check_run_is_recorded_ended_only_after_its_events_are_saved(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _daemon() as daemon:
        gate = asyncio.Event()
        store = daemon.state.activity.events
        save = store.append

        async def save_once_the_gate_opens(event: RuntimeEvent) -> None:
            await gate.wait()
            await save(event)

        monkeypatch.setattr(store, "append", save_once_the_gate_opens)
        try:
            submitted = await daemon.client.post("/api/executions", json=_CHECK_RUN)
            assert submitted.status_code == 200, submitted.text
            execution_id: str = submitted.json()["execution_id"]
            assert daemon.state.holder.check_runs is not None
            runtime = daemon.state.holder.check_runs.active
            assert runtime is not None

            async def its_work_is_done() -> bool:
                threads = [t.status for t in runtime.get_execution_detail(execution_id).threads]
                return threads == ["COMPLETED"] or runtime.get_execution(execution_id).status.is_terminal()

            await _until(its_work_is_done, "the check run's work to finish")
            for _ in range(20):
                await asyncio.sleep(0)
            assert runtime.get_execution(execution_id).status is ExecutionState.RUNNING, "not ended while its events wait"
        finally:
            gate.set()
        final = await asyncio.wait_for(runtime.wait(execution_id), timeout=30.0)

    assert final.status is ExecutionState.COMPLETED


@pytest.mark.timeout(240)
async def test_only_the_newest_check_runs_are_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("orca.runtime.runtime_holder.CHECK_RUNS_KEPT", 2)
    async with _daemon() as daemon:
        runs = [await _check_run(daemon) for _ in range(3)]

        kept = [e.execution_ids for e in await daemon.stores.entries()]
        assert kept == [(run,) for run in runs[1:]]
        listed = await daemon.client.get("/api/executions")
        assert sorted(e["id"] for e in listed.json()["executions"]) == sorted(runs[1:])


@pytest.mark.timeout(120)
async def test_no_live_or_sim_run_starts_while_a_check_run_runs() -> None:
    HOLD["released"] = False
    async with _daemon() as daemon:
        submitted = await daemon.client.post(
            "/api/executions", json={"workflow_name": "slow_left_on_system", "run_mode": "CHECK_RUN"},
        )
        assert submitted.status_code == 200, submitted.text
        running = submitted.json()["execution_id"]

        live = await daemon.client.post("/api/executions", json={**_CHECK_RUN, "run_mode": "LIVE"})
        HOLD["released"] = True

        assert live.status_code == 409, live.text
        assert live.json()["detail"]["code"] == "other_run_mode_active", live.text
        assert live.json()["detail"]["extras"]["blocking_execution_id"] == running


_MARKER = "every_earlier_frame_has_been_sent"


def _marker() -> RuntimeEvent:
    """An event sent after the check run: the feed sends in order, so once it arrives every earlier frame has."""
    return RuntimeEvent(
        event_name=_MARKER, execution_id="marker", timestamp=0.0, entity_type="ENTITY", entity_id="marker",
        status="RUNNING", context=WorkflowExecutionContext(execution_id="marker", workflow_name="marker"),
    )


async def _frames_until_the_marker(live: RecordingSubscriber) -> list[dict[str, JsonValue]]:
    frames: list[dict[str, JsonValue]] = []
    while not frames or frames[-1]["event_name"] != _MARKER:
        frames.append(json.loads(await asyncio.wait_for(live.frames.get(), timeout=60)))
    return frames[:-1]


async def _until(predicate_ok: Callable[[], Awaitable[bool]], what: str) -> None:
    deadline = time.monotonic() + 60
    while not await predicate_ok():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.05)


@pytest.mark.timeout(120)
async def test_a_check_runs_incident_is_read_by_its_execution_stays_in_its_store_and_stop_reaches_it() -> None:
    async with _daemon() as daemon:
        submitted = await daemon.client.post(
            "/api/executions", json={"workflow_name": "fails_on_the_shaker", "run_mode": "CHECK_RUN"},
        )
        assert submitted.status_code == 200, submitted.text
        execution_id = submitted.json()["execution_id"]

        async def its_incident_is_listed() -> bool:
            listed = await daemon.client.get("/api/incidents", params={"execution_id": execution_id})
            return len(listed.json()) > 0

        await _until(its_incident_is_listed, "the check run's incident")
        assert (await daemon.client.get("/api/incidents")).json() == []
        assert await daemon.state.activity.incidents.list() == []

        for body in ({}, {"confirm": True}):
            stopped = await daemon.client.post(f"/api/executions/{execution_id}/stop", json=body)
            assert stopped.status_code == 200, stopped.text

        async def the_check_run_has_ended() -> bool:
            return daemon.state.holder.check_runs is not None and daemon.state.holder.check_runs.active is None

        await _until(the_check_run_has_ended, "the stopped check run to end")
        assert await daemon.state.activity.incidents.list() == []
        listed = await daemon.client.get("/api/incidents")
        assert listed.json() == []
        store = await daemon.stores.open(await _run_id_of(daemon, execution_id))
        assert store is not None
        try:
            assert [i.execution_id for i in await store.incidents.list()] == [execution_id]
        finally:
            await store.aclose()


@pytest.mark.timeout(120)
async def test_a_finished_check_runs_plates_and_volumes_are_read_by_its_execution() -> None:
    async with _daemon() as daemon:
        resident = await _resident_plate(daemon)
        execution_id = await _check_run(daemon)

        read = await daemon.client.get(f"/api/executions/{execution_id}/check-run-labware")

        assert read.status_code == 200, read.text
        by_location = {lw["location"]: lw for lw in read.json()["labware"]}
        assert set(by_location) == {"pad1", "shelf"}
        assert by_location["shelf"]["id"] == resident
        assert {k: by_location["shelf"]["well_volumes"][k] for k in _RESIDENT_VOLUMES} == _RESIDENT_VOLUMES
        assert by_location["pad1"]["template_name"] == "left_plate"


async def test_a_check_run_that_is_not_kept_is_not_found() -> None:
    async with _daemon() as daemon:
        read = await daemon.client.get("/api/executions/no-such-run/check-run-labware")

        assert read.status_code == 404, read.text


@pytest.mark.timeout(120)
async def test_a_check_run_submitted_while_one_runs_joins_its_runtime_and_store() -> None:
    async with _daemon() as daemon:
        simple = {"workflow_name": "simple_workflow", "run_mode": "CHECK_RUN"}
        first = await daemon.client.post("/api/executions", json=simple)
        second = await daemon.client.post("/api/executions", json=simple)
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        executions = (first.json()["execution_id"], second.json()["execution_id"])

        async def the_check_run_has_ended() -> bool:
            return daemon.state.holder.check_runs is not None and daemon.state.holder.check_runs.active is None

        await _until(the_check_run_has_ended, "both check runs to end")
        assert [e.execution_ids for e in await daemon.stores.entries()] == [executions]
        assert await daemon.real_positions() == {}


@pytest.mark.timeout(120)
async def test_a_plate_the_check_run_disposed_is_read_where_the_run_left_it() -> None:
    async with _daemon() as daemon:
        submitted = await daemon.client.post(
            "/api/executions", json={"workflow_name": "simple_workflow", "run_mode": "CHECK_RUN"},
        )
        execution_id = submitted.json()["execution_id"]

        async def the_check_run_has_ended() -> bool:
            return daemon.state.holder.check_runs is not None and daemon.state.holder.check_runs.active is None

        await _until(the_check_run_has_ended, "the check run to end")
        read = await daemon.client.get(f"/api/executions/{execution_id}/check-run-labware")

        assert [(lw["template_name"], lw["location"]) for lw in read.json()["labware"]] == [("plate_96", "pad1")]
        assert await daemon.real_positions() == {}
        store = await daemon.stores.open(await _run_id_of(daemon, execution_id))
        assert store is not None
        try:
            assert await store.labware.list_active_locations() == [], "the store says the plate left"
        finally:
            await store.aclose()


@pytest.mark.timeout(120)
async def test_a_check_run_never_registers_on_the_real_device_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _daemon() as daemon:
        controller = daemon.state.connections.gateway.controller
        registered: list[str] = []
        monkeypatch.setattr(controller, "set_fault_listener", lambda listener: registered.append("set"))
        monkeypatch.setattr(controller, "clear_fault_listener", lambda listener: registered.append("clear"))

        await _check_run(daemon)

        assert registered == []


@pytest.mark.timeout(120)
async def test_a_plate_an_operator_registers_during_a_check_run_goes_to_the_real_record() -> None:
    HOLD["released"] = False
    async with _daemon() as daemon:
        submitted = await daemon.client.post(
            "/api/executions", json={"workflow_name": "slow_left_on_system", "run_mode": "CHECK_RUN"},
        )
        assert submitted.status_code == 200, submitted.text
        execution_id = submitted.json()["execution_id"]
        assert daemon.state.holder.check_runs is not None and daemon.state.holder.check_runs.active is not None

        registered = await _resident_plate(daemon)
        HOLD["released"] = True

        async def the_check_run_has_ended() -> bool:
            return daemon.state.holder.check_runs is not None and daemon.state.holder.check_runs.active is None

        await _until(the_check_run_has_ended, "the check run to end")
        assert await daemon.real_positions() == {registered: "shelf"}
        read = await daemon.client.get(f"/api/executions/{execution_id}/check-run-labware")
        assert registered not in [lw["id"] for lw in read.json()["labware"]]


@pytest.mark.timeout(240)
async def test_pruning_deletes_only_check_run_records(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("orca.runtime.runtime_holder.CHECK_RUNS_KEPT", 1)
    async with _daemon() as daemon:
        records = daemon.state.activity.execution_records
        records.record_running("real-run", "left_on_system", datetime.now(timezone.utc))

        first = await _check_run(daemon)
        second = await _check_run(daemon)

        kept = {r.id for r in await records.list_records()}
        assert kept == {"real-run", second}
        assert first not in kept


@pytest.mark.timeout(120)
async def test_the_run_list_marks_a_check_run() -> None:
    async with _daemon() as daemon:
        execution_id = await _check_run(daemon)

        listed = await daemon.client.get("/api/executions")
        got = await daemon.client.get(f"/api/executions/{execution_id}")

        assert [(e["id"], e["check_run"]) for e in listed.json()["executions"]] == [(execution_id, True)]
        assert got.json()["check_run"] is True


@pytest.mark.timeout(120)
async def test_a_check_run_whose_executions_have_finished_does_not_hold_up_an_unload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run reads completed before the check run's runtime is wound down; unload must not refuse in between."""
    winding_down = asyncio.Event()
    real_wait = SystemRuntime.wait

    async def wait_then_hold(self: SystemRuntime, execution_id: str) -> ExecutionRecord:
        record = await real_wait(self, execution_id)
        await winding_down.wait()
        return record

    monkeypatch.setattr(SystemRuntime, "wait", wait_then_hold)
    async with _daemon() as daemon:
        submitted = await daemon.client.post("/api/executions", json=_CHECK_RUN)
        assert submitted.status_code == 200, submitted.text
        execution_id = submitted.json()["execution_id"]

        async def it_reads_completed() -> bool:
            read = await daemon.client.get(f"/api/executions/{execution_id}")
            return read.json()["status"] == "completed"

        await _until(it_reads_completed, "the check run to read completed")
        try:
            assert daemon.state.holder.check_runs is not None and daemon.state.holder.check_runs.active is not None
            assert not daemon.state.holder.has_active_executions()
        finally:
            winding_down.set()


@pytest.mark.timeout(120)
async def test_an_unload_waits_for_a_check_run_that_is_still_shutting_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finished check run leaves the slot before its runtime is shut down; the unload must wait for that."""
    unloading = asyncio.Event()
    ending: list[ISystemRuntime] = []
    shut_down_when_aclose_returned: list[bool] = []
    real_end = CheckRuns._end
    real_aclose = CheckRuns.aclose

    async def end_once_unloading(running: _Running) -> None:
        ending.append(running.runtime)
        await unloading.wait()
        await real_end(running)

    async def recorded_aclose(self: CheckRuns) -> None:
        unloading.set()
        await real_aclose(self)
        shut_down_when_aclose_returned.append(ending[0].state is not RuntimeState.RUNNING)

    monkeypatch.setattr(CheckRuns, "_end", staticmethod(end_once_unloading))
    monkeypatch.setattr(CheckRuns, "aclose", recorded_aclose)
    async with _daemon() as daemon:
        submitted = await daemon.client.post("/api/executions", json=_CHECK_RUN)
        assert submitted.status_code == 200, submitted.text

        async def it_is_shutting_down() -> bool:
            return bool(ending)

        await _until(it_is_shutting_down, "the check run to start shutting down")
        unloaded = await daemon.client.post("/api/runtime/unload")

        assert unloaded.status_code == 200, unloaded.text
        assert shut_down_when_aclose_returned == [True]


@pytest.mark.timeout(120)
async def test_a_check_run_sends_none_of_its_commands_or_labware_writes_to_the_daemons_sinks() -> None:
    """A check run's commands and record writes stay in the check run."""
    async with _daemon() as daemon:
        await _resident_plate(daemon)
        stored = CollectorSink()
        daemon.state.events.add_sink(stored)
        live = RecordingSubscriber()
        daemon.state.event_sink.subscribe(live, None)

        async with sending(daemon.state.event_sink, live):
            execution_id = await _check_run(daemon)
            daemon.state.events.emit(_marker())
            frames = await _frames_until_the_marker(live)

        assert any(f["execution_id"] == execution_id for f in frames), "the check run sent nothing, so the test proves nothing"
        assert [f["event_name"] for f in frames if f["entity_type"] in {"COMMAND", "LABWARE"}] == []
        assert [e.event_name for e in stored.events if e.entity_type in {"COMMAND", "LABWARE"}] == []


async def _ended_check_run_submission(daemon: _Daemon) -> tuple[str, str]:
    """Submit a check run and wait until its runtime is gone; its submission id and execution id."""
    submitted = await daemon.client.post("/api/executions", json=_CHECK_RUN)
    assert submitted.status_code == 200, submitted.text
    body = submitted.json()

    async def the_check_run_has_ended() -> bool:
        return daemon.state.holder.check_runs is not None and daemon.state.holder.check_runs.active is None

    await _until(the_check_run_has_ended, "the check run to end")
    return body["id"], body["execution_id"]


@pytest.mark.timeout(120)
async def test_a_finished_check_runs_submission_is_still_listed_and_read() -> None:
    async with _daemon() as daemon:
        submission_id, execution_id = await _ended_check_run_submission(daemon)

        everything = await daemon.client.get("/api/submissions")
        by_run = await daemon.client.get("/api/submissions", params={"execution_id": execution_id})
        one = await daemon.client.get(f"/api/submissions/{submission_id}")

        assert [(s["id"], s["execution_id"]) for s in everything.json()["submissions"]] == [(submission_id, execution_id)]
        assert by_run.status_code == 200, by_run.text
        assert [s["id"] for s in by_run.json()["submissions"]] == [submission_id]
        assert one.status_code == 200, one.text
        assert one.json()["submission"]["execution_id"] == execution_id
        assert one.json()["submission"]["status"] == "COMPLETED"


@pytest.mark.timeout(240)
async def test_a_pruned_check_runs_submission_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("orca.runtime.runtime_holder.CHECK_RUNS_KEPT", 1)
    async with _daemon() as daemon:
        pruned, _ = await _ended_check_run_submission(daemon)
        kept, _ = await _ended_check_run_submission(daemon)

        everything = await daemon.client.get("/api/submissions")
        gone = await daemon.client.get(f"/api/submissions/{pruned}")

        assert [s["id"] for s in everything.json()["submissions"]] == [kept]
        assert gone.status_code == 404, gone.text

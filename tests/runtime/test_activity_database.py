"""A deployment's plates, runs, incidents and operation record are in its state database, so a restart keeps them."""

from datetime import datetime, timezone
import sqlite3
from pathlib import Path

from orca.resource_models.labware import LabwareInstance
from orca.daemon.app import ActivityDatabase
from orca.runtime.activity_database import state_db_path
from orca.runtime.execution_record import ExecutionState
from orca.runtime.incident_store import IncidentCategory
from orca.state.ops_store import OpsHistorySearchQuery
from orca.state.records import (
    AspirateDetails, DeviceOperation, OperationRecord, TrackingRecord, TrackingSource,
)


async def test_run_records_outlive_a_restart(tmp_path: Path) -> None:
    before = ActivityDatabase(state_db_path(tmp_path))
    await before.open()
    before.execution_records.record_running("exec-1", "assay", datetime.now(timezone.utc))
    await before.execution_records.flush_pending()
    await before.aclose()

    after = ActivityDatabase(state_db_path(tmp_path))
    await after.open()
    record = await after.execution_records.get_record("exec-1")
    await after.aclose()

    assert record is not None
    assert record.status == ExecutionState.RUNNING
    assert state_db_path(tmp_path) == tmp_path / ".orca" / "state.db"


async def test_a_plate_is_where_it_was_after_a_restart(tmp_path: Path) -> None:
    plate = LabwareInstance(template_name="plate_96", labware_type="costar_96", barcode="BC-1")
    before = ActivityDatabase(state_db_path(tmp_path))
    await before.open()
    await before.labware.register(plate)
    await before.labware.update_location(plate.id, "pad_1")
    await before.aclose()

    after = ActivityDatabase(state_db_path(tmp_path))
    await after.open()
    placed = await after.labware.list_active_locations()
    found = await after.labware.get_by_barcode("BC-1")
    await after.aclose()

    assert placed == [(plate.id, "pad_1")]
    assert found is not None and found.id == plate.id


async def test_the_state_database_is_kept_out_of_git(tmp_path: Path) -> None:
    database = ActivityDatabase(state_db_path(tmp_path))
    await database.open()
    await database.aclose()

    ignore = tmp_path / ".orca" / ".gitignore"
    assert ignore.read_text(encoding="utf-8").strip() == "*"


async def test_the_operation_record_is_in_the_state_database_and_outlives_a_restart(tmp_path: Path) -> None:
    record = TrackingRecord(
        execution_id="exec-1", action_id="a1", thread_id="t1", method_id="m1",
        source=TrackingSource.OBSERVED, timestamp=1.0, operations=[],
    )
    before = ActivityDatabase(state_db_path(tmp_path))
    await before.open()
    await before.ops_history.append("exec-1", record)
    await before.aclose()

    after = ActivityDatabase(state_db_path(tmp_path))
    await after.open()
    found = await after.ops_history.search(OpsHistorySearchQuery(execution_id="exec-1"))
    await after.aclose()

    assert found == [("exec-1", record)]
    assert not (tmp_path / ".orca" / "ops").exists()


def _record_on(device: str) -> TrackingRecord:
    op = OperationRecord(
        operation=DeviceOperation.ASPIRATE, device_name=device, affected_labware=["plate1"],
        action_id="a1", thread_id="t1", timestamp=1.0,
        details=AspirateDetails(labware="plate1", positions=["A1"], volumes=[10.0]),
    )
    return TrackingRecord(
        execution_id="exec-1", action_id="a1", thread_id="t1", method_id="m1",
        source=TrackingSource.OBSERVED, timestamp=1.0, operations=[op],
    )


async def test_records_saved_before_the_device_index_are_found_by_device_after_a_restart(tmp_path: Path) -> None:
    path = state_db_path(tmp_path)
    before = ActivityDatabase(path)
    await before.open()
    await before.ops_history.append("exec-1", _record_on("lh_1"))
    await before.aclose()
    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM ops_history_device")
    conn.commit()
    conn.close()

    after = ActivityDatabase(path)
    await after.open()
    found = await after.ops_history.search(OpsHistorySearchQuery(device_name="lh_1"))
    await after.aclose()

    assert [r.action_id for _, r in found] == ["a1"]


async def test_a_run_status_refused_while_the_daemon_shuts_down_leaves_a_saved_incident(tmp_path: Path) -> None:
    path = state_db_path(tmp_path)
    before = ActivityDatabase(path)
    await before.open()
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE executions")
    conn.commit()
    conn.close()
    before.execution_records.record_running("exec-1", "assay", datetime.now(timezone.utc))
    await before.aclose()

    after = ActivityDatabase(path)
    await after.open()
    unsaved = await after.incidents.list(category=IncidentCategory.RECORD_NOT_SAVED)
    await after.aclose()

    assert [incident.execution_id for incident in unsaved] == ["exec-1"]

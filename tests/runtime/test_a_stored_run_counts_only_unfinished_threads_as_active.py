"""A stored run's detail counts a thread as active unless it has finished or is stopping."""

from datetime import datetime, timezone

import pytest

from orca.runtime.db.engine import create_memory_engine
from orca.runtime.execution_record_service import ExecutionRecordService
from orca.runtime.sqlite_execution_record_store import SqliteExecutionRecordStore
from orca.workflow_models.status_enums import LabwareThreadStatus
from tests.unsaved_records import UnsavedRecords

NOT_ACTIVE = frozenset({
    LabwareThreadStatus.COMPLETED,
    LabwareThreadStatus.STOPPED,
    LabwareThreadStatus.ABORTED,
    LabwareThreadStatus.FAILED,
    LabwareThreadStatus.STOPPING,
})


async def _active_count_for_a_run_with_one_thread(status: LabwareThreadStatus) -> int:
    service = ExecutionRecordService(SqliteExecutionRecordStore(create_memory_engine()), UnsavedRecords())
    service.record_running("e1", "wf", submitted_at=datetime.now(timezone.utc))
    service.record_thread(
        "e1", thread_id="t1", name="plate_1", template_name="tpl", status=status.name,
    )
    detail = await service.get_detail("e1")
    assert detail is not None
    return detail.active_thread_count


@pytest.mark.parametrize("status", list(LabwareThreadStatus), ids=lambda s: s.name)
async def test_a_stored_thread_is_active_unless_finished_or_stopping(
    status: LabwareThreadStatus,
) -> None:
    active = await _active_count_for_a_run_with_one_thread(status)

    assert active == (0 if status in NOT_ACTIVE else 1)


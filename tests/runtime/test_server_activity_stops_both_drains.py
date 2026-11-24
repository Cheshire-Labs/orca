"""A server's activity stops both drains, even when stopping one of them fails."""

from unittest.mock import MagicMock

import pytest

from orca.runtime.execution_record_service import ExecutionRecordService
from orca.runtime.incident_service import IncidentService
from orca.runtime.startup import ActivityStores, build_activity


async def test_the_incident_drain_stops_when_the_run_record_drain_fails_to(monkeypatch: pytest.MonkeyPatch) -> None:
    activity = build_activity(ActivityStores(
        labware=MagicMock(), incidents=MagicMock(), execution_records=MagicMock(),
        ops_history=MagicMock(), device_modes=MagicMock(),
    ))
    incidents, records = activity.incidents, activity.execution_records
    assert isinstance(incidents, IncidentService) and isinstance(records, ExecutionRecordService)

    async def fails() -> None:
        raise RuntimeError("run-record drain did not stop")

    monkeypatch.setattr(records, "stop_drain_task", fails)
    activity.start()

    with pytest.raises(RuntimeError, match="did not stop"):
        await activity.stop()

    assert not incidents.is_draining

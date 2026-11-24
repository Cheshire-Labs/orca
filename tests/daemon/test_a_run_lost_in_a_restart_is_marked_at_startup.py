"""A run the last daemon left running is marked failed when the next daemon starts, before any mount."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orca.daemon import app as daemon_app
from orca.daemon.app import create_app
from orca.daemon.app import ActivityDatabase
from orca.runtime.activity_database import state_db_path
from orca.runtime.execution_record import ExecutionState
from orca.runtime.interrupted_runs import INTERRUPTED_EVENT_STATUS
from tests.event_subscriber import RecordingSubscriber, sending


async def test_a_run_left_running_is_marked_failed_and_announced_when_the_daemon_starts(
    tmp_path: Path,
) -> None:
    before = ActivityDatabase(state_db_path(tmp_path))
    await before.open()
    before.execution_records.record_running("exec-1", "assay", datetime.now(timezone.utc))
    await before.execution_records.flush_pending()
    await before.aclose()

    app = create_app(package_root=tmp_path)
    daemon = app.state.daemon
    subscriber = RecordingSubscriber()
    daemon.event_sink.subscribe(subscriber, None)
    async with app.router.lifespan_context(app), sending(daemon.event_sink, subscriber):
        record = await daemon.activity.execution_records.get_record("exec-1")
        frame = json.loads(await subscriber.frames.get())

    assert record is not None
    assert record.status == ExecutionState.FAILED
    assert frame["execution_id"] == "exec-1"
    assert frame["status"] == INTERRUPTED_EVENT_STATUS
    assert daemon.holder.built_runtime is None


async def test_a_run_record_the_scan_cannot_read_is_logged_and_the_daemon_still_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    async def _unreadable(*_args, **_kwargs):
        raise ValueError("a run record that does not parse")

    monkeypatch.setattr(daemon_app, "mark_interrupted_executions", _unreadable)
    app = create_app(package_root=tmp_path)

    async with app.router.lifespan_context(app):
        started = True

    assert started

    logged = [r for r in caplog.records if r.exc_info is not None]
    assert any("does not parse" in str(r.exc_info[1]) for r in logged)

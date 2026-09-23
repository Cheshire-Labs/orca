"""SqliteIncidentStore: cross-instance and cross-reopen durability.

Uses a real file-backed SQLite database (not :memory:) so that opening a fresh
engine on the same file genuinely exercises persistence across a process-like
boundary. The store is the durable layer (the database is the source of truth);
``insert`` writes directly, so no queue/drain is involved here.
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from orca.runtime.db import create_sqlite_engine
from orca.runtime.db.base import utc_now
from orca.runtime.incident_store import (
    ActionContinuedContext,
    ActionFailedContext,
    AutoSpawnFailedDetail,
    DeckReconcileConflictDetail,
    IncidentCategory,
    IncidentSeverity,
    LedgerContradictionDetail,
    MoveFailedContext,
    OtherIncidentDetail,
    RecoveryAction,
    build_incident,
)
from orca.runtime.sqlite_incident_store import SqliteIncidentStore

REPO_ROOT = Path(__file__).resolve().parents[1]


async def test_incidents_persist_across_fresh_instance_and_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "incidents.db"
    engine = create_sqlite_engine(db_path)
    store = SqliteIncidentStore(engine)
    await store.create_schema()

    spawn = build_incident(
        category=IncidentCategory.AUTO_SPAWN_FAILED,
        severity=IncidentSeverity.ERROR,
        message="spawn failed",
        detail=AutoSpawnFailedDetail(
            requested_labware_name="plate_1", requesting_thread_id="t-1"
        ),
        recovery_action=RecoveryAction.MANUAL_SPAWN,
        execution_id="exec-1",
        thread_id="t-1",
    )
    action = build_incident(
        category=IncidentCategory.ACTION_FAILED,
        severity=IncidentSeverity.CRITICAL,
        message="action raised",
        detail=ActionFailedContext(
            action_command="shake",
            method_name="run_shake",
            error_type="RuntimeError",
            error_message="boom",
            device_command="start_shake",
        ),
        recovery_action=RecoveryAction.THREAD_RECOVER_RETRY_OP,
        execution_id="exec-1",
        thread_id="t-2",
    )
    other = build_incident(
        category=IncidentCategory.OTHER,
        severity=IncidentSeverity.INFO,
        message="fyi",
        detail=OtherIncidentDetail(message_extra="note"),
        recovery_action=RecoveryAction.NONE,
        execution_id="exec-2",
    )
    await store.insert(spawn)
    await store.insert(action)
    await store.insert(other)
    await engine.dispose()

    # Fresh store on a NEW engine pointing at the SAME file.
    reopen_engine = create_sqlite_engine(db_path)
    reopened = SqliteIncidentStore(reopen_engine)

    got_spawn = await reopened.get(spawn.id)
    assert got_spawn is not None
    assert got_spawn.category is IncidentCategory.AUTO_SPAWN_FAILED
    assert isinstance(got_spawn.detail, AutoSpawnFailedDetail)
    assert got_spawn.detail.requested_labware_name == "plate_1"
    assert got_spawn.detail.requesting_thread_id == "t-1"

    got_action = await reopened.get(action.id)
    assert got_action is not None
    assert isinstance(got_action.detail, ActionFailedContext)
    assert got_action.detail.action_command == "shake"
    assert got_action.detail.error_message == "boom"
    assert got_action.detail.device_command == "start_shake"
    assert got_action.recovery_action is RecoveryAction.THREAD_RECOVER_RETRY_OP
    assert got_action.severity is IncidentSeverity.CRITICAL

    all_for_exec1 = await reopened.fetch(execution_id="exec-1")
    assert {i.id for i in all_for_exec1} == {spawn.id, action.id}

    only_other = await reopened.fetch(category=IncidentCategory.OTHER)
    assert [i.id for i in only_other] == [other.id]

    # acknowledge persists across reopen.
    await reopened.mark_acked(spawn.id, utc_now())
    await reopen_engine.dispose()

    third_engine = create_sqlite_engine(db_path)
    third = SqliteIncidentStore(third_engine)
    got_third = await third.get(spawn.id)
    assert got_third is not None and got_third.acknowledged is True
    unacked = await third.fetch(unacknowledged_only=True)
    assert spawn.id not in {i.id for i in unacked}
    assert {i.id for i in unacked} == {action.id, other.id}

    acked_count = await third.mark_all_acked(None, utc_now())
    assert acked_count == 2
    assert await third.fetch(unacknowledged_only=True) == []
    await third_engine.dispose()


def test_alembic_upgrade_head_creates_incidents_table(tmp_path: Path) -> None:
    db_path = tmp_path / "migrated.db"
    env = dict(os.environ)
    env["ORCA_DB_URL"] = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='incidents'"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [("incidents",)]


async def test_move_failed_incident_reads_back_through_the_store(tmp_path: Path) -> None:
    """A MOVE_FAILED incident must survive the SQLite round-trip. Without a
    json_to_detail branch for the category, the write succeeds (asdict
    fallthrough) but every read raises 'Unknown incident category', 500-ing
    every query surface -- the exact 'queryable incident' this category adds."""
    db_path = tmp_path / "move_incidents.db"
    engine = create_sqlite_engine(db_path)
    store = SqliteIncidentStore(engine)
    await store.create_schema()

    move = build_incident(
        category=IncidentCategory.MOVE_FAILED,
        severity=IncidentSeverity.ERROR,
        message="move raised",
        detail=MoveFailedContext(
            source="lh/carrier-7-2",
            target="lh/carrier-7-0",
            transporter="lh/gripper",
            labware="plate_1",
            error_type="ValueError",
            error_message="Target location is occupied",
        ),
        recovery_action=RecoveryAction.THREAD_RECOVER_RETRY,
        execution_id="exec-1",
        thread_id="t-move",
    )
    await store.insert(move)
    await engine.dispose()

    reopened = SqliteIncidentStore(create_sqlite_engine(db_path))
    got = await reopened.get(move.id)

    assert got is not None
    assert got.category is IncidentCategory.MOVE_FAILED
    assert isinstance(got.detail, MoveFailedContext)
    assert got.detail.source == "lh/carrier-7-2"
    assert got.detail.target == "lh/carrier-7-0"
    assert got.detail.transporter == "lh/gripper"
    assert got.detail.labware == "plate_1"
    assert got.detail.error_message == "Target location is occupied"
    # fetch (the list path) also goes through json_to_detail.
    assert [i.id for i in await reopened.fetch(category=IncidentCategory.MOVE_FAILED)] == [move.id]


async def test_action_continued_incident_reads_back_through_the_store(
    tmp_path: Path,
) -> None:
    """An ACTION_CONTINUED incident must survive the SQLite round-trip. It is the
    only durable record that a stretch of a run was never re-checked against the
    device, so a category with no json_to_detail branch would 500 every read of
    exactly the incident an operator most needs after continuing."""
    db_path = tmp_path / "continued_incidents.db"
    store = SqliteIncidentStore(create_sqlite_engine(db_path))
    await store.create_schema()

    continued = build_incident(
        category=IncidentCategory.ACTION_CONTINUED,
        severity=IncidentSeverity.WARNING,
        message="operator continued past shake",
        detail=ActionContinuedContext(
            action_command="shake",
            method_name="mix_method",
            error_type="RuntimeError",
            error_message="gripper reported empty jaws",
        ),
        recovery_action=RecoveryAction.NONE,
        execution_id="exec-1",
        thread_id="t-continue",
    )
    await store.insert(continued)

    reopened = SqliteIncidentStore(create_sqlite_engine(db_path))
    got = await reopened.get(continued.id)

    assert got is not None
    assert got.category is IncidentCategory.ACTION_CONTINUED
    assert isinstance(got.detail, ActionContinuedContext)
    assert got.detail.action_command == "shake"
    assert got.detail.method_name == "mix_method"
    assert got.detail.error_message == "gripper reported empty jaws"
    assert [
        i.id for i in
        await reopened.fetch(category=IncidentCategory.ACTION_CONTINUED)
    ] == [continued.id]


async def test_ledger_contradiction_incident_reads_back_through_the_store(
    tmp_path: Path,
) -> None:
    """A LEDGER_CONTRADICTED incident must survive the SQLite round-trip.

    It is the only durable record that an operator command proved the tip
    ledger wrong, and it names the positions that proved it. A category with
    no json_to_detail branch would 500 every read of it.
    """
    db_path = tmp_path / "contradiction_incidents.db"
    store = SqliteIncidentStore(create_sqlite_engine(db_path))
    await store.create_schema()

    contradicted = build_incident(
        category=IncidentCategory.LEDGER_CONTRADICTED,
        severity=IncidentSeverity.WARNING,
        message="pick_up_tips took tips the record said were gone",
        detail=LedgerContradictionDetail(
            device_name="flex_1",
            command="pick_up_tips",
            labware_id="lw-7",
            labware_name="r5_tips",
            positions=["A2", "B2"],
            believed="the record had 8 tips on it and none at A2, B2",
        ),
        recovery_action=RecoveryAction.NONE,
    )
    await store.insert(contradicted)

    reopened = SqliteIncidentStore(create_sqlite_engine(db_path))
    got = await reopened.get(contradicted.id)

    assert got is not None
    assert got.category is IncidentCategory.LEDGER_CONTRADICTED
    assert isinstance(got.detail, LedgerContradictionDetail)
    assert got.detail.positions == ["A2", "B2"]
    assert got.detail.labware_id == "lw-7"
    assert got.detail.command == "pick_up_tips"
    assert [
        i.id for i in
        await reopened.fetch(category=IncidentCategory.LEDGER_CONTRADICTED)
    ] == [contradicted.id]


async def test_deck_conflict_incident_keeps_the_labware_that_blocked_it(
    tmp_path: Path,
) -> None:
    """A deck conflict must read back with both plates named.

    The payload model forbids extra keys, so a field the writer sets and the
    reader does not know about does not degrade to a missing name: it raises
    on every read of that incident, which is the one an operator went looking
    for.
    """
    db_path = tmp_path / "deck_conflicts.db"
    store = SqliteIncidentStore(create_sqlite_engine(db_path))
    await store.create_schema()

    conflict = build_incident(
        category=IncidentCategory.DECK_RECONCILE_CONFLICT,
        severity=IncidentSeverity.ERROR,
        message="the jaws already hold a plate",
        detail=DeckReconcileConflictDetail(
            device_name="arm",
            labware_id="lw-2",
            labware_name="plate_2",
            position_id="arm/gripper",
            reason="ledger_target_occupied",
            blocking_labware_name="plate_1",
        ),
        recovery_action=RecoveryAction.NONE,
    )
    await store.insert(conflict)

    reopened = SqliteIncidentStore(create_sqlite_engine(db_path))
    got = await reopened.get(conflict.id)

    assert got is not None
    assert isinstance(got.detail, DeckReconcileConflictDetail)
    assert got.detail.reason == "ledger_target_occupied"
    assert got.detail.position_id == "arm/gripper"
    assert got.detail.blocking_labware_name == "plate_1"
    assert [
        i.id for i in
        await reopened.fetch(category=IncidentCategory.DECK_RECONCILE_CONFLICT)
    ] == [conflict.id]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

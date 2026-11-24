"""A table made before an index was declared gets that index when the store opens it."""

from pathlib import Path

import sqlalchemy as sa

from orca.runtime.db import create_sqlite_engine
from orca.runtime.sqlite_execution_record_store import SqliteExecutionRecordStore


async def test_an_executions_table_without_its_indexes_gets_them(tmp_path: Path) -> None:
    db_path = tmp_path / "activity.db"
    old = sa.create_engine(f"sqlite:///{db_path.as_posix()}")
    with old.begin() as conn:
        conn.execute(sa.text(
            "CREATE TABLE executions (execution_id VARCHAR(36) PRIMARY KEY, workflow_name VARCHAR(255) NOT NULL, "
            "submitted_at DATETIME NOT NULL, status VARCHAR(32) NOT NULL, terminal_at DATETIME, terminal_reason TEXT)"
        ))
    old.dispose()
    engine = create_sqlite_engine(db_path)
    store = SqliteExecutionRecordStore(engine)

    await store.create_schema()
    async with engine.connect() as conn:
        names = await conn.run_sync(lambda sync: {i["name"] for i in sa.inspect(sync).get_indexes("executions")})
    await engine.dispose()

    assert names == {"ix_executions_status", "ix_executions_submitted_at", "ix_executions_workflow_name"}

"""`alembic upgrade head` builds the tables the activity models declare, and no setup table."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from orca.runtime.db import Base

REPO_ROOT = Path(__file__).resolve().parents[2]


def _migrated_to_head(tmp_path: Path) -> Path:
    db_path = tmp_path / "migrated.db"
    env = dict(os.environ)
    env["ORCA_DB_URL"] = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return db_path


def test_alembic_head_creates_the_activity_tables_and_no_setup_table(tmp_path: Path) -> None:
    conn = sqlite3.connect(_migrated_to_head(tmp_path))
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        } - {"alembic_version"}
    finally:
        conn.close()
    assert tables == set(Base.metadata.tables)


def test_alembic_head_creates_the_indexes_the_activity_models_declare(tmp_path: Path) -> None:
    conn = sqlite3.connect(_migrated_to_head(tmp_path))
    try:
        migrated = _indexes(conn)
    finally:
        conn.close()
    declared = {
        (table.name, index.name or "", tuple(column.name for column in index.columns))
        for table in Base.metadata.tables.values() for index in table.indexes
    }
    assert migrated == declared


def _indexes(conn: sqlite3.Connection) -> set[tuple[str, str, tuple[str, ...]]]:
    """Every named index, with its table and columns; SQLite's own `sqlite_autoindex_*` are left out."""
    found: set[tuple[str, str, tuple[str, ...]]] = set()
    rows = conn.execute("SELECT name, tbl_name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL")
    for name, table in rows.fetchall():
        columns = tuple(row[2] for row in conn.execute(f'PRAGMA index_info("{name}")'))
        found.add((table, name, columns))
    return found

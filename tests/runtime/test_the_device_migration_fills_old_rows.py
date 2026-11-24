"""Upgrading to the device index fills it from each existing record's own operations."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BEFORE_DEVICE_INDEX = "b3f8d1c6e2a4"


def _alembic(db_path: Path, target: str) -> None:
    env = dict(os.environ)
    env["ORCA_DB_URL"] = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", target],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_existing_records_get_the_devices_named_in_their_operations(tmp_path: Path) -> None:
    db_path = tmp_path / "old.db"
    _alembic(db_path, BEFORE_DEVICE_INDEX)
    payloads = [
        {"operations": [{"device_name": "lh_1"}, {"device_name": "lh_1"}, {"device_name": "arm"}]},
        {"operations": []},
    ]
    conn = sqlite3.connect(db_path)
    try:
        for action_id, payload in enumerate(payloads):
            conn.execute(
                "INSERT INTO ops_history (execution_id, action_id, source, timestamp, payload)"
                " VALUES (?, ?, ?, ?, ?)",
                ("exec-1", f"a{action_id}", "observed", 1.0, json.dumps(payload)),
            )
        conn.commit()
    finally:
        conn.close()

    _alembic(db_path, "head")

    conn = sqlite3.connect(db_path)
    try:
        rows = list(conn.execute("SELECT record_id, device_name FROM ops_history_device ORDER BY device_name"))
    finally:
        conn.close()
    assert rows == [(1, "arm"), (1, "lh_1")]

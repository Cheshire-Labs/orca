"""The transition tables must iterate in a hash-seed-independent order.

Both tables are built by iterating ``frozenset``s of ``str, Enum`` statuses,
whose hash is randomized by PYTHONHASHSEED. If the build depends on that
iteration order, ``TABLE.items()`` differs per process, which breaks xdist's
identical-collection requirement for the parametrized table tests.
"""

import os
import subprocess
import sys

import pytest

_TABLES = {
    "action": "from orca.workflow_models.action_state_machine import "
    "ACTION_TRANSITION_TABLE as T",
    "thread": "from orca.workflow_models.labware_threads.thread_state_machine import "
    "THREAD_TRANSITION_TABLE as T",
}


def _table_order(import_stmt: str, seed: str) -> str:
    code = f"{import_stmt}\nprint('|'.join(f'{{k[0].name}},{{k[1].name}}' for k in T))"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONHASHSEED": seed},
    )
    return result.stdout.strip()


@pytest.mark.parametrize("import_stmt", _TABLES.values(), ids=_TABLES.keys())
def test_table_iteration_order_is_hashseed_independent(import_stmt: str) -> None:
    orders = {seed: _table_order(import_stmt, seed) for seed in ("0", "1", "2", "3")}
    assert len(set(orders.values())) == 1, (
        f"table iteration order varies with PYTHONHASHSEED: {orders}"
    )

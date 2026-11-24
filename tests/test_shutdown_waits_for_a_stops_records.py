"""A stop returns before its records are saved, and shutdown waits for them, up to a limit.

The records are the not-observed flags and the incident that says the command was stopped.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

from orca.runtime import system_runtime
from orca.runtime.incident_store import IncidentCategory
from tests.test_a_command_with_no_answer_is_settled import _operator_pick_stopped_mid_way
from tests.test_adhoc_commands_are_recorded import _Bench, running_arm_bench

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(60)]


@asynccontextmanager
async def _a_stop_whose_records_are_held(bench: _Bench) -> AsyncIterator[asyncio.Event]:
    """Stop an operator's pick mid-way, and hold its records unsaved until the yielded event is set."""
    save_them = asyncio.Event()
    system = bench.runtime.system
    note_no_answer = system.note_no_answer

    async def held(device_name: str, execution_id: str | None, named: Sequence[str], command: str) -> None:
        await save_them.wait()
        await note_no_answer(device_name, execution_id, named, command)

    with patch.object(system, "note_no_answer", held):
        await _operator_pick_stopped_mid_way(bench)
        yield save_them


async def test_shutdown_waits_for_a_stops_records_to_be_saved() -> None:
    async with running_arm_bench() as bench:
        async with _a_stop_whose_records_are_held(bench) as save_them:
            shutdown = asyncio.create_task(bench.runtime.shutdown())
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(shutdown), timeout=2.0)

            save_them.set()
            await shutdown

        incidents = await bench.runtime.incidents.list(category=IncidentCategory.RECOVERABLE_TIMEOUT)
        assert ["stopped" in incident.message for incident in incidents] == [True]


async def test_shutdown_stops_waiting_at_the_limit_and_says_what_was_not_saved(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(system_runtime, "_SHUTDOWN_DRAIN_TIMEOUT_S", 0.2)
    async with running_arm_bench() as bench:
        async with _a_stop_whose_records_are_held(bench):
            with caplog.at_level(logging.ERROR, logger=system_runtime.logger.name):
                await asyncio.wait_for(bench.runtime.shutdown(), timeout=20.0)

        not_saved = [
            record.getMessage() for record in caplog.records
            if record.levelno == logging.ERROR and "pick_at_coords on arm" in record.getMessage()
        ]
        assert len(not_saved) == 1
        assert await bench.runtime.incidents.list(category=IncidentCategory.RECOVERABLE_TIMEOUT) == []

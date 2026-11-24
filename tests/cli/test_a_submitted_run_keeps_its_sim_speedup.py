"""The record `orca run` prints for a submission says whether the run is a sim run and at what speed-up."""

import httpx
import pytest

from orca.cli.control_plane import RunModeStr
from tests.cli.fake_daemon_client import FakeDaemonClient


def _answering(run_mode: str, sim_speedup: float | None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "sub-1", "execution_id": "exec-1", "workflow_name": "wf", "group_count": 1,
            "status": "accepting", "batch_mode": "STANDALONE", "submitted_at": "2026-11-10T00:00:00Z",
            "run_mode": run_mode, "sim_speedup": sim_speedup,
        })
    return httpx.MockTransport(handler)


@pytest.mark.parametrize(("run_mode", "sim_speedup", "simulated"), [
    ("SIM", 50.0, True),
    ("SIM", None, True),
    ("CHECK_RUN", None, False),
    ("LIVE", None, False),
])
def test_the_submitted_record_carries_the_run_mode_and_speedup(
    run_mode: RunModeStr, sim_speedup: float | None, simulated: bool,
) -> None:
    client = FakeDaemonClient(_answering(run_mode, sim_speedup))

    record = client.submit_workflow("wf", run_mode=run_mode, sim_speedup=sim_speedup)

    assert (record.simulated, record.sim_speedup) == (simulated, sim_speedup)

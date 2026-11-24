"""Pool wait timeout: with `action_reservation_timeout` set and every pool device busy past it, the thread pauses; after a device frees, RETRY lets it finish.

A deployment sets the timeout in `system_config.json`; the test hands the lab's stores that setting.
"""

import asyncio

import pytest

from orca.config import OrcaConfig, ReservationConfig
from orca.workflow_models.status_enums import RecoveryDecision
from tests.sdk_features.kit import (
    Hold,
    build_world,
    close_world,
    new_lab,
    recover,
    speeds,
    submit,
    wait_for_a_pause,
    wait_for_the_end,
)
from tests.sdk_features.pool_world import PINNED, POOLED, pool_workflow
from tests.test_helpers import wait_until

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


def _short_pool_wait() -> OrcaConfig:
    return OrcaConfig(reservation=ReservationConfig(action_reservation_timeout=3.0))


async def test_a_timed_out_pool_wait_pauses_and_retry_waits_for_a_free_device(monkeypatch: pytest.MonkeyPatch) -> None:
    lab = new_lab()
    monkeypatch.setattr(lab.stores, "system_config", _short_pool_wait)
    holds = {name: Hold() for name in ("shaker_1", "shaker_2")}
    for name, hold in holds.items():
        lab.log(name).holds["shake"] = hold
    world = await build_world(lab, pool_workflow(lab, pin_1=True, pin_2=True))
    try:
        execution_id = await submit(world)
        await asyncio.wait_for(asyncio.gather(*(h.started.wait() for h in holds.values())), timeout=60)
        paused = await wait_for_a_pause(world, execution_id, template="pooled", timeout=30)
        assert paused.pause_reason == "error"

        del lab.log("shaker_2").holds["shake"]
        holds["shaker_2"].finish.set()
        await recover(world, execution_id, paused.id, RecoveryDecision.RETRY)
        await wait_until(lambda: speeds(world, "shaker_2") == [PINNED, POOLED], timeout=30)

        holds["shaker_1"].finish.set()
        ended = await wait_for_the_end(world, execution_id)
        assert set(ended.values()) == {"COMPLETED"}, ended
        assert speeds(world, "shaker_1") == [PINNED]
    finally:
        await close_world(world)

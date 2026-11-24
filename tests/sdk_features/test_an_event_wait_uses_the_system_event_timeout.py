"""`orca.on` and `orca.branch` with no timeout of their own wait for the system's `event_timeout`, then pause at `WAIT_EVENT_TIMEOUT`."""

import asyncio
from collections.abc import Callable

import pytest

import orca.orca as orca
from orca.config import CoordinationConfig, OrcaConfig
from orca.workflow_models.method_template import IMethodTemplate, MethodTemplate
from tests.sdk_features.event_world import event_workflow
from tests.sdk_features.kit import build_world, close_world, new_lab, speeds, submit, wait_for_a_pause

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


def _short_event_timeout() -> OrcaConfig:
    return OrcaConfig(coordination=CoordinationConfig(event_timeout=0.5))


def _wait_with_no_timeout(choices: dict[str, MethodTemplate]) -> IMethodTemplate:
    return orca.on("go")


def _branch_with_no_timeout(choices: dict[str, MethodTemplate]) -> IMethodTemplate:
    return orca.branch("go", {"a": [choices["a"]], "else": [choices["else"]]})


@pytest.mark.parametrize("step", [_wait_with_no_timeout, _branch_with_no_timeout], ids=["orca.on", "orca.branch"])
async def test_the_wait_pauses_after_the_system_event_timeout(
    monkeypatch: pytest.MonkeyPatch, step: Callable[[dict[str, MethodTemplate]], IMethodTemplate],
) -> None:
    lab = new_lab()
    monkeypatch.setattr(lab.stores, "system_config", _short_event_timeout)
    world = await build_world(lab, event_workflow(lab, asyncio.Event(), "x", step, name="system_timeout"))
    try:
        execution_id = await submit(world)
        paused = await wait_for_a_pause(world, execution_id, template="waiter", timeout=30)

        assert paused.pause_reason == "error"
        assert paused.pause_site == "WAIT_EVENT_TIMEOUT"
        assert "go" in (paused.pause_message or "")
        assert speeds(world, "shaker_1") == [100.0]
    finally:
        await close_world(world)

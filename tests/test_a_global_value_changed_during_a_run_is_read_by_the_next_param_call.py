"""A global value set during a run is saved to `global_variables.json` and read by the next `ctx.param("global.<name>")`
of a run that holds no value of its own."""

import asyncio
import json
from pathlib import Path

import pytest

from orca.runtime.facades.variables import VariableFacade
from orca.runtime.global_variable_service import GlobalVariableService
from orca.runtime.setup_file_stores import GlobalVariableFileStore
from orca.variables.variable_store import VariableService, VariableStore
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.device_handle import ActionRequest

pytestmark = pytest.mark.asyncio

EXECUTION = "exec-1"


def _ctx(service: VariableService) -> ActionContext:
    queue: asyncio.Queue[ActionRequest | None] = asyncio.Queue()
    return ActionContext(
        device_name="shaker_1",
        action_queue=queue,
        assigned_labware={},
        variable_store=service,
        execution_id=EXECUTION,
    )


def _facade(file: Path) -> tuple[VariableFacade, VariableService]:
    service = VariableService(VariableStore())
    service.create_execution(EXECUTION, "wf")
    return VariableFacade(service, GlobalVariableService(GlobalVariableFileStore(file))), service


async def test_the_next_read_sees_the_changed_global_and_the_file_keeps_it(tmp_path: Path) -> None:
    file = tmp_path / "global_variables.json"
    facade, service = _facade(file)
    await facade.set_global("speed", 420, confirm=True)
    ctx = _ctx(service)
    assert await ctx.param("global.speed") == 420

    await facade.set_global("speed", 520, confirm=True)

    assert await ctx.param("global.speed") == 520
    assert json.loads(file.read_text(encoding="utf-8"))["speed"]["value"] == 520


async def test_a_run_with_its_own_value_keeps_reading_it_after_the_global_changes(tmp_path: Path) -> None:
    facade, service = _facade(tmp_path / "global_variables.json")
    await facade.set_global("speed", 420, confirm=True)
    service.set("global.speed", 300, EXECUTION)
    ctx = _ctx(service)

    await facade.set_global("speed", 520, confirm=True)

    assert await ctx.param("global.speed") == 300

"""A global declared in `global_variables.json` gives a run its default, and a value outside its bounds is refused."""

import json
from pathlib import Path

import pytest

from orca.variables.errors import VariableValidationError
from orca.runtime.global_variable_service import GlobalVariableService
from orca.runtime.setup_file_stores import GlobalVariableFileStore
from tests.sdk_features.kit import build_world, close_world, new_lab, run_to_end, speeds
from tests.sdk_features.variable_world import Reads, variable_workflow

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


async def test_the_run_reads_the_declared_default_and_an_out_of_bounds_value_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    file = tmp_path / "global_variables.json"
    file.write_text(json.dumps({"speed": {"type": "int", "default": 333, "min": 100, "max": 600}}), encoding="utf-8")
    lab = new_lab()
    monkeypatch.setattr(lab.stores, "global_variables", lambda: GlobalVariableService(GlobalVariableFileStore(file)))
    reads = Reads("global.speed")
    reads.before_first_read.set()
    reads.between_actions.set()
    world = await build_world(lab, variable_workflow(lab, reads, {}))
    try:
        with pytest.raises(VariableValidationError, match="above maximum"):
            await world.runtime.variables.set_global("speed", 900, confirm=True)

        _, ended = await run_to_end(world)
        assert set(ended.values()) == {"COMPLETED"}, ended
        assert speeds(world, "shaker_1") == [333.0, 333.0]
        assert json.loads(file.read_text(encoding="utf-8")) == {"speed": {"type": "int", "default": 333, "min": 100, "max": 600}}
    finally:
        await close_world(world)

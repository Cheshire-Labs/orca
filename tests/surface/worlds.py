"""The daemons a test module drives: each booted on first use and closed with the module."""

from collections.abc import Callable
from pathlib import Path

import pytest
from _pytest.mark.structures import ParameterSet

from tests.surface.context import FINISHED_RUN_LIMIT_S, PAUSED_RUN_LIMIT_S, Context
from tests.surface.workflow_sources import MANUAL_STEP_WORKFLOW
from tests.surface.world import BOOT_LIMIT_S, REQUEST_LIMIT_S, World


def _lab(root: Path, paused_run: bool) -> Context:
    context = Context(World("pylabrobot_example", root, {"surface_manual": MANUAL_STEP_WORKFLOW}))
    context.build_finished_run()
    if paused_run:
        context.build_paused_run()
    return context


def _hamilton(root: Path, paused_run: bool) -> Context:
    return Context(World("hamilton_smc", root))


def _unstarted(root: Path, paused_run: bool) -> Context:
    """The lab before any run: the first run in it brings every device up."""
    return Context(World("pylabrobot_example", root))


_BUILDERS: dict[str, Callable[[Path, bool], Context]] = {"lab": _lab, "hamilton": _hamilton, "unstarted": _unstarted}


# Each case may make one slow request. The case that opens a world also boots it and runs its setup runs.
# These allow one slow request, not a worst case: every poll can wait as long.
CASE_LIMIT_S = REQUEST_LIMIT_S
OPENS_A_WORLD_LIMIT_S = BOOT_LIMIT_S + FINISHED_RUN_LIMIT_S + PAUSED_RUN_LIMIT_S + REQUEST_LIMIT_S


def case_params(world_of_each_case: list[str]) -> list[ParameterSet]:
    """One param per case index, each with a time limit; the first case of each world gets the longer one."""
    seen: set[str] = set()
    params: list[ParameterSet] = []
    for index, world in enumerate(world_of_each_case):
        limit = CASE_LIMIT_S if world in seen else OPENS_A_WORLD_LIMIT_S
        params.append(pytest.param(index, marks=pytest.mark.timeout(limit)))
        seen.add(world)
    return params


class Worlds:
    def __init__(self, root: Path, *, paused_run: bool) -> None:
        self._root = root
        self._paused_run = paused_run
        self._open: dict[str, Context] = {}

    def get(self, name: str) -> Context:
        if name not in self._open:
            folder = self._root / name
            folder.mkdir(exist_ok=True)
            self._open[name] = _BUILDERS[name](folder, self._paused_run)
        return self._open[name]

    def close(self) -> None:
        # Newest first: each world restores the environment the one before it set.
        for context in reversed(self._open.values()):
            context.world.close()

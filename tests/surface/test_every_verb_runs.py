"""Every `orca` verb has a case that runs it against a running daemon in simulation."""

from collections.abc import Iterator

import pytest

from tests.surface.cli_cases import CLI_CASES
from tests.surface.in_order import InOrder
from tests.surface.worlds import Worlds, case_params


@pytest.fixture(scope="module")
def worlds(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Worlds]:
    opened = Worlds(tmp_path_factory.mktemp("cli"), paused_run=True)
    yield opened
    opened.close()


@pytest.fixture(scope="module")
def in_order() -> InOrder:
    return InOrder([c.world for c in CLI_CASES])


@pytest.mark.parametrize("index", case_params([c.world for c in CLI_CASES]), ids=[c.case_id for c in CLI_CASES])
def test_the_verb_runs(index: int, worlds: Worlds, in_order: InOrder) -> None:
    in_order.start(index)
    case = CLI_CASES[index]
    context = worlds.get(case.world)
    if case.before is not None:
        case.before(context)
    argv = case.resolved(context)
    reply = context.world.cli(*argv)
    shown = f"orca {' '.join(argv)} exited {reply.exit_code}: {reply.output[:400]}"
    assert "Traceback" not in reply.output, shown
    assert reply.exit_code == case.exit_code, shown
    assert case.expect in reply.output, shown
    if case.after is not None:
        case.after(context, reply.output)

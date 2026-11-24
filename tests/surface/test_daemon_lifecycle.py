"""The verbs and routes that start, mount, reload, unload and stop a daemon, against a daemon in a child process."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.isolated_daemon import pick_free_port
from tests.surface.lifecycle_cases import LAST, LIFECYCLE_STEPS, PORT
from tests.surface.cli_cases import CliCase
from tests.surface.in_order import InOrder
from tests.surface.process_daemon import STEP_LIMIT_S, ProcessDaemon
from tests.surface.rest_cases import RestCase


pytestmark = pytest.mark.timeout(STEP_LIMIT_S)


@pytest.fixture(scope="module")
def daemon(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ProcessDaemon]:
    started = ProcessDaemon(tmp_path_factory.mktemp("lifecycle"), "pylabrobot_example")
    yield started
    started.kill()


@pytest.fixture(scope="module")
def in_order() -> InOrder:
    return InOrder([s.world for s in LIFECYCLE_STEPS])


def _needs_last(step: CliCase | RestCase) -> bool:
    if isinstance(step, CliCase):
        return LAST in step.argv
    return LAST in step.path or isinstance(step.body, dict) and LAST in step.body.values()


@pytest.mark.parametrize("index", range(len(LIFECYCLE_STEPS)), ids=[s.case_id for s in LIFECYCLE_STEPS])
def test_the_step_answers(index: int, daemon: ProcessDaemon, in_order: InOrder) -> None:
    in_order.start(index)
    step = LIFECYCLE_STEPS[index]
    last = daemon.last_execution_id() if _needs_last(step) else ""
    if isinstance(step, CliCase):
        # Picked as the step runs: a port picked earlier can be taken by another worker's daemon by now.
        port = str(pick_free_port()) if PORT in step.argv else ""
        argv = [*step.verb.split(), *(last if a == LAST else port if a == PORT else a for a in step.argv)]
        reply = daemon.cli(*argv)
        shown = f"orca {' '.join(argv)} exited {reply.exit_code}: {reply.output[:400]}"
        assert "Traceback" not in reply.output, shown
        assert reply.exit_code == step.exit_code, shown
        assert step.expect in reply.output, shown
    else:
        body = {k: last if v == LAST else v for k, v in step.body.items()} if isinstance(step.body, dict) else None
        answer = daemon.request(step.method, (step.path or step.template).replace(LAST, last), json=body)
        shown = f"{step.key} answered {answer.status}: {answer.text[:300]}"
        assert answer.status < 500, shown
        assert answer.status == step.status, shown
        assert isinstance(answer.body, dict) and step.expect in answer.body, shown

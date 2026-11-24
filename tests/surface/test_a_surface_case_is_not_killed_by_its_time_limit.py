"""A surface case fails on its own limits, never on the suite's time limit, and its HTTP client never reuses a connection.

The time limit kills the whole test worker (`os._exit`), so the rest of the module moves to a worker with no world.
A pooled connection the daemon closed after five idle seconds is reset when reused (WinError 10054).
"""

import asyncio
import importlib
import re
import threading
from collections.abc import Iterator
from pathlib import Path
from types import FunctionType

import httpx
import pytest
import uvicorn
from cheshire_source_text import CodeLine, code_lines, code_lines_of
from _pytest.mark.structures import ParameterSet
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from tests.surface.context import FINISHED_RUN_LIMIT_S, PAUSED_RUN_LIMIT_S
from tests.surface.process_daemon import CALL_LIMIT_S
from tests.surface.world import BOOT_LIMIT_S, REQUEST_LIMIT_S, a_client_that_never_reuses_a_connection
from tests.surface.worlds import CASE_LIMIT_S, OPENS_A_WORLD_LIMIT_S, case_params


def _limits(params: list[ParameterSet]) -> list[float]:
    return [float(next(m for m in p.marks if m.name == "timeout").args[0]) for p in params]


def test_the_case_that_opens_each_world_has_time_for_the_boot_both_runs_and_a_request() -> None:
    opens, again, other_world = _limits(case_params(["lab", "lab", "hamilton"]))

    assert opens >= BOOT_LIMIT_S + FINISHED_RUN_LIMIT_S + PAUSED_RUN_LIMIT_S + REQUEST_LIMIT_S
    assert other_world == opens
    assert REQUEST_LIMIT_S <= again < opens




_SURFACE = Path(__file__).resolve().parent


def _opens(constructor: str, lines: list[CodeLine]) -> bool:
    return any(re.search(rf"\b{constructor}\(", line.text) for line in lines)


def _modules_opening(constructor: str) -> list[str]:
    return sorted(path.stem for path in _SURFACE.glob("test_*.py") if _opens(constructor, code_lines_of(path)))


def _case_limits(module_name: str) -> list[float | None]:
    """The time limit of each case of the module's one test parametrized over `index`."""
    module = importlib.import_module(f"tests.surface.{module_name}")
    found: list[pytest.Mark] = []
    for name, test in vars(module).items():
        if name.startswith("test_") and isinstance(test, FunctionType):
            marks: list[pytest.Mark] = getattr(test, "pytestmark", [])
            found += [m for m in marks if m.name == "parametrize" and (m.args or [m.kwargs.get("argnames")])[0] == "index"]
    assert len(found) == 1, f"{module_name} has {len(found)} tests parametrized over its case index, not one"
    mark = found[0]
    params: list[ParameterSet] = list(mark.args[1] if len(mark.args) > 1 else mark.kwargs["argvalues"])
    return [next((float(m.args[0]) for m in p.marks if m.name == "timeout"), None) for p in params]


def test_the_scan_finds_the_modules_that_open_a_world_or_a_process_daemon() -> None:
    assert _modules_opening("Worlds") == ["test_every_route_answers", "test_every_verb_runs"]
    assert _modules_opening("ProcessDaemon") == ["test_daemon_lifecycle"]


def test_the_scan_ignores_a_name_in_a_comment_a_string_or_a_longer_name() -> None:
    assert _opens("Worlds", code_lines("opened = Worlds(root, paused_run=False)\n"))
    assert not _opens("Worlds", code_lines("# Worlds(root)\nnote = 'Worlds(root)'\nopened = OpenWorlds(root)\n"))


@pytest.mark.parametrize("module_name", _modules_opening("Worlds"))
def test_every_module_that_opens_a_world_gives_its_cases_the_case_params_limits(module_name: str) -> None:
    limits = _case_limits(module_name)

    assert None not in limits, f"{module_name} has cases with no time limit of their own: it does not use case_params"
    assert limits[0] == OPENS_A_WORLD_LIMIT_S, f"{module_name}'s first case opens a world"
    assert set(limits) <= {CASE_LIMIT_S, OPENS_A_WORLD_LIMIT_S}, limits


@pytest.mark.parametrize("module_name", _modules_opening("ProcessDaemon"))
def test_a_process_daemon_step_has_time_for_a_lookup_and_a_call(module_name: str) -> None:
    mark = importlib.import_module(f"tests.surface.{module_name}").pytestmark
    assert isinstance(mark, pytest.MarkDecorator) and mark.name == "timeout", mark
    assert float(mark.args[0]) >= 2 * CALL_LIMIT_S


async def _the_callers_port(request: Request) -> PlainTextResponse:
    assert request.client is not None
    return PlainTextResponse(str(request.client.port))


@pytest.fixture
def server_url() -> Iterator[str]:
    server = uvicorn.Server(uvicorn.Config(
        Starlette(routes=[Route("/port", _the_callers_port)]), host="127.0.0.1", port=0, log_level="warning",
    ))
    thread = threading.Thread(target=asyncio.run, args=(server.serve(),), daemon=True)
    thread.start()
    while not server.started:
        threading.Event().wait(0.05)
    port = int(server.servers[0].sockets[0].getsockname()[1])
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(10.0)


def _ports_of_two_requests(client: httpx.Client) -> tuple[str, str]:
    with client:
        return client.get("/port").text, client.get("/port").text


def test_the_world_client_opens_a_new_connection_for_every_request(server_url: str) -> None:
    first, second = _ports_of_two_requests(a_client_that_never_reuses_a_connection(server_url))

    assert first != second


def test_a_default_client_would_reuse_the_connection(server_url: str) -> None:
    first, second = _ports_of_two_requests(httpx.Client(base_url=server_url))

    assert first == second

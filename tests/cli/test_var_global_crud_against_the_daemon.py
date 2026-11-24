"""`orca var set-global`, `get-global` and `unset-global` create, read, update and delete a global on a real daemon."""
import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from typer.testing import CliRunner, Result

from orca.cli.app import app as cli
from orca.cli.client import OrcaClient
from tests.global_variables_daemon import PLATE_COUNT, a_daemon_declaring


class _TheDaemonOnTheTestLoop(httpx.BaseTransport):
    """Sends each request of the CLI's thread to the in-process daemon, which runs on the test's event loop."""

    def __init__(self, daemon: FastAPI, loop: asyncio.AbstractEventLoop) -> None:
        self._asgi = httpx.ASGITransport(app=daemon)
        self._loop = loop

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        async def send() -> httpx.Response:
            response = await self._asgi.handle_async_request(request)
            return httpx.Response(response.status_code, headers=response.headers, content=await response.aread())

        return asyncio.run_coroutine_threadsafe(send(), self._loop).result()


class _ClientOfTheTestDaemon(OrcaClient):
    def __init__(self, transport: httpx.BaseTransport) -> None:
        super().__init__("http://daemon.test")
        self._transport = transport

    def _make_http_client(self) -> httpx.Client:
        return httpx.Client(base_url="http://daemon.test", transport=self._transport)


@pytest_asyncio.fixture
async def orca(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[CliRunner]:
    async with a_daemon_declaring(PLATE_COUNT) as daemon:
        client = _ClientOfTheTestDaemon(_TheDaemonOnTheTestLoop(daemon, asyncio.get_running_loop()))
        monkeypatch.setattr("orca.cli.var.get_client", lambda: client)
        yield CliRunner()


async def _run(orca: CliRunner, *args: str) -> Result:
    return await asyncio.to_thread(orca.invoke, cli, ["--json", "--yes", "var", *args])


async def _read(orca: CliRunner, name: str) -> tuple[int | str | None, str]:
    result = await _run(orca, "get-global", name)
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    return body["value"], body["source"]


async def test_a_declared_global_is_read_set_updated_and_unset_back_to_its_default(orca: CliRunner) -> None:
    assert await _read(orca, "plate_count") == (1, "global-default")

    assert (await _run(orca, "set-global", "plate_count", "2")).exit_code == 0
    assert await _read(orca, "plate_count") == (2, "global")

    assert (await _run(orca, "set-global", "plate_count", "3")).exit_code == 0
    assert await _read(orca, "plate_count") == (3, "global")

    assert (await _run(orca, "unset-global", "plate_count")).exit_code == 0
    assert await _read(orca, "plate_count") == (1, "global-default")


async def test_a_value_outside_the_declared_bounds_fails_and_the_value_is_kept(orca: CliRunner) -> None:
    await _run(orca, "set-global", "plate_count", "2")

    assert (await _run(orca, "set-global", "plate_count", "9")).exit_code != 0

    assert await _read(orca, "plate_count") == (2, "global")


async def test_an_undeclared_global_is_created_read_updated_and_unset(orca: CliRunner) -> None:
    assert (await _run(orca, "get-global", "operator")).exit_code != 0

    await _run(orca, "set-global", "operator", "ana")
    assert await _read(orca, "operator") == ("ana", "global")

    await _run(orca, "set-global", "operator", "ben")
    assert await _read(orca, "operator") == ("ben", "global")

    assert (await _run(orca, "unset-global", "operator")).exit_code == 0
    assert (await _run(orca, "get-global", "operator")).exit_code != 0

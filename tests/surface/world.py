"""A running daemon on a real port that a sync test drives over HTTP and through the `orca` CLI.

The daemon's runtime, device controller and in-process orca-client live on a loop in a background thread, the
way `tests/daemon/conftest.py` builds them, so one daemon serves every case in a module.
"""

import asyncio
import itertools
import os
import shutil
import sys
import threading
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path

import httpx
import uvicorn
from cheshire_drivers.device_setup import BUILTIN_CLIENT
from pydantic import JsonValue
from typer.testing import CliRunner

import orca.orca as orca
from orca.cli.app import app as cli_app
from orca.daemon.app import create_app
from orca.daemon.lifecycle import DaemonInfo, delete_pid_file, pid_file_path, write_pid_file
from orca.events.event_bus import EventBus
from orca.runtime.startup import PackageSetupStoreFactory
from orca.events.runtime_event import RuntimeEvent
from orca.runtime.incident_store import IncidentCategory
from orca.runtime.intervention import INCIDENT_ENTITY
from orca.runtime.runtime_interface import ISystemRuntime
from orca.runtime.system_runtime import SystemRuntime
from tests.memory_activity import close_memory_activity, memory_daemon_system, memory_ops_store
from tests.memory_activity import memory_unsaved_positions
from tests.real_device_path import real_device_path
from tests.surface.device_locks import DeviceLockRecorder
from tests.memory_activity import memory_labware_service

EXAMPLES_ROOT = Path(__file__).resolve().parents[2] / "examples"
_COUNTER = itertools.count(1)
BOOT_LIMIT_S = 180.0
REQUEST_LIMIT_S = 300.0
_RUNNING_END = ("completed", "partial_completed", "failed", "aborted")


@dataclass(frozen=True)
class Reply:
    status: int
    body: JsonValue
    text: str


@dataclass(frozen=True)
class CliReply:
    exit_code: int
    output: str


def a_client_that_never_reuses_a_connection(base_url: str) -> httpx.Client:
    # uvicorn closes a connection idle for 5 s; a pooled one reused as it closes is reset (WinError 10054).
    return httpx.Client(base_url=base_url, timeout=REQUEST_LIMIT_S, limits=httpx.Limits(max_keepalive_connections=0))


class World:
    def __init__(self, package: str, root: Path, extra_workflows: dict[str, str] | None = None) -> None:
        self._package = package
        self._module = f"surface_{package}_{next(_COUNTER)}"
        self._folder = root / self._module
        shutil.copytree(EXAMPLES_ROOT / package, self._folder, ignore=shutil.ignore_patterns(".orca", "__pycache__"))
        for name, source in (extra_workflows or {}).items():
            (self._folder / "workflows" / f"{name}.py").write_text(source, encoding="utf-8")
        self._home = root / f"{package}-home"
        self._home.mkdir()
        self._port = 0
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runtime: SystemRuntime | None = None
        self._parked: list[asyncio.Future[str]] = []
        self._failure: BaseException | None = None
        self._saved_env = {k: os.environ.get(k) for k in ("ORCA_PACKAGE_DIR", "ORCA_DAEMON_HOME")}
        os.environ["ORCA_PACKAGE_DIR"] = str(self._folder)
        os.environ["ORCA_DAEMON_HOME"] = str(self._home)
        self._pid_file = pid_file_path()
        self._root = str(root)
        sys.path.insert(0, self._root)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(BOOT_LIMIT_S) or self._failure is not None:
            raise RuntimeError(f"the surface daemon did not start: {self._failure!r}")
        self._http = a_client_that_never_reuses_a_connection(f"http://127.0.0.1:{self._port}")

    @property
    def folder(self) -> Path:
        return self._folder

    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except BaseException as exc:
            self._failure = exc
            self._ready.set()

    async def _serve(self) -> None:
        self._stop = asyncio.Event()
        self._loop = asyncio.get_running_loop()
        async with AsyncExitStack() as stack:
            path = await stack.enter_async_context(real_device_path())
            build = await orca.build_system_from_package(
                self._module,
                PackageSetupStoreFactory(self._folder, default_client=BUILTIN_CLIENT),
                memory_ops_store(),
                path.device_factory,
                event_bus=EventBus(),
                labware_service=memory_labware_service(),
                unsaved_positions=memory_unsaved_positions(),
            )
            injected = await memory_daemon_system(build.system)
            runtime = injected.runtime
            assert isinstance(runtime, SystemRuntime)
            self._runtime = runtime
            await runtime.start()
            # Port 0: the OS picks a free port at bind time, so no other process can take it first.
            server = uvicorn.Server(uvicorn.Config(
                DeviceLockRecorder(create_app(injected=injected)), host="127.0.0.1", port=0, log_level="warning",
            ))
            serving = asyncio.create_task(server.serve())
            while not server.started:
                await asyncio.sleep(0.05)
            self._port = int(server.servers[0].sockets[0].getsockname()[1])
            write_pid_file(DaemonInfo(pid=os.getpid(), port=self._port, started_at=time.time()), self._pid_file)
            self._ready.set()
            await self._stop.wait()
            server.should_exit = True
            await serving
            for task in self._parked:
                task.cancel()
            await _shutdown(runtime)
            await close_memory_activity()

    def request(
        self, method: str, path: str, *, json: JsonValue = None, params: dict[str, str | int] | None = None,
    ) -> Reply:
        response = self._http.request(method, path, json=json, params=params)
        try:
            body: JsonValue = response.json()
        except ValueError:
            body = None
        return Reply(response.status_code, body, response.text)

    def cli(self, *args: str) -> CliReply:
        result = CliRunner().invoke(cli_app, list(args), input="")
        return CliReply(result.exit_code, result.output)

    def wait_execution(self, execution_id: str, limit_s: float = 240.0) -> str:
        end = time.time() + limit_s
        status = ""
        while time.time() < end:
            reply = self.request("GET", f"/api/executions/{execution_id}")
            if isinstance(reply.body, dict):
                status = str(reply.body.get("status"))
            if status in _RUNNING_END:
                return status
            time.sleep(0.5)
        return status

    def park_timeout(self) -> str:
        """Declare a recoverable-timeout incident on a device call that never answers; returns its id."""
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(self._park(), self._loop).result(30.0)

    async def _park(self) -> str:
        assert self._runtime is not None
        sink = _FirstTimeoutIncident()
        self._runtime.register_sink(sink)

        async def never_answers() -> str:
            await asyncio.sleep(600.0)
            return "late"

        self._parked.append(asyncio.ensure_future(self._runtime.recoverable_timeout_coordinator.run_with_timeout(
            "exec-surface", "device", "command", max_seconds=0.05, coro_factory=never_answers,
        )))
        await asyncio.wait_for(sink.parked.wait(), 15.0)
        assert sink.incident_id is not None
        return sink.incident_id

    def close(self) -> None:
        self._http.close()
        if self._stop is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(60.0)
        delete_pid_file(self._pid_file)
        sys.path.remove(self._root)
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class _FirstTimeoutIncident:
    def __init__(self) -> None:
        self.parked = asyncio.Event()
        self.incident_id: str | None = None

    def on_event(self, event: RuntimeEvent) -> None:
        if (
            self.incident_id is None and event.entity_type == INCIDENT_ENTITY
            and event.status == IncidentCategory.RECOVERABLE_TIMEOUT.value
        ):
            self.incident_id = event.entity_id
            self.parked.set()


async def _shutdown(runtime: ISystemRuntime) -> None:
    if runtime.state.name == "RUNNING":
        await runtime.shutdown(confirm=True)

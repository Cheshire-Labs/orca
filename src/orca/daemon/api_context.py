"""What the local daemon gives the REST API."""

from collections.abc import Sequence
from urllib.parse import urlsplit

from fastapi import WebSocket
from fastapi.params import Depends

from orca.api.interfaces import WebSocketRefusal
from orca.daemon.remedy_routes import DAEMON_ACTION_ROUTES, MOUNT_PACKAGE
from orca.daemon.daemon_state import DaemonState
from orca.gateway.device_command_runner import IDeviceCommandRunner
from orca.operations.wire_models import TopologySourceResponse
from orca.operations.audit import IAuditLog
from orca.operations.events import IEventLog
from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError
from orca.operations.blocker_models import BlockersPayload
from orca.operations.blocker_wire import blockers_now
from orca.operations.no_runtime import NoRuntime
from orca.runtime.check_runs import CheckRuns
from orca.runtime.remedies import Remedy, RemedyStep
from orca.runtime.deployment_registries import IDeploymentRegistries
from orca.runtime.runtime_holder import IRuntimeHolder
from orca.runtime.runtime_interface import ISystemRuntime
from orca.runtime.store_factory import IRuntimeStoreFactory


def _mount_remedy() -> Remedy:
    step = RemedyStep(
        verb="topology.mount",
        label="Mount the package",
        args={},
        action=MOUNT_PACKAGE,
        cli="orca topology mount",
    )
    return Remedy(
        id=step.verb,
        label=step.label,
        explain="Builds the system from the package folder's topology.py and workflows/ and starts it.",
        steps=(step,),
        recommended=True,
    )


_NO_RUNTIME = NoRuntime(remedy=_mount_remedy(), hint=lambda _error: "Fix the package files and mount again.")


class DaemonApiContext:
    def __init__(self, state: DaemonState) -> None:
        self._holder = state.holder
        self._runtime = state.holder.built_runtime
        self._package_root = state.package_root
        self._registries = state.registries
        self._setup_stores = state.setup
        self._audit_log = state.audit_log
        self._event_log = state.event_log
        self._device_commands = state.device_commands

    def runtime(self) -> ISystemRuntime:
        if self._runtime is None:
            raise OperationError.runtime_not_ready("no system loaded")
        return self._runtime

    def built_runtime(self) -> ISystemRuntime | None:
        return self._runtime

    def check_runs(self) -> CheckRuns | None:
        return self._holder.check_runs

    def deployment_registries(self) -> IDeploymentRegistries:
        return self._registries

    def setup_stores(self) -> IRuntimeStoreFactory:
        return self._setup_stores

    async def blockers(self) -> BlockersPayload:
        return await blockers_now(
            self._runtime, DAEMON_ACTION_ROUTES,
            without_a_runtime=lambda: _NO_RUNTIME.blocker(self._holder.last_build_error),
        )

    def is_built(self) -> bool:
        return self._runtime is not None

    def last_build_error(self) -> dict[str, str] | None:
        return _NO_RUNTIME.build_error(self._holder.last_build_error)

    async def topology_source(self) -> TopologySourceResponse:
        topology_file = self._package_root / "topology.py"
        if not topology_file.is_file():
            raise OperationError.of(ErrorCode.TOPOLOGY_NOT_FOUND, f"no topology.py in {self._package_root}")
        return TopologySourceResponse(source=topology_file.read_text(encoding="utf-8"))

    def audit_log(self) -> IAuditLog:
        return self._audit_log

    def event_log(self) -> IEventLog:
        return self._event_log

    def device_commands(self) -> IDeviceCommandRunner:
        return self._device_commands

    def runtime_holder(self) -> IRuntimeHolder:
        return self._holder

    async def package_revision(self) -> str | None:
        return None


_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class LocalhostAuth:
    """The daemon listens on 127.0.0.1 only, so a request needs no key."""

    def dependencies(self) -> Sequence[Depends]:
        return ()

    async def refuse_websocket(self, websocket: WebSocket) -> WebSocketRefusal | None:
        # A browser sends WebSockets across origins, so any open page could read the feed.
        origin = websocket.headers.get("origin")
        if origin is not None and urlsplit(origin).hostname not in _LOCAL_HOSTS:
            return WebSocketRefusal(code=1008, reason="origin not allowed")
        return None



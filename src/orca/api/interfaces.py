"""What the one REST API needs from whichever server mounts it."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from fastapi import WebSocket
from fastapi.params import Depends

from orca.gateway.device_command_runner import IDeviceCommandRunner
from orca.operations.wire_models import TopologySourceResponse
from orca.operations.binding import BoundOperation, RunnableOperation
from orca.operations.audit import IAuditLog
from orca.operations.events import IEventLog
from orca.operations.blocker_models import BlockersPayload
from orca.runtime.check_runs import CheckRuns
from orca.runtime.deployment_registries import IDeploymentRegistries
from orca.events.emitter import IEventSink
from orca.runtime.runtime_holder import IRuntimeHolder
from orca.runtime.runtime_interface import ISystemRuntime
from orca.runtime.store_factory import IRuntimeStoreFactory


class IApiContext(Protocol):
    """What one request can ask its server for."""

    def runtime(self) -> ISystemRuntime:
        """The built runtime. Raises `OperationError.runtime_not_ready` if there is none."""
        ...

    def check_runs(self) -> CheckRuns | None:
        """None until a system is built."""
        ...

    def built_runtime(self) -> ISystemRuntime | None:
        """The built runtime, or None when there is none."""
        ...

    def deployment_registries(self) -> IDeploymentRegistries:
        """The setup registries, which work with no runtime built."""
        ...

    def setup_stores(self) -> IRuntimeStoreFactory:
        """The setup file stores this server chose at startup."""
        ...

    async def blockers(self) -> BlockersPayload:
        """Everything stopping the run right now."""
        ...

    def is_built(self) -> bool: ...

    def last_build_error(self) -> dict[str, str] | None: ...

    async def topology_source(self) -> TopologySourceResponse: ...

    def audit_log(self) -> IAuditLog: ...

    def event_log(self) -> IEventLog: ...

    def device_commands(self) -> IDeviceCommandRunner:
        """The one path an operator's device command takes on this server."""
        ...

    def runtime_holder(self) -> IRuntimeHolder:
        """What builds, rebuilds and unloads this server's runtime."""
        ...

    async def package_revision(self) -> str | None:
        """The revision the package folder is at, on a server that tracks one."""
        ...


@dataclass(frozen=True)
class WebSocketRefusal:
    """The close code and reason a refused WebSocket connection gets."""

    code: int
    reason: str


class IApiAuth(Protocol):
    """How this server decides a request is allowed."""

    def dependencies(self) -> Sequence[Depends]: ...

    async def refuse_websocket(self, websocket: WebSocket) -> WebSocketRefusal | None:
        """Why this connection is refused, or None when it is allowed."""
        ...


class IEventSubscriber(Protocol):
    """Where one subscriber's frames go."""

    async def send_text(self, data: str) -> None: ...


class IEventStream(Protocol):
    """The live event feed this server serves."""

    def subscribe(self, subscriber: IEventSubscriber, execution_id: str | None) -> None:
        """Start queueing frames for `subscriber`: one execution's, or all of them."""
        ...

    def unsubscribe(self, subscriber: IEventSubscriber) -> None: ...

    async def run_subscriber(self, subscriber: IEventSubscriber) -> None:
        """Send queued frames to `subscriber` until a send fails."""
        ...


class IEventFeed(IEventStream, IEventSink, Protocol):
    """A live event feed the runtime writes to and the server streams from."""


ContextT = TypeVar("ContextT", bound=IApiContext)
ContextT_contra = TypeVar("ContextT_contra", bound=IApiContext, contravariant=True)


@dataclass(frozen=True)
class OperationEntry(Generic[ContextT_contra]):
    """One Operation this server serves, and how to build it for a request."""

    op_class: type[BoundOperation]
    build: Callable[[ContextT_contra], RunnableOperation]
    public: bool = False


class IOperationCatalog(Protocol[ContextT_contra]):
    """Which Operations this server serves."""

    def entries(self) -> Sequence[OperationEntry[ContextT_contra]]: ...

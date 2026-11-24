from orca.operations.system_loading_models import (
    MOUNT_PACKAGE_ROUTE,
    RELOAD_RUNTIME_ROUTE,
    UNLOAD_ROUTE,
)
from orca.operations.system_loading_models import (
    UnloadRequest,
)
"""Build, reload and unload the server's package folder as a running system.

Reload is served by both servers. Mount and unload are served only by the local daemon.
"""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import ClassVar, Protocol

from pydantic import BaseModel

from orca.operations.binding import RestBinding
from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError
from orca.operations.wire_models import (
    MountPackageRequest,
    MountPackageResponse,
    ReloadRequest,
    ReloadResponse,
    UnloadResponse,
)
from orca.runtime.package_import import PackageLoadError
from orca.runtime.runtime_holder import IRuntimeHolder, RuntimeBusyError
from orca.runtime.runtime_interface import ISystemRuntime

PackageRevision = Callable[[], Awaitable[str | None]]


class IReloadableRuntime(Protocol):
    """What the reload Operation asks of the runtime holder."""

    @property
    def package_dir(self) -> Path: ...

    @property
    def runtime(self) -> ISystemRuntime: ...

    @property
    def is_building(self) -> bool: ...

    def has_active_executions(self) -> bool: ...

    async def reload(self, *, reason: str | None = None, confirm: bool = False) -> None: ...


def _refuse_while_mounting(holder: IReloadableRuntime) -> None:
    if holder.is_building:
        raise OperationError.of(
            ErrorCode.CONFLICT,
            f"a mount of {holder.package_dir} is still in progress; wait for it to finish, "
            "then check `orca status`",
        )


def active_executions_refusal(message: str) -> OperationError:
    return OperationError.of(
        ErrorCode.ACTIVE_EXECUTIONS_EXIST, message,
    )


class MountPackageOperation:
    Request: ClassVar[type[BaseModel]] = MOUNT_PACKAGE_ROUTE.request
    Response: ClassVar[type[BaseModel]] = MOUNT_PACKAGE_ROUTE.response
    binding: ClassVar[RestBinding] = MOUNT_PACKAGE_ROUTE.binding

    def __init__(self, *, holder: IRuntimeHolder) -> None:
        self._holder = holder

    async def run(self, req: MountPackageRequest) -> MountPackageResponse:
        if self._holder.is_built:
            raise OperationError.of(
                ErrorCode.CONFLICT,
                f"{self._holder.package_dir} is already mounted; POST {UNLOAD_ROUTE.binding.url} first",
            )
        _refuse_while_mounting(self._holder)
        topology_file = self._holder.package_dir / "topology.py"
        if not topology_file.is_file():
            raise OperationError.of(
                ErrorCode.TOPOLOGY_NOT_FOUND,
                f"no topology.py in {self._holder.package_dir}. The daemon builds the folder "
                "it was started in, or ORCA_PACKAGE_DIR.",
            )
        try:
            await self._holder.build()
        except PackageLoadError as exc:
            raise OperationError.of(
                ErrorCode.PACKAGE_LOAD_FAILED, str(exc),
            ) from exc
        return MountPackageResponse(
            package_dir=str(self._holder.package_dir), runtime_state=self._holder.runtime.state,
        )


class UnloadOperation:
    Request: ClassVar[type[BaseModel]] = UNLOAD_ROUTE.request
    Response: ClassVar[type[BaseModel]] = UNLOAD_ROUTE.response
    binding: ClassVar[RestBinding] = UNLOAD_ROUTE.binding

    def __init__(self, *, holder: IRuntimeHolder) -> None:
        self._holder = holder

    async def run(self, req: UnloadRequest) -> UnloadResponse:
        _refuse_while_mounting(self._holder)
        if not self._holder.is_built:
            raise OperationError.of(ErrorCode.CONFLICT, "nothing to unload; no system is loaded")
        if self._holder.has_active_executions():
            raise active_executions_refusal("executions are still running; stop them before unloading")
        await self._holder.unload()
        return UnloadResponse()


class ReloadRuntimeOperation:
    """Rebuild the runtime from the package folder's files as they are on disk now."""

    Request: ClassVar[type[BaseModel]] = RELOAD_RUNTIME_ROUTE.request
    Response: ClassVar[type[BaseModel]] = RELOAD_RUNTIME_ROUTE.response
    binding: ClassVar[RestBinding] = RELOAD_RUNTIME_ROUTE.binding

    def __init__(self, *, holder: IReloadableRuntime, revision: PackageRevision) -> None:
        self._holder = holder
        self._revision = revision

    async def run(self, req: ReloadRequest) -> ReloadResponse:
        _refuse_while_mounting(self._holder)
        if self._holder.has_active_executions():
            raise active_executions_refusal("Active executions exist; stop them before reloading.")
        try:
            await self._holder.reload(reason=req.reason or None, confirm=True)
        except RuntimeBusyError as exc:
            raise active_executions_refusal(str(exc)) from exc
        except PackageLoadError as exc:
            raise OperationError.of(
                ErrorCode.REBUILD_FAILED, str(exc),
            ) from exc
        return ReloadResponse(
            head_sha=await self._revision(),
            registered=sorted(self._holder.runtime.system.get_workflow_templates().keys()),
            removed=[],
            unchanged=[],
        )


DAEMON_LOADING_OPERATIONS = (
    MountPackageOperation,
    UnloadOperation,
)

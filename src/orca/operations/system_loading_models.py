"""Mount, unload and reload wire models and routes."""

from pydantic import BaseModel, ConfigDict
from orca.operations.binding import RestBinding, Route
from orca.operations.catalog_models import EmptyRequest
from orca.operations.route_tags import RouteTag
from orca.operations.wire_models import (
    MountPackageRequest,
    HealthResponse,
    MountPackageResponse,
    ReloadRequest,
    ReloadResponse,
    ShutdownResponse,
    UnloadResponse,
)


class UnloadRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


MOUNT_PACKAGE_ROUTE = Route(
    MountPackageRequest,
    MountPackageResponse,
    RestBinding(
        method="POST", path="/runtime/mount-package", tag=RouteTag.LIFECYCLE,
        drives_device=True,
    ),
)


UNLOAD_ROUTE = Route(
    UnloadRequest,
    UnloadResponse,
    RestBinding(
        method="POST", path="/runtime/unload", tag=RouteTag.LIFECYCLE,
        drives_device=True,
    ),
)


HEALTH_ROUTE = Route(
    EmptyRequest,
    HealthResponse,
    RestBinding(method="GET", path="/health", tag=RouteTag.LIFECYCLE, under_api=False),
)


SHUTDOWN_ROUTE = Route(
    EmptyRequest,
    ShutdownResponse,
    RestBinding(method="POST", path="/shutdown", tag=RouteTag.LIFECYCLE, drives_device=True, under_api=False),
)


RELOAD_RUNTIME_ROUTE = Route(
    ReloadRequest,
    ReloadResponse,
    RestBinding(
        method="POST", path="/runtime/reload", tag=RouteTag.RUNTIME,
        carries_blockers=True,
        drives_device=True,
    ),
)

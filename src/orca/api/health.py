"""`GET /health`: the one liveness read, with the same body on every server."""

from collections.abc import Callable

from fastapi import APIRouter

from orca.operations.system_loading_models import HEALTH_ROUTE
from orca.operations.wire_models import HealthResponse
from orca.runtime.runtime_holder import IRuntimeHolder


def health_router(current_holder: Callable[[], IRuntimeHolder]) -> APIRouter:
    """Answers 200 while the server is up. It needs no auth and no built runtime; `/api/runtime/status` says why a run cannot go.

    `current_holder` raises the server's not-ready error before its startup has made the holder.
    """
    router = APIRouter()

    @router.get(
        HEALTH_ROUTE.binding.path, response_model=HEALTH_ROUTE.response, tags=[HEALTH_ROUTE.binding.tag],
    )
    async def health() -> HealthResponse:
        holder = current_holder()
        runtime = holder.built_runtime
        return HealthResponse(
            system_loaded=runtime is not None,
            runtime_state=runtime.state if runtime is not None else None,
            package_dir=str(holder.package_dir),
            mounting=holder.is_building,
        )

    return router

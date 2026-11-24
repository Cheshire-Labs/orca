"""GET /health answers from the server's runtime holder, and its not-ready error before the holder exists."""

from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from orca.api import health_router, install_error_handlers
from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError
from orca.runtime.runtime_holder import IRuntimeHolder


def _client(current_holder: Callable[[], IRuntimeHolder]) -> TestClient:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(health_router(current_holder))
    return TestClient(app)


def test_health_reads_the_holder() -> None:
    holder = MagicMock(spec=IRuntimeHolder)
    holder.built_runtime = None
    holder.is_building = True
    holder.package_dir = Path("pkg")

    body = _client(lambda: holder).get("/health").json()

    assert body == {
        "status": "ok", "system_loaded": False, "runtime_state": None, "package_dir": "pkg", "mounting": True,
    }


def test_health_before_the_holder_exists_answers_the_servers_not_ready_error() -> None:
    def not_started() -> IRuntimeHolder:
        raise OperationError.of(ErrorCode.SERVICE_NOT_READY, "the server is still starting")

    response = _client(not_started).get("/health")

    assert response.status_code == 503

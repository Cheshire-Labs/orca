"""The error envelope both servers send, from `install_error_handlers`.

These tests pin the shape and behavior of the typed channel:

  * An :class:`OperationError` raised from a handler turns
    into a typed envelope with the right status code.
  * Bare ``HTTPException(detail="...")`` reshapes to the typed
    envelope. (The CI guard in 5d blocks this in app code, but the
    runtime safety net keeps the wire shape correct if a third-party
    library raises HTTPException directly.)
  * ``RequestValidationError`` reshapes to a 422 envelope with the
    first-error message and ``MISSING_FIELD`` vs ``VALIDATION_FAILED``
    code distinction.
  * Unhandled ``RuntimeError`` reshapes to a 500 ``INTERNAL_ERROR``
    envelope without leaking the traceback.
"""

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from httpx import Response
from pydantic import BaseModel

from orca.api import ErrorEnvelope, install_error_handlers
from orca.api.errors import code_of_status
from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError


@pytest.fixture
def app_with_handlers() -> FastAPI:
    """A bare app with the handlers both servers install."""
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/raises_orca")
    async def raises_orca() -> dict[str, str]:
        raise OperationError(
            ErrorCode.WORKFLOW_NOT_FOUND, "workflow 'foo' not found", {"workflow": "foo"},
        )

    @app.get("/raises_not_found")
    async def raises_not_found() -> dict[str, str]:
        raise OperationError(ErrorCode.WORKFLOW_NOT_FOUND, "workflow 'bar' not found")

    @app.get("/raises_conflict")
    async def raises_conflict() -> dict[str, str]:
        raise OperationError(ErrorCode.ALREADY_EXISTS, "workflow 'baz' already exists")

    @app.get("/raises_validation_failed")
    async def raises_validation_failed() -> dict[str, str]:
        raise OperationError(ErrorCode.VALIDATION_ERROR, "name must be lowercase")

    @app.get("/raises_service_unavailable")
    async def raises_service_unavailable() -> dict[str, str]:
        raise OperationError.runtime_not_ready("runtime not yet built")

    @app.get("/raises_bare_http_string")
    async def raises_bare_http_string() -> dict[str, str]:
        raise HTTPException(status_code=500, detail="oops")

    @app.get("/raises_bare_http_unknown_status")
    async def raises_bare_http_unknown_status() -> dict[str, str]:
        raise HTTPException(status_code=418, detail="teapot")

    @app.get("/raises_runtime_error")
    async def raises_runtime_error() -> dict[str, str]:
        raise RuntimeError("uncaught failure with secrets in stack")

    class _Body(BaseModel):
        name: str

    @app.post("/echo")
    async def echo(body: _Body) -> dict[str, str]:
        return {"name": body.name}

    return app


def _detail(resp: Response) -> ErrorEnvelope:
    return ErrorEnvelope.model_validate(resp.json()["detail"])


# -- OperationError ---------------------------------------------------------


def test_an_operation_error_renders_the_envelope(app_with_handlers: FastAPI) -> None:
    """OperationError is the typed channel application code raises into."""
    client = TestClient(app_with_handlers, raise_server_exceptions=False)
    resp = client.get("/raises_orca")
    assert resp.status_code == 404
    detail = _detail(resp)
    assert detail.code == "workflow_not_found"
    assert detail.message == "workflow 'foo' not found"
    assert detail.extras == {"workflow": "foo"}
    # Round-trip through the model so any drift (extra/missing key)
    # shows up here in addition to the structural CI guard.


def test_not_found_error_uses_404(app_with_handlers: FastAPI) -> None:
    client = TestClient(app_with_handlers, raise_server_exceptions=False)
    resp = client.get("/raises_not_found")
    assert resp.status_code == 404
    detail = _detail(resp)
    assert detail.code == "workflow_not_found"
    assert detail.extras is None


def test_conflict_error_uses_409(app_with_handlers: FastAPI) -> None:
    client = TestClient(app_with_handlers, raise_server_exceptions=False)
    resp = client.get("/raises_conflict")
    assert resp.status_code == 409
    detail = _detail(resp)
    assert detail.code == "already_exists"


def test_validation_failed_error_uses_422(app_with_handlers: FastAPI) -> None:
    client = TestClient(app_with_handlers, raise_server_exceptions=False)
    resp = client.get("/raises_validation_failed")
    assert resp.status_code == 422
    assert _detail(resp).code == "validation_error"


def test_service_unavailable_uses_503(app_with_handlers: FastAPI) -> None:
    client = TestClient(app_with_handlers, raise_server_exceptions=False)
    resp = client.get("/raises_service_unavailable")
    assert resp.status_code == 503
    assert _detail(resp).code == "runtime_not_ready"


def test_extras_are_null_when_empty(app_with_handlers: FastAPI) -> None:
    """No extras means the wire field is null, not an empty dict.

    Pinning this avoids drift where the wire shape gains an opaque
    ``"extras": {}`` slot that consumers have to treat as meaningful.
    """
    client = TestClient(app_with_handlers, raise_server_exceptions=False)
    resp = client.get("/raises_not_found")
    assert _detail(resp).extras is None


# -- Layer 2: HTTPException reshape ----------------------------------------


def test_bare_http_string_detail_reshaped(app_with_handlers: FastAPI) -> None:
    """``raise HTTPException(detail="oops")`` from third-party code must
    reshape into the typed envelope. Status maps to INTERNAL_ERROR for 500."""
    client = TestClient(app_with_handlers, raise_server_exceptions=False)
    resp = client.get("/raises_bare_http_string")
    assert resp.status_code == 500
    detail = _detail(resp)
    assert detail.code == "internal_error"
    assert detail.message == "oops"
    assert detail.extras is None


def test_bare_http_unknown_status_falls_back_to_http_error(
    app_with_handlers: FastAPI,
) -> None:
    """Status outside the mapping table falls to ``HTTP_ERROR``."""
    client = TestClient(app_with_handlers, raise_server_exceptions=False)
    resp = client.get("/raises_bare_http_unknown_status")
    assert resp.status_code == 418
    assert _detail(resp).code == "http_error"


# -- Layer 2: RequestValidationError ---------------------------------------


def test_request_validation_missing_field(app_with_handlers: FastAPI) -> None:
    """Empty body against a model with a required ``name`` field hits
    the missing-field path and produces ``MISSING_FIELD`` code."""
    client = TestClient(app_with_handlers, raise_server_exceptions=False)
    resp = client.post("/echo", json={})
    assert resp.status_code == 422
    detail = _detail(resp)
    assert detail.code == "validation_error"
    assert "name" in detail.message
    # Full Pydantic error list lives under extras.
    assert detail.extras is not None
    errors = detail.extras["errors"]
    assert isinstance(errors, list) and len(errors) >= 1


def test_request_validation_envelope_round_trips(
    app_with_handlers: FastAPI,
) -> None:
    """The 422 envelope must validate as :class:`ErrorEnvelope` so the
    structural guard in 5d catches drift on this path too."""
    client = TestClient(app_with_handlers, raise_server_exceptions=False)
    resp = client.post("/echo", json={})
    _detail(resp)


# -- Layer 2: catch-all ----------------------------------------------------


def test_unhandled_runtime_error_reshapes_to_500_envelope(
    app_with_handlers: FastAPI,
) -> None:
    """Bare ``RuntimeError`` lands on the catch-all and gets a typed 500.

    The wire MUST NOT include the exception message (which can carry
    secrets, file paths, traceback hints) -- only the canonical
    "internal server error" string. The original message is logged
    server-side via ``logger.error(..., exc_info=exc)``.
    """
    client = TestClient(app_with_handlers, raise_server_exceptions=False)
    resp = client.get("/raises_runtime_error")
    assert resp.status_code == 500
    detail = _detail(resp)
    assert detail.code == "internal_error"
    assert detail.message == "internal server error"
    assert "secrets" not in detail.message


# -- helper: status-to-code mapping ----------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "auth_error"),
        (403, "forbidden"),
        (404, "route_not_found"),
        (405, "method_not_allowed"),
        (409, "conflict"),
        (422, "validation_error"),
        (500, "internal_error"),
        (503, "service_not_ready"),
        (418, "http_error"),
    ],
)
def test_the_code_of_a_bare_status(status: int, expected: str) -> None:
    assert code_of_status(status) == expected

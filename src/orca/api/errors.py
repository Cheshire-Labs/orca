"""The one error shape both servers send: `{"detail": {"code", "message", "extras"}}`, for every error.

`install_error_handlers` gives an app that shape for what escapes a route: an `OperationError`, a refused
confirmation, a setup file Orca cannot read, a device advertising fewer interfaces than expected, a request that does not validate, an unknown route or method,
and anything unhandled.
"""

import logging
from collections.abc import Mapping

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, JsonValue
from starlette.exceptions import HTTPException as StarletteHTTPException

from orca.operations._interface_mismatch import interface_mismatch
from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError
from orca.runtime.danger import ConfirmationRequired
from orca.runtime.errors import TopologyCollisionError
from orca.runtime.setup_files import SetupFileError

logger = logging.getLogger(__name__)

_CODE_OF_STATUS: dict[int, ErrorCode] = {
    401: ErrorCode.AUTH_ERROR,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.ROUTE_NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    409: ErrorCode.CONFLICT,
    422: ErrorCode.VALIDATION_ERROR,
    500: ErrorCode.INTERNAL_ERROR,
    503: ErrorCode.SERVICE_NOT_READY,
}


class ErrorEnvelope(BaseModel):
    """What `detail` holds in every error response."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    message: str
    extras: dict[str, JsonValue] | None = None


def code_of_status(http_status: int) -> ErrorCode:
    """The code for an error that carries only an HTTP status. The response keeps that status."""
    return _CODE_OF_STATUS.get(http_status, ErrorCode.HTTP_ERROR)


def envelope_of(exc: OperationError) -> ErrorEnvelope:
    return ErrorEnvelope(code=exc.surface_code(), message=exc.message, extras=exc.extras or None)


def error_response(
    envelope: ErrorEnvelope, http_status: int, headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=http_status, content={"detail": envelope.model_dump(mode="json")}, headers=headers,
    )


async def _operation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, OperationError)
    http_status = exc.http_status
    logger.log(
        logging.ERROR if http_status >= 500 else logging.WARNING,
        "%s %s -> %s (%d): %s", request.method, request.url.path, exc.surface_code(), http_status, exc.message,
    )
    return error_response(envelope_of(exc), http_status)


def _envelope_of_http_detail(http_status: int, detail: JsonValue) -> ErrorEnvelope:
    if isinstance(detail, dict) and isinstance(detail.get("code"), str) and "message" in detail:
        extras = detail.get("extras")
        others = {k: v for k, v in detail.items() if k not in {"code", "message", "extras"}}
        merged: dict[str, JsonValue] = {**(extras if isinstance(extras, dict) else {}), **others}
        return ErrorEnvelope(code=str(detail["code"]), message=str(detail["message"]), extras=merged or None)
    message = detail if isinstance(detail, str) else ("" if detail is None else str(detail))
    return ErrorEnvelope(code=code_of_status(http_status), message=message)


async def _device_interface_mismatch(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, TopologyCollisionError)
    return await _operation_error(request, interface_mismatch(exc))


async def _http_exception(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    envelope = _envelope_of_http_detail(exc.status_code, jsonable_encoder(exc.detail))
    logger.log(
        logging.ERROR if exc.status_code >= 500 else logging.WARNING,
        "%s %s -> %s (%d): %s", request.method, request.url.path, envelope.code, exc.status_code, envelope.message,
    )
    return error_response(envelope, exc.status_code, headers=exc.headers)


async def _request_validation(request: Request, exc: Exception) -> JSONResponse:
    """The first error names the field; every error rides in `extras.errors`."""
    assert isinstance(exc, RequestValidationError)
    errors: list[JsonValue] = jsonable_encoder(exc.errors())
    message = "request validation failed"
    first = errors[0] if errors else None
    if isinstance(first, dict):
        loc = first.get("loc")
        where = ".".join(str(part) for part in loc) if isinstance(loc, list) and loc else "<unknown>"
        message = f"{where}: {first.get('msg', 'invalid')}"
    code = ErrorCode.VALIDATION_ERROR
    logger.warning("%s %s -> %s (%d): %s", request.method, request.url.path, code, code.http_status, message)
    return error_response(ErrorEnvelope(code=code, message=message, extras={"errors": errors}), code.http_status)


async def _setup_file_error(request: Request, exc: Exception) -> JSONResponse:
    """A setup file Orca cannot read answers with its path, never a bare 500."""
    assert isinstance(exc, SetupFileError)
    logger.error("Setup file refused on %s %s: %s", request.method, request.url.path, exc)
    return error_response(
        ErrorEnvelope(
            code=ErrorCode.SETUP_FILE_INVALID, message=str(exc), extras={"path": str(exc.path), "reason": exc.reason},
        ),
        ErrorCode.SETUP_FILE_INVALID.http_status,
    )


async def _confirmation_required(request: Request, exc: Exception) -> JSONResponse:
    """The prompt a caller shows before it resends with `confirm`."""
    assert isinstance(exc, ConfirmationRequired)
    descriptor = exc.descriptor
    extras: dict[str, JsonValue] = jsonable_encoder({
        "action_name": exc.action_name,
        "danger_level": descriptor.danger_level.name,
        "prompt": descriptor.message,
        "requires_reason": descriptor.requires_reason,
        "parameters": [
            {
                "name": p.name, "type_name": p.type_name, "required": p.required,
                "default": p.default, "description": p.description,
            }
            for p in descriptor.parameters
        ],
        "call_args": exc.call_args,
    })
    code = ErrorCode.CONFIRMATION_REQUIRED
    logger.warning("%s %s -> %s (%d): %s", request.method, request.url.path, code, code.http_status, exc)
    return error_response(ErrorEnvelope(code=code, message=str(exc), extras=extras), code.http_status)


async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    """Logs the traceback; never sends it."""
    logger.error("Unhandled exception on %s %s", request.method, request.url.path, exc_info=exc)
    code = ErrorCode.INTERNAL_ERROR
    return error_response(ErrorEnvelope(code=code, message="internal server error"), code.http_status)


def install_error_handlers(app: FastAPI) -> None:
    """Give every error `app` answers the one envelope. Both servers call this."""
    app.add_exception_handler(OperationError, _operation_error)
    app.add_exception_handler(StarletteHTTPException, _http_exception)
    # Starlette answers an unknown route or method by status, before the class handler.
    app.add_exception_handler(404, _http_exception)
    app.add_exception_handler(405, _http_exception)
    app.add_exception_handler(RequestValidationError, _request_validation)
    app.add_exception_handler(SetupFileError, _setup_file_error)
    app.add_exception_handler(TopologyCollisionError, _device_interface_mismatch)
    app.add_exception_handler(ConfirmationRequired, _confirmation_required)
    app.add_exception_handler(Exception, _unhandled)

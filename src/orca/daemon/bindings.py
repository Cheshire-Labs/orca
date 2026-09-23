"""`bind_orca_rest`: wire an Operation onto a FastAPI router for the orca-core daemon.

Explicit binders, not decorator registration.

The daemon is localhost-only, so the binder
defaults to `public=True` (no auth dependency added). A hosted deployment's binders
default to `public=False` and attach a deployment-secret dependency.

The Operation itself never knows about FastAPI, paths, or HTTP. The
binder converts `OperationError` to the daemon's existing error shape
(an `HTTPException` with appropriate status code) so callers (the CLI)
see no change in wire behavior.
"""

from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any, TypeAlias

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, JsonValue

from orca.operations._protocol import (
    Operation,
    OperationError,
    OperationErrorCode,
)

OperationFactory: TypeAlias = Callable[[Request], Operation[Any, Any]]
RequestBuilder: TypeAlias = Callable[[Request], Awaitable[BaseModel]]

_OP_CODE_TO_HTTP_STATUS: dict[OperationErrorCode, int] = {
    OperationErrorCode.NOT_FOUND: status.HTTP_404_NOT_FOUND,
    OperationErrorCode.INVALID_INPUT: status.HTTP_400_BAD_REQUEST,
    OperationErrorCode.CONFLICT: status.HTTP_409_CONFLICT,
    OperationErrorCode.SERVICE_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
    OperationErrorCode.UNAUTHORIZED: status.HTTP_401_UNAUTHORIZED,
    OperationErrorCode.INTERNAL_ERROR: status.HTTP_500_INTERNAL_SERVER_ERROR,
}


def bind_orca_rest(
    router: APIRouter,
    *,
    path: str,
    method: str,
    op_factory: OperationFactory,
    request_model: type[BaseModel] | None = None,
    request_builder: RequestBuilder | None = None,
    response_model: type[BaseModel] | None = None,
    status_code: int = status.HTTP_200_OK,
    tags: list[str | Enum] | None = None,
) -> None:
    """Mount an Operation on `router` for the orca-core daemon.

    Exactly one of `request_model` / `request_builder` must be set.

    * `request_model=`: FastAPI parses the JSON body against the Pydantic
      model, publishes the schema in OpenAPI, and pre-validates with a
      standard 422 envelope on body-parse failures.
    * `request_builder=`: the binder calls the supplied async function
      with the FastAPI `Request`; use for GETs / no-body shapes.

    `tags`: OpenAPI sidebar grouping for the generated REST docs.

    The factory is called per-request so the runtime can be resolved
    against the daemon's `app.state.system_runtime` slot (which may be
    None when no system is loaded).
    """
    method_upper = method.upper()
    if method_upper not in {"GET", "POST", "PUT", "DELETE", "PATCH"}:
        raise ValueError(f"unsupported HTTP method: {method!r}")
    if (request_model is None) == (request_builder is None):
        raise ValueError(
            "bind_orca_rest requires exactly one of request_model / request_builder",
        )

    if request_model is not None:
        handler = _make_body_handler(op_factory, request_model)
    else:
        assert request_builder is not None
        handler = _make_builder_handler(op_factory, request_builder)

    # Without an explicit operation_id + summary, FastAPI derives both
    # from the handler function's __name__, which is the literal "handler"
    # defined by the _make_*_handler helpers. That gives every binding
    # operationId="handler" (then "handler_2", ... after FastAPI dedup)
    # and summary "Handler" -- the latter is what OpenAPI doc generators
    # use for page titles and sidebar labels. Derive both from the path.
    path_tail = path.rstrip("/").rsplit("/", 1)[-1]
    operation_id = path_tail.replace("-", "_")
    summary = path_tail.replace("-", " ").title()

    router.add_api_route(
        path,
        handler,
        methods=[method_upper],
        response_model=response_model,
        status_code=status_code,
        operation_id=operation_id,
        summary=summary,
        tags=tags,
    )


def _make_body_handler(
    op_factory: OperationFactory,
    request_model: type[BaseModel],
) -> Callable[..., Awaitable[Any]]:
    """Build a handler with `payload` annotated as the request_model class.

    FastAPI inspects the annotation at registration time and treats
    `payload` as a body parameter, which keeps the OpenAPI schema and
    422 envelope on body parse errors aligned with the rest of the
    daemon's routes.
    """

    # `payload: request_model` uses a runtime-captured class as the
    # annotation; pyright's `valid-type` check rejects this even though
    # FastAPI explicitly requires a real class object at the annotation
    # slot to drive body parsing + OpenAPI schema generation. The
    # closure binds `request_model` at handler-build time so Python's
    # def-time annotation eval picks up the right type. The alternatives
    # (exec-ing the function from a template, or `Annotated[BaseModel,
    # Body()]`, which loses the schema) are worse.
    async def handler(payload: request_model, request: Request) -> Any:  # type: ignore[valid-type]
        try:
            op = op_factory(request)
            return await op.run(payload)
        except OperationError as exc:
            raise _to_http(exc) from exc

    return handler


def _make_builder_handler(
    op_factory: OperationFactory,
    request_builder: RequestBuilder,
) -> Callable[..., Awaitable[Any]]:

    async def handler(request: Request) -> Any:
        try:
            op_req = await request_builder(request)
            op = op_factory(request)
            return await op.run(op_req)
        except OperationError as exc:
            raise _to_http(exc) from exc

    return handler


def _to_http(exc: OperationError) -> HTTPException:
    """Convert `OperationError` to the daemon's existing wire shape.

    The daemon uses bare `HTTPException` with string details for the
    historical generic-error path so existing CLI clients see no change.

    Operations that raise via ``OperationError.typed(...)`` set
    ``wire_code`` and ``status_code`` to pin the start-location and run-mode
    typed-envelope contracts ({code, message, extras} with the specific
    wire code + status). Either override forces the typed-envelope wire
    shape so envelope-aware callers see the full payload.
    """
    if exc.status_code is not None:
        http_status = exc.status_code
    else:
        http_status = _OP_CODE_TO_HTTP_STATUS.get(
            exc.code, status.HTTP_400_BAD_REQUEST,
        )
    wire_code = exc.wire_code if exc.wire_code is not None else exc.code.value
    detail: dict[str, JsonValue] | str
    typed = exc.wire_code is not None or exc.status_code is not None
    if exc.extras or typed:
        detail = {
            "code": wire_code,
            "message": exc.message,
            "extras": exc.extras or {},
        }
    else:
        detail = exc.message
    return HTTPException(status_code=http_status, detail=detail)

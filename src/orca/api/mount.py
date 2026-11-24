"""Mount the API on a router. Both servers serve Operations and live events through this."""

import asyncio
from collections.abc import Callable, Sequence

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, status
from fastapi.routing import APIRoute
from starlette.routing import BaseRoute
from fastapi.responses import FileResponse
from pydantic import BaseModel, JsonValue

from orca.api.interfaces import (
    ContextT,
    IApiAuth,
    IEventStream,
    IOperationCatalog,
    OperationEntry,
)
from orca.operations.binding import FileAnswer, RestBinding, check_binding, operation_id_of
from orca.operations.error_mapping import known_operation_error
from orca.operations.operator_device import claims_name_commands
from orca.api.errors import envelope_of
from orca.operations.protocol import OperationError
from orca.operations.blocker_models import with_blockers
from orca.operations.endpoint import EndpointResult, rest_endpoint

EVENT_STREAM_PATH = "/ws/events"
DRIVES_DEVICE = "x-drives-device"
LONG_POLL = "x-long-poll"


def _has_flag(route: BaseRoute, flag: str) -> bool:
    return isinstance(route, APIRoute) and bool((route.openapi_extra or {}).get(flag))


def drives_a_device(route: BaseRoute) -> bool:
    """True for a mounted Operation whose binding declares `drives_device`."""
    return _has_flag(route, DRIVES_DEVICE)


def never_timed_out(route: BaseRoute) -> bool:
    """True for a mounted Operation whose binding declares `drives_device` or `long_poll`."""
    return _has_flag(route, DRIVES_DEVICE) or _has_flag(route, LONG_POLL)


def _timeout_flags(binding: RestBinding) -> dict[str, JsonValue]:
    flags: dict[str, JsonValue] = {}
    if binding.drives_device:
        flags[DRIVES_DEVICE] = True
    if binding.long_poll:
        flags[LONG_POLL] = True
    return flags


def to_http(exc: OperationError) -> HTTPException:
    """The `{code, message, extras}` envelope every route sends."""
    detail: dict[str, JsonValue] = envelope_of(exc).model_dump(mode="json")
    return HTTPException(status_code=exc.http_status, detail=detail)


def mount_api(
    router: APIRouter,
    catalog: IOperationCatalog[ContextT],
    context_of: Callable[[Request], ContextT],
    auth: IApiAuth,
) -> None:
    """Mount every Operation in `catalog` at the route it declares. Raises on two routes one url could reach, or a bad claim."""
    entries = catalog.entries()
    refuse_two_routes_at_one_path(entries)
    claims_name_commands(tuple(entry.op_class for entry in entries))
    for entry in entries:
        _mount(router, entry, context_of, auth)


def refuse_two_routes_at_one_path(entries: Sequence[OperationEntry[ContextT]]) -> None:
    """Raise when one method and url could reach two Operations, so no route depends on the order it was mounted in."""
    seen: list[tuple[str, str, tuple[_Segment, ...], str]] = []
    for entry in entries:
        binding = check_binding(entry.op_class)
        segments = _segments(binding.path)
        for method, path, other, name in seen:
            if method == binding.method and _overlap(segments, other):
                raise ValueError(
                    f"{entry.op_class.__name__} at {binding.method} {binding.path} and {name} at {method} {path} "
                    "can both be reached by one url",
                )
        seen.append((binding.method, binding.path, segments, entry.op_class.__name__))


# A path segment: a literal, one parameter segment ("*") or a `:path` parameter's tail ("**", zero or more segments).
_Segment = str


def _segments(path: str) -> tuple[_Segment, ...]:
    out: list[_Segment] = []
    for part in path.strip("/").split("/"):
        if part.endswith(":path}"):
            out.extend(("*", "**"))
        elif part.startswith("{"):
            out.append("*")
        else:
            out.append(part)
    return tuple(out)


def _overlap(a: tuple[_Segment, ...], b: tuple[_Segment, ...]) -> bool:
    """Whether some url matches both templates."""
    if a and a[0] == "**":
        return _overlap(a[1:], b) or (bool(b) and _overlap(a, b[1:]))
    if b and b[0] == "**":
        return _overlap(a, b[1:]) or (bool(a) and _overlap(a[1:], b))
    if not a or not b:
        return not a and not b
    if a[0] != "*" and b[0] != "*" and a[0] != b[0]:
        return False
    return _overlap(a[1:], b[1:])


def mount_event_stream(
    router: APIRouter,
    stream_of: Callable[[WebSocket], IEventStream | None],
    auth: IApiAuth,
) -> None:
    """Mount the live event WebSocket. `stream_of` answers None until the server has a stream."""

    async def events_websocket(
        websocket: WebSocket,
        execution_id: str | None = Query(default=None),
    ) -> None:
        refusal = await auth.refuse_websocket(websocket)
        if refusal is not None:
            await websocket.close(code=refusal.code, reason=refusal.reason)
            return
        stream = stream_of(websocket)
        if stream is None:
            await websocket.close(
                code=status.WS_1011_INTERNAL_ERROR, reason="Event stream not initialized",
            )
            return
        stream.subscribe(websocket, execution_id=execution_id)
        try:
            await websocket.accept()
            await _send_until_the_client_leaves(stream, websocket)
        finally:
            stream.unsubscribe(websocket)

    router.add_api_websocket_route(EVENT_STREAM_PATH, events_websocket)


async def _send_until_the_client_leaves(stream: IEventStream, websocket: WebSocket) -> None:
    # A quiet feed never sends, so it would never see the client leave, and
    # uvicorn waits for this handler at shutdown.
    sending = asyncio.create_task(stream.run_subscriber(websocket))
    try:
        while (await websocket.receive())["type"] != "websocket.disconnect":
            pass
    finally:
        sending.cancel()


def _mount(
    router: APIRouter,
    entry: OperationEntry[ContextT],
    context_of: Callable[[Request], ContextT],
    auth: IApiAuth,
) -> None:
    op_class = entry.op_class
    binding = check_binding(op_class)
    response_model: type[BaseModel] | None = op_class.Response
    serves_a_file = issubclass(op_class.Response, FileAnswer)
    if binding.status_code == status.HTTP_204_NO_CONTENT or serves_a_file:
        response_model = None
    elif binding.carries_blockers:
        response_model = with_blockers(op_class.Response)

    async def run(request: Request, payload: BaseModel) -> EndpointResult:
        context = context_of(request)
        try:
            result = await entry.build(context).run(payload)
        except Exception as exc:
            known = known_operation_error(exc)
            if known is None:
                raise
            raise to_http(known) from exc
        if serves_a_file:
            answer = FileAnswer.model_validate(result.model_dump())
            return FileResponse(answer.path, media_type=answer.media_type)
        if not binding.carries_blockers:
            return result
        blockers = await context.blockers()
        return {**result.model_dump(mode="json"), **blockers.model_dump(mode="json")}

    router.add_api_route(
        binding.url,
        rest_endpoint(op_class, run),
        methods=[binding.method],
        response_model=response_model,
        status_code=binding.status_code,
        operation_id=operation_id_of(op_class),
        summary=operation_id_of(op_class).replace("_", " ").title(),
        tags=[binding.tag],
        openapi_extra=_timeout_flags(binding) or None,
        responses={200: {"content": {"application/octet-stream": {}}}} if serves_a_file else None,
        dependencies=[] if entry.public else list(auth.dependencies()),
    )

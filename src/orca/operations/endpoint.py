"""Build the FastAPI endpoint for an Operation from its `RestBinding`.

The daemon and a hosted deployment both mount Operations through this, so a
Request field lands in the path, the query or the body by one rule on every
surface.
"""

import inspect
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, TypeAlias

from fastapi import Body, Path, Query, Request
from fastapi.responses import FileResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, JsonValue, ValidationError, create_model
from pydantic.fields import FieldInfo

from orca.operations.binding import BoundOperation, check_binding

EndpointResult: TypeAlias = BaseModel | dict[str, JsonValue] | FileResponse
RunOperation: TypeAlias = Callable[[Request, BaseModel], Awaitable[EndpointResult]]


def rest_endpoint(
    op_class: type[BoundOperation], run: RunOperation,
) -> Callable[..., Awaitable[EndpointResult]]:
    """A handler whose signature FastAPI reads as the binding's path, query and body."""
    binding = check_binding(op_class)
    request_model = op_class.Request
    fields = request_model.model_fields
    params: list[inspect.Parameter] = [
        inspect.Parameter("request", inspect.Parameter.KEYWORD_ONLY, annotation=Request),
    ]
    for name in binding.path_params:
        params.append(_param(name, fields[name], Path))
    rest = {n: f for n, f in fields.items() if n not in binding.path_params}
    if binding.fields_in_query:
        for name, info in rest.items():
            _refuse_a_model_in_the_query(op_class, name, info)
            params.append(_param(name, info, Query))
    elif rest:
        body_model = _body_model(request_model, rest) if binding.path_params else request_model
        params.append(_body_param(body_model, rest))

    async def handler(**kwargs: object) -> EndpointResult:
        request = kwargs.pop("request")
        if not isinstance(request, Request):
            raise TypeError("FastAPI passes the request")
        body = kwargs.pop("body", None)
        if isinstance(body, BaseModel):
            kwargs.update(body.model_dump(exclude_unset=True))
        try:
            payload = request_model.model_validate(kwargs)
        except ValidationError as exc:
            raise RequestValidationError(exc.errors()) from exc
        return await run(request, payload)

    # FastAPI reads the parameters off the signature.
    setattr(handler, "__signature__", inspect.Signature(params))
    handler.__name__ = op_class.__name__
    return handler


def _body_param(body_model: type[BaseModel], fields: dict[str, FieldInfo]) -> inspect.Parameter:
    if any(info.is_required() for info in fields.values()):
        return inspect.Parameter(
            "body", inspect.Parameter.KEYWORD_ONLY, annotation=Annotated[body_model, Body()],
        )
    return inspect.Parameter(
        "body",
        inspect.Parameter.KEYWORD_ONLY,
        default=None,
        annotation=Annotated[body_model | None, Body()],
    )


def _param(name: str, info: FieldInfo, where: Callable[..., FieldInfo]) -> inspect.Parameter:
    default = (
        inspect.Parameter.empty if info.is_required()
        else info.get_default(call_default_factory=True)
    )
    return inspect.Parameter(
        name,
        inspect.Parameter.KEYWORD_ONLY,
        default=default,
        annotation=Annotated[info.annotation, where(description=info.description)],
    )


def _body_model(request_model: type[BaseModel], rest: dict[str, FieldInfo]) -> type[BaseModel]:
    # pydantic types field definitions as Any.
    definitions: dict[str, Any] = {name: (info.annotation, info) for name, info in rest.items()}
    return create_model(
        f"{request_model.__name__}Body", __config__=request_model.model_config, **definitions,
    )


def _refuse_a_model_in_the_query(
    op_class: type[BoundOperation], name: str, info: FieldInfo,
) -> None:
    annotation = info.annotation
    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        raise ValueError(
            f"{op_class.__name__}.{name} is a model and cannot be a query parameter; "
            "bind the Operation with POST",
        )

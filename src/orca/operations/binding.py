"""Where an Operation sits on REST, declared once as a `Route` beside its request and response models.

`orca.api.mount_api` reads it through the Operation class, so the daemon and a hosted deployment
mount an Operation at the same path, with the same method, request and response. `OrcaClient` reads
the same `Route`, so a route is typed in one place.
"""

import re
from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable, Mapping
from typing import ClassVar, Generic, Literal, Protocol, TypeVar
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict

HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]

_PATH_PARAM = re.compile(r"\{(\w+)(:path)?\}")

API_PREFIX = "/api"

_Value = TypeVar("_Value")
_Request = TypeVar("_Request", bound=BaseModel)
_Response = TypeVar("_Response", bound=BaseModel)


@dataclass(frozen=True)
class RestBinding:
    """The REST route an Operation is served at.

    Every server serves `path` under `API_PREFIX`, except a route with `under_api` False (liveness and shutdown). Fields of the Request named in the path
    are path parameters; on GET and DELETE the rest are query parameters, and
    otherwise they are the JSON body.

    `drives_device` is True when the route can send a command to a device, hold a device's
    lock, or shut the runtime down, which waits on device commands. A server must never cut
    such a request off part-way.

    `long_poll` is True when the server holds the request open by design, up to a wait the
    request names itself. It drives no device.

    No server or client times out a route that has either flag.
    """

    method: HttpMethod
    path: str
    tag: str
    carries_blockers: bool = False
    drives_device: bool = False
    long_poll: bool = False
    status_code: int = 200
    under_api: bool = True
    path_params: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "path_params", tuple(name for name, _ in _PATH_PARAM.findall(self.path)))

    @property
    def never_timed_out(self) -> bool:
        return self.drives_device or self.long_poll

    @property
    def url(self) -> str:
        """Where the route is served: `path` under `API_PREFIX`, or bare when `under_api` is False."""
        return API_PREFIX + self.path if self.under_api else self.path

    @property
    def fields_in_query(self) -> bool:
        return self.method in ("GET", "DELETE")

    def fill_url(self, fields: Mapping[str, _Value]) -> tuple[str, dict[str, _Value]]:
        """The url with each path parameter filled in, and the fields that are left.

        A `:path` parameter keeps its slashes. The others are percent-encoded whole.
        """
        left = dict(fields)

        def fill(match: re.Match[str]) -> str:
            value = left.pop(match.group(1))
            return quote(str(value), safe="/" if match.group(2) else "")

        return _PATH_PARAM.sub(fill, self.url), left


@dataclass(frozen=True)
class Route(Generic[_Request, _Response]):
    """Where an Operation is served, and what it takes and answers.

    Declared once beside the models, so the Operation and the CLI read the same copy and the CLI
    needs nothing from the engine.
    """

    request: type[_Request]
    response: type[_Response]
    binding: RestBinding


class FileAnswer(BaseModel):
    """An Operation's answer that is a file: the route sends the file's bytes with this media type."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    media_type: str


class RunnableOperation(Protocol):
    """Something a route can run: an Operation, or one wrapped to do more around it."""

    # Each Operation narrows the request type, so the protocol leaves the parameters open.
    @property
    def run(self) -> Callable[..., Awaitable[BaseModel]]: ...


RequestT_contra = TypeVar("RequestT_contra", bound=BaseModel, contravariant=True)
ResponseT_co = TypeVar("ResponseT_co", bound=BaseModel, covariant=True)


class OperationStep(Protocol[RequestT_contra, ResponseT_co]):
    """One Operation another Operation runs, typed by its own request and response."""

    @property
    def run(self) -> Callable[[RequestT_contra], Awaitable[ResponseT_co]]: ...


class BoundOperation(RunnableOperation, Protocol):
    """An Operation that declares its REST binding."""

    Request: ClassVar[type[BaseModel]]
    Response: ClassVar[type[BaseModel]]
    binding: ClassVar[RestBinding]


def check_binding(op_class: type[BoundOperation]) -> RestBinding:
    """The binding, after checking its path names only Request fields."""
    binding = op_class.binding
    fields = op_class.Request.model_fields
    missing = [name for name in binding.path_params if name not in fields]
    if missing:
        raise ValueError(
            f"{op_class.__name__}: path {binding.path!r} names {missing}, "
            f"which {op_class.Request.__name__} does not have",
        )
    return binding


def operation_id_of(op_class: type[BoundOperation]) -> str:
    """`GetSystemInfoOperation` -> `get_system_info`."""
    name = op_class.__name__.removesuffix("Operation")
    return "".join(f"_{c.lower()}" if c.isupper() else c for c in name).lstrip("_")

"""`route_of` names the path an Operation is served at, which is not under `/api` for every route."""

from typing import ClassVar

from pydantic import BaseModel

from orca.operations.binding import RestBinding
from orca.operations.blocker_wire import route_of


class _Req(BaseModel):
    name: str


class _InApi:
    Request: ClassVar[type[BaseModel]] = _Req
    Response: ClassVar[type[BaseModel]] = _Req
    binding: ClassVar[RestBinding] = RestBinding(method="POST", path="/things/{name}", tag="t")

    async def run(self, req: _Req, /) -> _Req:
        return req


class _Bare(_InApi):
    binding: ClassVar[RestBinding] = RestBinding(method="POST", path="/things/{name}", tag="t", under_api=False)


def test_a_remedy_names_a_route_under_the_api_prefix() -> None:
    assert route_of(_InApi).rest == "POST /api/things/<name>"


def test_a_remedy_names_a_route_declared_outside_the_api_prefix_at_its_bare_path() -> None:
    assert route_of(_Bare).rest == "POST /things/<name>"

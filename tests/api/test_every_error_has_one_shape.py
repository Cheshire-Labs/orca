"""Every error either server answers is `{"detail": {"code", "message", "extras"}}`, not only Operation errors."""

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from orca.api import install_error_handlers
from orca.daemon.app import create_app
from orca.runtime.danger import DangerLevel, dangerous
from tests.daemon.started_daemon import started_daemon


class _Body(BaseModel):
    name: str


@dangerous(name="test.errors.wipe", level=DangerLevel.OPERATOR, message="wipe {what}")
def _wipe(what: str) -> str:
    return what


def _app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)

    @app.post("/body")
    async def body(payload: _Body) -> str:
        return payload.name

    @app.get("/wipe")
    async def wipe() -> str:
        return _wipe("deck")

    return app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=_app(), raise_app_exceptions=False), base_url="http://t") as c:
        yield c


async def test_an_unknown_route_is_the_envelope(client: AsyncClient) -> None:
    resp = await client.get("/nowhere")

    assert resp.status_code == 404
    assert resp.json() == {"detail": {"code": "route_not_found", "message": "Not Found", "extras": None}}


async def test_a_wrong_method_is_the_envelope(client: AsyncClient) -> None:
    resp = await client.get("/body")

    assert resp.status_code == 405
    assert resp.json()["detail"]["code"] == "method_not_allowed"


async def test_a_refused_confirmation_is_the_envelope_with_its_prompt(client: AsyncClient) -> None:
    resp = await client.get("/wipe")

    detail = resp.json()["detail"]
    assert resp.status_code == 400
    assert detail["code"] == "confirmation_required"
    assert (detail["extras"]["action_name"], detail["extras"]["prompt"]) == ("test.errors.wipe", "wipe {what}")


async def test_the_daemon_answers_a_bad_body_with_the_envelope() -> None:
    async with started_daemon(create_app()) as daemon:
        resp = await daemon.post("/api/executions", json={})

    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "validation_error"

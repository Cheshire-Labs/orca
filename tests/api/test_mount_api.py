"""What every route mounted from the Operation catalog does."""

from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.params import Depends as DependsParam
from fastapi.testclient import TestClient
from httpx import Response
from pydantic import BaseModel

from orca.api import OperationEntry, WebSocketRefusal, mount_api
from orca.gateway.controller.exceptions import CommandExecutionError, DeviceOfflineError
from orca.cli.client import OrcaClient
from orca.operations.binding import FileAnswer, RestBinding, Route
from orca.operations.catalog_models import NoContent
from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError
from orca.operations.blocker_models import BlockersPayload, payload_of
from orca.resource_models.device_error import DeviceUnderExternalControlError
from orca.runtime.runtime_interface import ISystemRuntime


class _Req(BaseModel):
    name: str
    reason: str | None = None


class _Resp(BaseModel):
    name: str
    reason: str | None


class _Echo:
    Request: ClassVar[type[BaseModel]] = _Req
    Response: ClassVar[type[BaseModel]] = _Resp
    binding: ClassVar[RestBinding] = RestBinding(
        method="DELETE", path="/things/{name}", tag="t",
    )

    async def run(self, req: _Req, /) -> _Resp:
        return _Resp(name=req.name, reason=req.reason)


class _Refuses(_Echo):
    binding: ClassVar[RestBinding] = RestBinding(
        method="GET", path="/refused/{name}", tag="t",
    )

    async def run(self, req: _Req, /) -> _Resp:
        raise OperationError.of(ErrorCode.WORKFLOW_NOT_FOUND, "no such thing", name=req.name)


class _NeedsARuntime(_Echo):
    binding: ClassVar[RestBinding] = RestBinding(
        method="GET", path="/runtime/{name}", tag="t",
    )


class _CarriesBlockers(_Echo):
    binding: ClassVar[RestBinding] = RestBinding(
        method="PUT", path="/carried/{name}", tag="t",
        carries_blockers=True,
    )


class _Public(_Echo):
    binding: ClassVar[RestBinding] = RestBinding(
        method="GET", path="/public/{name}", tag="t",
    )


class _NoContent:
    Request: ClassVar[type[BaseModel]] = _Req
    Response: ClassVar[type[BaseModel]] = NoContent
    binding: ClassVar[RestBinding] = RestBinding(
        method="DELETE", path="/empty/{name}", tag="t", status_code=204,
    )

    async def run(self, req: _Req, /) -> NoContent:
        return NoContent()


class _Context:
    def runtime(self) -> ISystemRuntime:
        raise OperationError.runtime_not_ready("no system loaded")

    async def blockers(self) -> BlockersPayload:
        return payload_of([])


def _build_needing_a_runtime(context: _Context) -> _NeedsARuntime:
    context.runtime()
    return _NeedsARuntime()


class _Catalog:
    def entries(self) -> Sequence[OperationEntry[_Context]]:
        return (
            OperationEntry(_Echo, lambda _: _Echo()),
            OperationEntry(_Refuses, lambda _: _Refuses()),
            OperationEntry(_NeedsARuntime, _build_needing_a_runtime),
            OperationEntry(_CarriesBlockers, lambda _: _CarriesBlockers()),
            OperationEntry(_Public, lambda _: _Public(), public=True),
            OperationEntry(_NoContent, lambda _: _NoContent()),
        )


def _refuse_without_key(request: Request) -> None:
    if "x-key" not in request.headers:
        raise HTTPException(status_code=401, detail="no key")


class _Auth:
    def dependencies(self) -> Sequence[DependsParam]:
        return [Depends(_refuse_without_key)]

    async def refuse_websocket(self, websocket: WebSocket) -> WebSocketRefusal | None:
        return None


def _client() -> TestClient:
    router = APIRouter()
    mount_api(router, _Catalog(), lambda _: _Context(), _Auth())
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, headers={"x-key": "k"})


def test_an_optional_field_is_an_optional_query_parameter() -> None:
    client = _client()
    assert client.delete("/api/things/a").json() == {"name": "a", "reason": None}
    assert client.delete("/api/things/a", params={"reason": "r"}).json() == {"name": "a", "reason": "r"}


def test_an_operation_error_is_sent_as_code_message_extras() -> None:
    response = _client().get("/api/refused/a")
    assert response.status_code == 404
    assert response.json()["detail"] == {
        "code": "workflow_not_found", "message": "no such thing", "extras": {"name": "a"},
    }


def test_a_route_with_no_runtime_answers_503_runtime_not_ready() -> None:
    response = _client().get("/api/runtime/a")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "runtime_not_ready"


def test_a_route_that_carries_blockers_adds_them_to_its_answer() -> None:
    body = _client().put("/api/carried/a", json={}).json()
    assert body["name"] == "a"
    assert body["blockers"] == []


def test_every_route_needs_auth_unless_its_entry_is_public() -> None:
    client = _client()
    client.headers.pop("x-key")
    assert client.delete("/api/things/a").status_code == 401
    assert client.get("/api/public/a").status_code == 200


def test_a_write_whose_fields_are_all_optional_needs_no_body() -> None:
    response = _client().put("/api/carried/a")
    assert response.status_code == 200, response.text
    assert response.json()["reason"] is None


def test_a_204_binding_sends_no_body() -> None:
    response = _client().delete("/api/empty/a")

    assert response.status_code == 204
    assert response.content == b""


class _AFile:
    Request: ClassVar[type[BaseModel]] = _Req
    Response: ClassVar[type[BaseModel]] = FileAnswer
    binding: ClassVar[RestBinding] = RestBinding(method="GET", path="/files/{name}", tag="t")

    def __init__(self, folder: Path) -> None:
        self._folder = folder

    async def run(self, req: _Req, /) -> FileAnswer:
        return FileAnswer(path=str(self._folder / req.name), media_type="model/stl")


class _FileCatalog:
    def __init__(self, folder: Path) -> None:
        self._folder = folder

    def entries(self) -> Sequence[OperationEntry[_Context]]:
        return (OperationEntry(_AFile, lambda _: _AFile(self._folder)),)


def test_an_operation_that_answers_a_file_sends_its_bytes_with_its_media_type(tmp_path: Path) -> None:
    (tmp_path / "arm.stl").write_bytes(b"solid arm")
    router = APIRouter()
    mount_api(router, _FileCatalog(tmp_path), lambda _: _Context(), _Auth())
    app = FastAPI()
    app.include_router(router)

    response = TestClient(app, headers={"x-key": "k"}).get("/api/files/arm.stl")

    assert response.status_code == 200
    assert response.content == b"solid arm"
    assert response.headers["content-type"] == "model/stl"


class _RaisesDeviceError(_Echo):
    binding: ClassVar[RestBinding] = RestBinding(
        method="POST", path="/drives-a-device/{name}", tag="t",
    )

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def run(self, req: _Req, /) -> _Resp:
        raise self._error


class _DeviceErrorCatalog:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def entries(self) -> Sequence[OperationEntry[_Context]]:
        return (OperationEntry(_RaisesDeviceError, lambda _: _RaisesDeviceError(self._error)),)


def _post_raising(error: Exception) -> Response:
    router = APIRouter()
    mount_api(router, _DeviceErrorCatalog(error), lambda _: _Context(), _Auth())
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, headers={"x-key": "k"}, raise_server_exceptions=False)
    return client.post("/api/drives-a-device/a", json={})


def test_an_offline_device_inside_an_operation_answers_503_device_offline() -> None:
    response = _post_raising(DeviceOfflineError("transporter_1 is not connected"))
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "device_offline"
    assert response.json()["detail"]["message"] == "transporter_1 is not connected"


def test_a_driver_failure_inside_an_operation_keeps_its_driver_error_type() -> None:
    response = _post_raising(CommandExecutionError("overtemperature", error_type="SealerError"))
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "command_error"
    assert response.json()["detail"]["extras"]["driver_error_type"] == "SealerError"


def test_a_device_under_external_control_inside_an_operation_answers_409_with_its_name() -> None:
    response = _post_raising(DeviceUnderExternalControlError("arm_1"))
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "device_under_external_control"
    assert response.json()["detail"]["extras"]["device_name"] == "arm_1"


class _OutsideTheApiPrefix(_Echo):
    binding: ClassVar[RestBinding] = RestBinding(
        method="GET", path="/bare/{name}", tag="t", under_api=False,
    )


class _BareCatalog:
    def entries(self) -> Sequence[OperationEntry[_Context]]:
        return (OperationEntry(_OutsideTheApiPrefix, lambda _: _OutsideTheApiPrefix(), public=True),)


class _ClientOverTheTestApp(OrcaClient):
    def __init__(self, test_client: TestClient) -> None:
        super().__init__("http://testserver")
        self._test_client = test_client

    def _make_http_client(self) -> TestClient:
        return self._test_client


def test_a_route_declared_outside_the_api_prefix_is_mounted_and_called_at_its_bare_path() -> None:
    router = APIRouter()
    mount_api(router, _BareCatalog(), lambda _: _Context(), _Auth())
    app = FastAPI()
    app.include_router(router)
    test_client = TestClient(app)

    assert test_client.get("/bare/a").status_code == 200
    assert test_client.get("/api/bare/a").status_code == 404

    route = Route(_Req, _Resp, _OutsideTheApiPrefix.binding)
    answer = _ClientOverTheTestApp(test_client)._call(route, _Req(name="a"))
    assert answer == _Resp(name="a", reason=None)

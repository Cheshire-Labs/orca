"""Unit + integration tests for `GetSystemInfoOperation`.

Unit tests mock `ISystemRuntime` and exercise `run()` directly. The
integration tests go through the daemon's binder + FastAPI router and
verify the wire shape on `/operations/system-info`.
"""

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from orca.daemon.app import create_app
from orca.operations._protocol import OperationError, OperationErrorCode
from orca.operations.system import (
    GetSystemInfoOperation,
    GetSystemInfoRequest,
)
from orca.runtime.facades.registry import IRegistryFacade, RegistryFacade
from orca.runtime.registries.null_gateway_registry import NullDeviceConnectionSource
from orca.runtime.system_runtime import SystemRuntime

from tests.test_system_runtime import _build_simple_system


@pytest_asyncio.fixture
async def loaded_client(runtime: SystemRuntime) -> AsyncIterator[AsyncClient]:
    """Daemon with the daemon-conftest fixture system pre-loaded."""
    app = create_app(initial_system_runtime=runtime)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://daemon.test",
    ) as c:
        yield c


@pytest_asyncio.fixture
async def empty_client() -> AsyncIterator[AsyncClient]:
    """Daemon with no system loaded."""
    app = create_app(initial_system_runtime=None)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://daemon.test",
    ) as c:
        yield c


# -- Unit tests ----------------------------------------------------


class _RuntimeWithRealRegistry:
    """Minimal runtime exposing a real RegistryFacade over a real System."""

    def __init__(self, registry: RegistryFacade) -> None:
        self.registry = registry


@pytest.mark.asyncio
async def test_get_system_info_computes_fields_from_real_system() -> None:
    """Drive the op against a real RegistryFacade so the snapshot is computed.

    The facade reads `system.name/description/version` off the live System
    built by `_build_simple_system`; the op then maps that snapshot onto its
    response. Nothing about the unit's own logic (facade compute + op map) is
    stubbed, so a regression in either field-read or field-map fails here.
    """
    system, _ = await _build_simple_system()
    facade = RegistryFacade(
        system=system,
        list_reservations_fn=lambda _execution_id: [],
        cancel_reservation_fn=lambda _execution_id, _reservation_id: None,
        connections=NullDeviceConnectionSource(),
    )
    op = GetSystemInfoOperation(runtime=_RuntimeWithRealRegistry(facade))

    resp = await op.run(GetSystemInfoRequest())

    assert resp.name == system.name == "test_system"
    assert resp.description == system.description
    assert resp.version == system.version


@pytest.mark.asyncio
async def test_get_system_info_runtime_with_broken_registry_raises_unavailable() -> None:
    """If the runtime is in a broken/partial state, surface the categorized error."""

    class _BrokenRuntime:
        @property
        def registry(self) -> IRegistryFacade:
            raise AttributeError("registry not wired")

    op = GetSystemInfoOperation(runtime=_BrokenRuntime())
    with pytest.raises(OperationError) as excinfo:
        await op.run(GetSystemInfoRequest())
    assert excinfo.value.code == OperationErrorCode.SERVICE_UNAVAILABLE


# -- Integration tests through the daemon binder -------------------


@pytest.mark.asyncio
async def test_daemon_rest_system_info_returns_200_when_loaded(
    loaded_client: AsyncClient,
) -> None:
    """The binder wires Operation -> JSON response."""
    resp = await loaded_client.get("/operations/system-info")
    assert resp.status_code == 200
    body = resp.json()
    # ``is_simulating`` was retired in canon's sim-hierarchy v3.4 when
    # run-mode moved from deployment-scope to per-submission scope.
    assert set(body.keys()) == {"name", "description", "version"}
    assert isinstance(body["name"], str)


@pytest.mark.asyncio
async def test_daemon_rest_system_info_503_when_no_system(
    empty_client: AsyncClient,
) -> None:
    """No runtime loaded => OperationError.service_unavailable => HTTP 503."""
    resp = await empty_client.get("/operations/system-info")
    assert resp.status_code == 503
    assert "no system loaded" in resp.text.lower()

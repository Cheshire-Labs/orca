"""Tests for registry HTTP routes.

What these catch:
- Gating: every /catalog/* route requires a loaded system; 409 if not.
- Wire-up: each route reaches the corresponding IRegistryFacade method
  and returns the expected DTO shape. A regression that pointed the
  route at the wrong facade method would surface here.
- Fixture system contents: the fixture system exposes at
  least one workflow, one location (pad1), one device (shaker1). Tests
  assert non-empty lists so "route works but returns nothing" bugs
  still fail.
"""

from httpx import AsyncClient

from orca.daemon.schemas import (
    DeviceDTO,
    LocationDTO,
    MethodTemplateDTO,
    SystemInfoDTO,
    ThreadTemplateDTO,
    WorkflowTemplateDTO,
)


# -- Gating tests: every /catalog/* and /system route is 409 without a system.


async def test_system_info_gated_on_system_loaded(
    empty_client: AsyncClient,
) -> None:
    resp = await empty_client.get("/system")
    assert resp.status_code == 409


async def test_catalog_workflows_gated_on_system_loaded(
    empty_client: AsyncClient,
) -> None:
    resp = await empty_client.get("/catalog/workflows")
    assert resp.status_code == 409


async def test_catalog_methods_gated_on_system_loaded(
    empty_client: AsyncClient,
) -> None:
    resp = await empty_client.get("/catalog/methods")
    assert resp.status_code == 409


async def test_catalog_locations_gated_on_system_loaded(
    empty_client: AsyncClient,
) -> None:
    resp = await empty_client.get("/catalog/locations")
    assert resp.status_code == 409


async def test_catalog_devices_gated_on_system_loaded(
    empty_client: AsyncClient,
) -> None:
    resp = await empty_client.get("/catalog/devices")
    assert resp.status_code == 409


async def test_catalog_threads_gated_on_system_loaded(
    empty_client: AsyncClient,
) -> None:
    resp = await empty_client.get("/catalog/threads")
    assert resp.status_code == 409


# -- Wire-up tests: each route returns the expected shape against the
# -- fixture system (one workflow, one shaker, one transporter).


async def test_system_info_returns_topology_metadata(
    client: AsyncClient,
) -> None:
    resp = await client.get("/system")
    assert resp.status_code == 200
    info = SystemInfoDTO.model_validate(resp.json())
    # The in-process `client` fixture uses _build_simple_system() which
    # calls SdkToSystemBuilder(name="test_system", ...).
    # Sim-hierarchy v3.4: SystemInfoDTO is identity-only; no is_simulating.
    assert info.name == "test_system"


async def test_catalog_workflows_includes_simple_workflow(
    client: AsyncClient,
) -> None:
    resp = await client.get("/catalog/workflows")
    assert resp.status_code == 200
    names = [WorkflowTemplateDTO.model_validate(w).name for w in resp.json()]
    assert "simple_workflow" in names


async def test_catalog_methods_returns_templates_before_any_submit(
    client: AsyncClient,
) -> None:
    """`/catalog/methods` returns every MethodTemplate declared in the loaded
    system, regardless of whether any workflow has been submitted. Templates
    are registered at `MethodTemplate.__init__` time (via `_PENDING` +
    `SdkToSystemBuilder` drain), so they are visible immediately after load.
    """
    resp = await client.get("/catalog/methods")
    assert resp.status_code == 200
    items = [MethodTemplateDTO.model_validate(m) for m in resp.json()]
    names = [m.name for m in items]
    assert "shake_method" in names, (
        f"expected shake_method in registered list with no submit; got {names}"
    )


async def test_catalog_methods_carries_failure_policy(
    client: AsyncClient,
) -> None:
    """`/catalog/methods` exposes each template's failure policy as the
    `FailurePolicy` enum (serialized to its `.name` on the wire). Attribute
    access works with the enum so callers don't grep on magic strings.
    """
    from orca.workflow_models.status_enums import FailurePolicy

    resp = await client.get("/catalog/methods")
    assert resp.status_code == 200
    items = [MethodTemplateDTO.model_validate(m) for m in resp.json()]
    shake = next((m for m in items if m.name == "shake_method"), None)
    assert shake is not None, "fixture must expose shake_method"
    # The fixture constructs MethodTemplate with the default policy: PAUSE.
    assert shake.failure_policy is FailurePolicy.PAUSE, (
        f"expected FailurePolicy.PAUSE; got {shake.failure_policy!r}"
    )
    # Wire form is the name string, not the int value from auto().
    payload = resp.json()
    shake_raw = next(m for m in payload if m["name"] == "shake_method")
    assert shake_raw["failure_policy"] == "PAUSE", (
        f"wire format must be the enum name; got {shake_raw['failure_policy']!r}"
    )


async def test_catalog_threads_returns_templates_before_any_submit(
    client: AsyncClient,
) -> None:
    """`/catalog/threads` returns every ThreadTemplate declared in the loaded
    system. The fixture attaches its thread to a workflow; that still makes
    it a template and it must appear under /catalog/threads.
    """
    resp = await client.get("/catalog/threads")
    assert resp.status_code == 200
    items = [ThreadTemplateDTO.model_validate(t) for t in resp.json()]
    # The fixture's ThreadTemplate uses the plate labware's name ("plate_96")
    # as its thread-template name (see ThreadTemplate.name at
    # workflow_models/thread_template.py).
    names = [t.name for t in items]
    assert "plate_96" in names, (
        f"expected 'plate_96' thread template in list; got {names}"
    )


async def test_catalog_locations_includes_pad1(
    client: AsyncClient,
) -> None:
    resp = await client.get("/catalog/locations")
    assert resp.status_code == 200
    names = [LocationDTO.model_validate(loc).name for loc in resp.json()]
    assert "pad1" in names


async def test_catalog_devices_includes_shaker1(
    client: AsyncClient,
) -> None:
    resp = await client.get("/catalog/devices")
    assert resp.status_code == 200
    devices = [DeviceDTO.model_validate(d) for d in resp.json()]
    assert any(d.name == "shaker1" for d in devices), (
        f"expected shaker1 in {[d.name for d in devices]}"
    )

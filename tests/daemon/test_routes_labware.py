"""Tests for labware Operation routes on the daemon.

The per-route legacy ``/labware/...`` daemon URLs are gone;
the unified Operations surface (``POST /operations/<op>``) is the
canonical home for labware read + write.

What these catch:
- The Operations binder surfaces "no system loaded" as 503
  ``service_unavailable``, not 409.
- Empty state handles cleanly (fresh system has no registered labware
  until a workflow submission creates ExecutingLabware instances).
- Unknown id / barcode -> 404, not 500.
- Register -> 200 with the new snapshot; edit/reset -> 200 with
  ``{status: "ok"}``.
"""

from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from orca.daemon.app import create_app
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.system_runtime import SystemRuntime

from tests.test_system_runtime import _build_simple_system


@pytest_asyncio.fixture
async def labware_client() -> AsyncIterator[AsyncClient]:
    """Client with an InMemoryLabwareStore wired in explicitly."""
    system, _ = await _build_simple_system()
    rt = SystemRuntime(system, labware_store=InMemoryLabwareStore())
    await rt.start()
    app = create_app(initial_system_runtime=rt)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://daemon.test",
        ) as c:
            yield c
    finally:
        if rt.state.name == "RUNNING":
            await rt.shutdown(confirm=True)


async def test_labware_list_gated(empty_client: AsyncClient) -> None:
    resp = await empty_client.get("/operations/list-labware")
    assert resp.status_code == 503


async def test_labware_get_by_id_gated(empty_client: AsyncClient) -> None:
    resp = await empty_client.post(
        "/operations/get-labware-by-id",
        json={"labware_id": "something"},
    )
    assert resp.status_code == 503


async def test_labware_get_by_barcode_gated(empty_client: AsyncClient) -> None:
    resp = await empty_client.post(
        "/operations/get-labware-by-barcode",
        json={"barcode": "123"},
    )
    assert resp.status_code == 503


async def test_labware_history_gated(empty_client: AsyncClient) -> None:
    resp = await empty_client.post(
        "/operations/get-labware-history",
        json={"labware_id": "something"},
    )
    assert resp.status_code == 503


async def test_labware_get_by_id_unknown_returns_404(
    client: AsyncClient,
) -> None:
    resp = await client.post(
        "/operations/get-labware-by-id",
        json={"labware_id": "does-not-exist"},
    )
    assert resp.status_code == 404


async def test_labware_get_by_barcode_unknown_returns_404(
    client: AsyncClient,
) -> None:
    resp = await client.post(
        "/operations/get-labware-by-barcode",
        json={"barcode": "does-not-exist"},
    )
    assert resp.status_code == 404


async def test_labware_list_succeeds_on_fresh_system(
    client: AsyncClient,
) -> None:
    """Fresh system has no ExecutingLabware registered yet; list must
    still succeed with a (possibly empty) array, not 500."""
    resp = await client.get("/operations/list-labware")
    assert resp.status_code == 200
    assert isinstance(resp.json()["labware"], list)


# -- Write routes ----------------------------------------------------------


async def test_labware_register_creates_new_instance(
    labware_client: AsyncClient,
) -> None:
    """register-labware instantiates a new labware from a template and
    returns it in the response. Subsequent get-by-id finds it.

    OPERATOR-level; no reason required (failure mode is
    register-wrong-template, visible at first move).
    """
    resp = await labware_client.post(
        "/operations/register-labware",
        json={
            "template_name": "plate_96",
            "barcode": "BC-1",
            "location": "pad1",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "registered"
    snapshot = body["labware"]
    assert snapshot["template_name"] == "plate_96"
    assert snapshot["barcode"] == "BC-1"
    assert snapshot["current_location"] == "pad1"


async def test_labware_register_unknown_template_returns_404(
    labware_client: AsyncClient,
) -> None:
    resp = await labware_client.post(
        "/operations/register-labware",
        json={"template_name": "no_such_template"},
    )
    assert resp.status_code == 404


async def test_labware_edit_location_requires_reason(
    labware_client: AsyncClient,
) -> None:
    """edit-location requires a reason; empty string -> 422.

    The schema is ``reason: str = Field(..., min_length=1)`` so the rejection
    happens at Pydantic body-parse time rather than deep in the @dangerous
    facade call.
    """
    reg = await labware_client.post(
        "/operations/register-labware",
        json={"template_name": "plate_96", "location": "pad1"},
    )
    assert reg.status_code == 200
    labware_id = reg.json()["labware"]["id"]

    resp = await labware_client.post(
        "/operations/edit-labware-location",
        json={"labware_id": labware_id, "location": "pad1", "reason": ""},
    )
    assert resp.status_code == 422


async def test_labware_edit_barcode_rewrites_barcode_index(
    labware_client: AsyncClient,
) -> None:
    """Edit-barcode is OPERATOR-level; no reason required."""
    reg = await labware_client.post(
        "/operations/register-labware",
        json={
            "template_name": "plate_96",
            "barcode": "BC-orig",
            "location": "pad1",
        },
    )
    assert reg.status_code == 200
    labware_id = reg.json()["labware"]["id"]

    resp = await labware_client.post(
        "/operations/edit-labware-barcode",
        json={"labware_id": labware_id, "new_barcode": "BC-NEW"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    lookup = await labware_client.post(
        "/operations/get-labware-by-id", json={"labware_id": labware_id},
    )
    assert lookup.status_code == 200
    assert lookup.json()["labware"]["barcode"] == "BC-NEW"


async def test_labware_edit_location_moves_instance(
    labware_client: AsyncClient,
) -> None:
    reg = await labware_client.post(
        "/operations/register-labware",
        json={"template_name": "plate_96", "location": "pad1"},
    )
    labware_id = reg.json()["labware"]["id"]

    resp = await labware_client.post(
        "/operations/edit-labware-location",
        json={
            "labware_id": labware_id,
            "location": "shaker1",
            "reason": "operator-audit",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    lookup = await labware_client.post(
        "/operations/get-labware-by-id", json={"labware_id": labware_id},
    )
    assert lookup.status_code == 200
    # A bare device name resolves to its single site: the plate lands at the
    # real slot, not the off-graph mutex. Pre-fix this asserted "shaker1", which
    # meant the plate sat on the mutex and blocked every action on the device.
    assert lookup.json()["labware"]["current_location"] == "shaker1/slot"


async def test_labware_register_at_bridge_surfaces_in_loaded_labware_ids(
    labware_client: AsyncClient,
) -> None:
    """Sister bug to edit-location sync: ``register-labware`` at a
    device-backed (LabwareStagingBridge) site must also surface the
    labware on the registry's ``loaded_labware_ids``. Pre-fix the bridge
    held the plate only in ``staged_labware``, which ``loaded_labware``
    did not include, so ``describe location shaker1/slot`` reported
    (none) immediately after a successful register. The device's labware
    site is the flat node ``shaker1/slot``.
    """
    reg = await labware_client.post(
        "/operations/register-labware",
        json={"template_name": "plate_96", "location": "shaker1/slot"},
    )
    assert reg.status_code == 200, reg.text
    labware_id = reg.json()["labware"]["id"]

    locs = await labware_client.get("/operations/list-locations")
    assert locs.status_code == 200
    by_name = {loc["name"]: loc for loc in locs.json()["locations"]}
    assert labware_id in by_name["shaker1/slot"]["loaded_labware_ids"], (
        "bridge-backed site should report the registered labware "
        "in loaded_labware_ids (the bridge's staged_labware counts as "
        "'loaded at this slot' for display purposes)"
    )


async def test_labware_edit_location_syncs_resource_inventory(
    labware_client: AsyncClient,
) -> None:
    """Edit-location must sync the location-side resource inventory. Pre-fix,
    ``labware where <id>`` reported the new location but ``describe location
    <new>`` still reported (none) because the underlying resource's
    ``loaded_labware`` was only mutated by ``Location.initialize_labware``,
    which ``edit_location`` never called. Moving back to the source then
    proves the round-trip clears both sides.
    """
    reg = await labware_client.post(
        "/operations/register-labware",
        json={"template_name": "plate_96", "location": "pad1"},
    )
    assert reg.status_code == 200, reg.text
    labware_id = reg.json()["labware"]["id"]

    resp = await labware_client.post(
        "/operations/edit-labware-location",
        json={
            "labware_id": labware_id,
            "location": "shaker1/slot",
            "reason": "operator-audit",
        },
    )
    assert resp.status_code == 200, resp.text

    locs = await labware_client.get("/operations/list-locations")
    assert locs.status_code == 200
    by_name = {loc["name"]: loc for loc in locs.json()["locations"]}
    assert labware_id in by_name["shaker1/slot"]["loaded_labware_ids"], (
        "target location should report the moved labware in loaded_labware_ids"
    )
    assert labware_id not in by_name["pad1"]["loaded_labware_ids"], (
        "source location should NOT still report the moved labware"
    )

    # Round-trip: move back, both sides should flip again.
    resp = await labware_client.post(
        "/operations/edit-labware-location",
        json={
            "labware_id": labware_id,
            "location": "pad1",
            "reason": "reset",
        },
    )
    assert resp.status_code == 200, resp.text
    locs = await labware_client.get("/operations/list-locations")
    by_name = {loc["name"]: loc for loc in locs.json()["locations"]}
    assert labware_id in by_name["pad1"]["loaded_labware_ids"]
    assert labware_id not in by_name["shaker1/slot"]["loaded_labware_ids"]


async def test_labware_reset_location_requires_reason(
    labware_client: AsyncClient,
) -> None:
    """Empty reason -> 422 (Pydantic body-parse rejection, same as edit-location)."""
    reg = await labware_client.post(
        "/operations/register-labware",
        json={"template_name": "plate_96", "location": "pad1"},
    )
    labware_id = reg.json()["labware"]["id"]

    resp = await labware_client.post(
        "/operations/reset-labware-location",
        json={"labware_id": labware_id, "location": "pad1", "reason": ""},
    )
    assert resp.status_code == 422


async def test_labware_reset_location_clears_history(
    labware_client: AsyncClient,
) -> None:
    reg = await labware_client.post(
        "/operations/register-labware",
        json={"template_name": "plate_96", "location": "pad1"},
    )
    labware_id = reg.json()["labware"]["id"]

    resp = await labware_client.post(
        "/operations/reset-labware-location",
        json={
            "labware_id": labware_id,
            "location": "shaker1",
            "reason": "pre-run reseat",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    lookup = await labware_client.post(
        "/operations/get-labware-by-id", json={"labware_id": labware_id},
    )
    assert lookup.status_code == 200
    # A bare device name resolves to its single site: the plate lands at the
    # real slot, not the off-graph mutex. Pre-fix this asserted "shaker1", which
    # meant the plate sat on the mutex and blocked every action on the device.
    assert lookup.json()["labware"]["current_location"] == "shaker1/slot"


async def test_labware_edit_location_unknown_id_returns_404(
    labware_client: AsyncClient,
) -> None:
    resp = await labware_client.post(
        "/operations/edit-labware-location",
        json={
            "labware_id": "nonexistent", "location": "pad1", "reason": "test",
        },
    )
    assert resp.status_code == 404


async def test_labware_edit_barcode_unknown_id_returns_404(
    labware_client: AsyncClient,
) -> None:
    resp = await labware_client.post(
        "/operations/edit-labware-barcode",
        json={"labware_id": "nonexistent", "new_barcode": "BC-1"},
    )
    assert resp.status_code == 404


async def test_labware_reset_location_unknown_id_returns_404(
    labware_client: AsyncClient,
) -> None:
    resp = await labware_client.post(
        "/operations/reset-labware-location",
        json={
            "labware_id": "nonexistent", "location": "pad1", "reason": "test",
        },
    )
    assert resp.status_code == 404


# -- Well-volume CRUD routes -----------------------------------------------


async def _register_plate(labware_client: AsyncClient) -> str:
    reg = await labware_client.post(
        "/operations/register-labware", json={"template_name": "plate_96"},
    )
    assert reg.status_code == 200, reg.text
    return reg.json()["labware"]["id"]


async def test_set_then_get_well_volumes_round_trip(
    labware_client: AsyncClient,
) -> None:
    labware_id = await _register_plate(labware_client)
    set_resp = await labware_client.post(
        "/operations/set-well-volumes",
        json={
            "labware_id": labware_id,
            "well_volumes": {"A1": 100.0, "A2": 50.0},
            "reason": "operator prefill",
        },
    )
    assert set_resp.status_code == 200, set_resp.text
    assert set_resp.json()["status"] == "ok"

    get_resp = await labware_client.post(
        "/operations/get-well-volumes", json={"labware_id": labware_id},
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["well_volumes"] == {"A1": 100.0, "A2": 50.0}


async def test_set_well_volumes_blank_reason_returns_422(
    labware_client: AsyncClient,
) -> None:
    labware_id = await _register_plate(labware_client)
    resp = await labware_client.post(
        "/operations/set-well-volumes",
        json={"labware_id": labware_id, "well_volumes": {"A1": 10.0}, "reason": ""},
    )
    assert resp.status_code == 422


async def test_set_well_volumes_overfill_rejected(
    labware_client: AsyncClient,
) -> None:
    labware_id = await _register_plate(labware_client)
    # SimWell capacity is 385 uL.
    resp = await labware_client.post(
        "/operations/set-well-volumes",
        json={"labware_id": labware_id, "well_volumes": {"A1": 1000.0}, "reason": "r"},
    )
    assert resp.status_code == 400
    assert "capacity" in resp.text


async def test_get_well_volumes_unknown_id_returns_404(
    labware_client: AsyncClient,
) -> None:
    resp = await labware_client.post(
        "/operations/get-well-volumes", json={"labware_id": "nonexistent"},
    )
    assert resp.status_code == 404


@pytest_asyncio.fixture
async def rack_client() -> AsyncIterator[AsyncClient]:
    """Client over a system that declares a tip rack template."""
    from tests.test_tip_state_reseed import _build_system_with_rack

    system = await _build_system_with_rack()
    rt = SystemRuntime(system, labware_store=InMemoryLabwareStore())
    await rt.start()
    app = create_app(initial_system_runtime=rt)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://daemon.test",
        ) as c:
            yield c
    finally:
        if rt.state.name == "RUNNING":
            await rt.shutdown(confirm=True)


async def _register_rack(rack_client: AsyncClient) -> str:
    reg = await rack_client.post(
        "/operations/register-labware", json={"template_name": "tips_96"},
    )
    assert reg.status_code == 200, reg.text
    return reg.json()["labware"]["id"]


async def test_set_then_get_tip_state_round_trip(
    rack_client: AsyncClient,
) -> None:
    labware_id = await _register_rack(rack_client)
    set_resp = await rack_client.post(
        "/operations/set-tip-state",
        json={
            "labware_id": labware_id,
            "tip_positions_present": ["A1", "B1"],
            "reason": "fresh rack, first column only",
        },
    )
    assert set_resp.status_code == 200, set_resp.text
    assert set_resp.json()["status"] == "ok"

    get_resp = await rack_client.post(
        "/operations/get-tip-state", json={"labware_id": labware_id},
    )
    assert get_resp.status_code == 200
    body = get_resp.json()
    assert body["tip_positions_present"] == ["A1", "B1"]
    assert body["provenance"] == "known"


async def test_set_tip_state_blank_reason_returns_422(
    rack_client: AsyncClient,
) -> None:
    labware_id = await _register_rack(rack_client)
    resp = await rack_client.post(
        "/operations/set-tip-state",
        json={"labware_id": labware_id, "tip_positions_present": ["A1"], "reason": ""},
    )
    assert resp.status_code == 422


async def test_set_tip_state_on_a_plate_rejected(
    rack_client: AsyncClient,
) -> None:
    reg = await rack_client.post(
        "/operations/register-labware", json={"template_name": "plate_96"},
    )
    assert reg.status_code == 200, reg.text
    plate_id = reg.json()["labware"]["id"]
    resp = await rack_client.post(
        "/operations/set-tip-state",
        json={"labware_id": plate_id, "tip_positions_present": ["A1"], "reason": "r"},
    )
    assert resp.status_code == 400
    assert "tip rack" in resp.text


async def test_confirm_tip_state_on_a_registered_rack_returns_ok(
    rack_client: AsyncClient,
) -> None:
    """Registering writes the rack's opening entry, so there is a layout to
    agree with straight away. (Confirming a labware the record knows nothing
    about is still refused; that state is not reachable over REST, so the
    refusal is pinned at the facade instead.)"""
    labware_id = await _register_rack(rack_client)
    resp = await rack_client.post(
        "/operations/confirm-tip-state", json={"labware_id": labware_id},
    )
    assert resp.status_code == 200, resp.text


async def test_confirm_tip_state_after_set_returns_ok(
    rack_client: AsyncClient,
) -> None:
    labware_id = await _register_rack(rack_client)
    await rack_client.post(
        "/operations/set-tip-state",
        json={"labware_id": labware_id, "tip_positions_present": ["A1"], "reason": "r"},
    )
    resp = await rack_client.post(
        "/operations/confirm-tip-state", json={"labware_id": labware_id},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_get_tip_state_unknown_id_returns_404(
    rack_client: AsyncClient,
) -> None:
    resp = await rack_client.post(
        "/operations/get-tip-state", json={"labware_id": "nonexistent"},
    )
    assert resp.status_code == 404

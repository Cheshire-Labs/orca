"""Tests for incident HTTP routes.

What these catch:
- Gating: /incidents, /incidents/{id}, /incidents/{id}/ack, /incidents/ack-all
  all require a loaded system; 409 pre-load.
- Empty-state: list returns [] on a fresh system (no incidents recorded yet).
- Bad category string -> 400 BAD_REQUEST (not 500).
- Unknown incident id -> 404 from get + ack.
"""

from httpx import AsyncClient


async def test_incidents_list_gated(
    empty_client: AsyncClient,
) -> None:
    resp = await empty_client.get("/incidents")
    assert resp.status_code == 409


async def test_incidents_get_gated(
    empty_client: AsyncClient,
) -> None:
    resp = await empty_client.get("/incidents/does-not-exist")
    assert resp.status_code == 409


async def test_incidents_ack_gated(
    empty_client: AsyncClient,
) -> None:
    resp = await empty_client.post("/incidents/does-not-exist/ack")
    assert resp.status_code == 409


async def test_incidents_ack_all_gated(
    empty_client: AsyncClient,
) -> None:
    resp = await empty_client.post("/incidents/ack-all")
    assert resp.status_code == 409


async def test_incidents_list_empty_on_fresh_system(
    client: AsyncClient,
) -> None:
    """Fresh system -> no incidents recorded. Empty list is success,
    not an error."""
    resp = await client.get("/incidents")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_incidents_list_rejects_unknown_category(
    client: AsyncClient,
) -> None:
    resp = await client.get("/incidents", params={"category": "NOPE"})
    assert resp.status_code == 400


async def test_incidents_get_unknown_id_returns_404(
    client: AsyncClient,
) -> None:
    resp = await client.get("/incidents/does-not-exist")
    assert resp.status_code == 404


async def test_incidents_ack_unknown_id_returns_404(
    client: AsyncClient,
) -> None:
    resp = await client.post("/incidents/does-not-exist/ack")
    assert resp.status_code == 404


async def test_incidents_ack_all_empty_returns_zero(
    client: AsyncClient,
) -> None:
    """No incidents to ack -> acknowledged_count is 0, not an error."""
    resp = await client.post("/incidents/ack-all")
    assert resp.status_code == 200
    assert resp.json()["acknowledged_count"] == 0

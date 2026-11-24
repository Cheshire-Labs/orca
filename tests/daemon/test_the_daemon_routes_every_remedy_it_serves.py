"""A remedy step on the daemon names a route the daemon mounts, and none it serves is left out."""

from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from orca.daemon.app import create_app
from orca.daemon.remedy_routes import DAEMON_ACTION_ROUTES
from orca.runtime.remedies import RemedyAction


def _mounted() -> set[str]:
    return {
        f"{method} {route.path.replace('{', '<').replace('}', '>')}"
        for route in create_app().routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }


def test_every_remedy_route_is_mounted() -> None:
    mounted = _mounted()
    assert {r.rest for r in DAEMON_ACTION_ROUTES.values()} <= mounted


def test_a_held_reservation_can_be_cancelled_from_its_remedy() -> None:
    route = DAEMON_ACTION_ROUTES[RemedyAction.CANCEL_RESERVATION]
    assert route.rest == "DELETE /api/executions/<execution_id>/reservations/<reservation_id>"
    assert route.query == ("reason",)


async def test_a_recovery_route_answers_with_what_still_blocks_the_run(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/devices/shaker1/clear-fault", json={"reason": "test"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["blockers_known"] is True
    assert body["blocker_count"] == len(body["blockers"])


async def test_a_daemon_with_nothing_mounted_points_at_mounting_its_package() -> None:
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://daemon") as c:
        resp = await c.get("/api/runtime/status")

    assert resp.status_code == 200, resp.text
    blocker = resp.json()["blockers"][0]
    assert "Submit" not in blocker["detail"]
    step = blocker["remedies"][0]["steps"][0]
    assert step["rest"] == "POST /api/runtime/mount-package"
    assert step["cli"] == "orca topology mount"

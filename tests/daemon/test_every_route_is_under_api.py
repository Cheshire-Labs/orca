"""The daemon serves every Operation under /api; only liveness, shutdown and the websockets sit outside it."""

from fastapi.routing import APIRoute, APIWebSocketRoute

from orca.daemon.app import create_app

_OUTSIDE_THE_API = {"/health", "/shutdown", "/ws/events", "/ws/devices"}


def test_every_daemon_route_is_under_api_but_liveness_shutdown_and_the_websockets() -> None:
    paths = {
        route.path for route in create_app().routes
        if isinstance(route, (APIRoute, APIWebSocketRoute))
    }

    assert {p for p in paths if not p.startswith("/api/")} == _OUTSIDE_THE_API


def test_the_daemon_serves_the_device_command_routes_a_hosted_server_serves() -> None:
    paths = {route.path for route in create_app().routes if isinstance(route, APIRoute)}

    assert {
        "/api/devices/{device_id}/introspection",
        "/api/devices/{device_id}/commands",
    } <= paths

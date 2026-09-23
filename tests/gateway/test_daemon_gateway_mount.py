"""The orca daemon serves the device-bridge gateway websocket route."""

from unittest.mock import MagicMock

import pytest

from orca.daemon.app import create_app
from orca.gateway.auth import NullConnectionAuthenticator
from orca.gateway.websocket.router import _authenticator_from_websocket


def test_daemon_mounts_ws_devices_route() -> None:
    app = create_app()
    ws_paths = {
        p
        for route in app.routes
        if (p := getattr(route, "path", None)) is not None
    }
    assert "/ws/devices" in ws_paths


@pytest.mark.asyncio
async def test_ws_devices_default_authenticator_allows_keyless_connect() -> None:
    """A fresh daemon leaves connection_authenticator unset; the router must
    resolve that to the allow-all NullConnectionAuthenticator, so a keyless
    device bridge connects. Asserting the resolved authenticator (not the raw
    unset attribute) is what proves the default-allow behavior actually
    holds."""
    app = create_app()
    assert getattr(app.state, "connection_authenticator", None) is None

    websocket = MagicMock()
    websocket.app = app
    authenticator = _authenticator_from_websocket(websocket)
    assert isinstance(authenticator, NullConnectionAuthenticator)
    assert await authenticator.authenticate(None) is True

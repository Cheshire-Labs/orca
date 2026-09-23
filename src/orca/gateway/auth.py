"""Connection authentication seam for the device-bridge gateway.

The gateway's websocket handshake is authenticated through an injected
``IConnectionAuthenticator``. orca-core ships ``NullConnectionAuthenticator``
(allow-all): a single-node engine trusts its own LAN and needs no shared secret.
A hosting layer (running the gateway remotely, at scale) stores its own
authenticator on ``app.state.connection_authenticator`` to enforce a real check
(e.g. a deployment secret).
"""

from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class IConnectionAuthenticator(Protocol):
    """Decides whether a device-bridge handshake may connect.

    ``authenticate`` receives the X-API-Key header value (``None`` if the
    client sent none) and returns True to accept, False to reject. The
    implementation owns the whole decision, including whether a missing key is
    acceptable, so a single-node deployment can allow keyless localhost clients
    while a hosted deployment requires the secret.
    """

    async def authenticate(self, api_key: Optional[str]) -> bool: ...


class NullConnectionAuthenticator:
    """Allow-all authenticator for standalone single-node operation."""

    async def authenticate(self, api_key: Optional[str]) -> bool:
        return True

"""Disconnect-grace timeout source for mid-command WebSocket drops.

When a device-bridge WebSocket disconnects with a command in flight, the
controller holds the pending future and arms a disconnect timer; on
reconnect within the grace the command is resent (or failed, per
``resend_on_reconnect``), and on grace expiry the future fails with
``DeviceOfflineError``.

DisconnectGrace consolidates the timeout-source decision into a single
class:

  1. **Topology override** (``set_topology_resolver``) -- the deployment's
     topology card can override per-device. Set by main.py once the
     runtime is built. Returning ``None`` from the resolver falls
     through to (2).
  2. **Fallback** -- a single conservative default (300s) for devices
     that don't have a topology override.

The pre-refactor per-device-kind ``DEFAULT_DISCONNECT_TIMEOUTS`` table
is gone -- the topology card is the canonical place for per-device
disconnect policy, and the single fallback handles uncovered devices.
"""

import logging
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


TopologyGraceResolver = Callable[[str], Awaitable[Optional[float]]]


class DisconnectGrace:
    """Resolve the disconnect-grace timeout for a device."""

    DEFAULT_FALLBACK_SECONDS: float = 300.0

    def __init__(self, *, fallback_seconds: float = DEFAULT_FALLBACK_SECONDS) -> None:
        if fallback_seconds <= 0:
            raise ValueError(
                f"fallback_seconds must be > 0, got {fallback_seconds}"
            )
        self._fallback_seconds = fallback_seconds
        self._topology_resolver: Optional[TopologyGraceResolver] = None

    @property
    def fallback_seconds(self) -> float:
        return self._fallback_seconds

    def set_topology_resolver(
        self, resolver: Optional[TopologyGraceResolver],
    ) -> None:
        """Register the deployment topology-card lookup.

        Called by main.py after the runtime is built. Pass ``None`` to
        drop the resolver back to the fallback-only path.
        """
        self._topology_resolver = resolver

    async def grace_for(self, device_id: str) -> float:
        """Return the grace seconds for the next disconnect on ``device_id``.

        Consults the topology resolver first (the deployment's per-device
        override). If the resolver is absent, returns ``None``, or raises
        an exception, falls back to ``fallback_seconds``.
        """
        if self._topology_resolver is not None:
            try:
                resolved = await self._topology_resolver(device_id)
                if resolved is not None:
                    return float(resolved)
            except Exception:
                logger.exception(
                    "disconnect grace topology resolver failed for %r; "
                    "using fallback",
                    device_id,
                )
        return self._fallback_seconds

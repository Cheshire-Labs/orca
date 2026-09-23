"""Tests for the disconnect-grace timeout source.

DisconnectGrace consolidates the pre-refactor 3-tier resolver chain into
a single class:

  1. Topology override via ``set_topology_resolver`` (deployment policy).
  2. Single fallback (300s by default).

The per-device-kind ``DEFAULT_DISCONNECT_TIMEOUTS`` table is gone.
"""

import pytest

from orca.gateway.controller.disconnect_grace import DisconnectGrace


class TestDisconnectGraceConstruction:
    def test_default_fallback_is_300s(self) -> None:
        grace = DisconnectGrace()
        assert grace.fallback_seconds == 300.0

    def test_custom_fallback_honored(self) -> None:
        grace = DisconnectGrace(fallback_seconds=120.0)
        assert grace.fallback_seconds == 120.0

    def test_zero_fallback_rejected(self) -> None:
        with pytest.raises(ValueError):
            DisconnectGrace(fallback_seconds=0.0)

    def test_negative_fallback_rejected(self) -> None:
        with pytest.raises(ValueError):
            DisconnectGrace(fallback_seconds=-1.0)


@pytest.mark.asyncio
class TestGraceFor:
    async def test_no_resolver_returns_fallback(self) -> None:
        grace = DisconnectGrace(fallback_seconds=42.0)
        assert await grace.grace_for("shaker_1") == 42.0

    async def test_resolver_value_wins(self) -> None:
        grace = DisconnectGrace(fallback_seconds=42.0)

        async def resolver(device_id: str) -> float | None:
            assert device_id == "shaker_1"
            return 99.0

        grace.set_topology_resolver(resolver)
        assert await grace.grace_for("shaker_1") == 99.0

    async def test_resolver_returns_none_falls_to_fallback(self) -> None:
        grace = DisconnectGrace(fallback_seconds=42.0)

        async def resolver(_device_id: str) -> float | None:
            return None

        grace.set_topology_resolver(resolver)
        assert await grace.grace_for("shaker_1") == 42.0

    async def test_resolver_exception_falls_to_fallback(self) -> None:
        grace = DisconnectGrace(fallback_seconds=42.0)

        async def resolver(_device_id: str) -> float | None:
            raise RuntimeError("registry boom")

        grace.set_topology_resolver(resolver)
        assert await grace.grace_for("shaker_1") == 42.0

    async def test_clearing_resolver_drops_back_to_fallback(self) -> None:
        grace = DisconnectGrace(fallback_seconds=42.0)

        async def resolver(_device_id: str) -> float | None:
            return 99.0

        grace.set_topology_resolver(resolver)
        assert await grace.grace_for("shaker_1") == 99.0

        grace.set_topology_resolver(None)
        assert await grace.grace_for("shaker_1") == 42.0

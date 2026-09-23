"""Storage.dispense() wraps the driver-side `dispense()` wire command.

The orca-side Storage device is the engine's handle on an IPlateSource
device (stacker, hotel). `Storage.dispense()` is a thin wrapper that
delegates to the underlying IStorageDriver. orca-side concerns
(fabricating the LabwareInstance, slot writes, observer fan-out) live
above this call in the FromSource spawn-action (separate
branch); this method only fires the wire command.
"""

from unittest.mock import AsyncMock, patch

from cheshire_drivers.sims import SimStorageDriver

from orca.devices.devices import Storage
from orca.devices.device_interfaces import IPlateSource


class TestStorageDispenseWrapper:
    async def test_storage_dispense_calls_driver_dispense(self) -> None:
        storage = Storage("test_stacker")
        mock_dispense = AsyncMock(return_value=None)
        storage.driver.dispense = mock_dispense  # type: ignore[method-assign]

        result = await storage.dispense()

        assert result is None
        mock_dispense.assert_awaited_once_with()

    def test_storage_is_iplate_source(self) -> None:
        """Storage declares IPlateSource so the pre-check + spawn-action
        dispatch can identify it as a multi-plate source."""
        storage = Storage("test_stacker")
        assert isinstance(storage, IPlateSource)

    async def test_storage_sim_dispense_routes_through_sim_driver(self) -> None:
        """Unmocked, dispense() resolves and drives the real sim storage driver.

        Asserting only ``result is None`` is vacuous (every ``-> None`` method
        passes it). Pin the real sim behavior: the resolved driver is the sim
        storage driver, and the wrapper delegates to that driver's own
        ``dispense`` (wrapped so the genuine sim body still runs).
        """
        storage = Storage("test_stacker")
        assert isinstance(storage.driver, SimStorageDriver)

        with patch.object(
            storage.driver, "dispense", wraps=storage.driver.dispense,
        ) as spy:
            result = await storage.dispense()

        assert result is None
        spy.assert_awaited_once_with()

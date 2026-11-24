"""A step on a `ResourcePool` is given a free, online member; a busy one is passed over, and with every member busy the
step waits until one frees."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from unittest.mock import Mock

import pytest
import pytest_asyncio

from orca.config import ReservationConfig
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.resource_pool import ResourcePool
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.system.reservation_manager.reservation_manager import ThreadReservationCoordinator
from orca.system.system_map import SystemMap
from orca.workflow_models.actions.util import ResourcePoolResolver
from tests.mock import UniversalMockDevice
from tests.test_helpers import create_simple_system_map, create_test_device, create_test_transporter, let_the_loop_run

pytestmark = pytest.mark.asyncio


@dataclass
class _Lab:
    system_map: SystemMap
    coordinator: ThreadReservationCoordinator
    shakers: dict[str, UniversalMockDevice]
    pool: ResourcePool

    def site_of(self, shaker: str) -> Location:
        [site] = self.shakers[shaker].locations
        return site

    def owner_of(self, reserved: LocationReservation) -> str:
        [owner] = [name for name, device in self.shakers.items() if reserved.reserved_location in device.locations]
        return owner

    async def occupy(self, shaker: str) -> None:
        site = self.site_of(shaker)
        assert await self.coordinator.try_reserve_location(f"holder_of_{shaker}", site.position_id, LocationReservation(site))

    def free(self, shaker: str) -> None:
        self.coordinator.release_reservations_for_threads({f"holder_of_{shaker}"})

    def is_free(self, shaker: str) -> bool:
        return self.coordinator.get_reservation_at(self.site_of(shaker).position_id) is None

    async def resolve(self) -> LocationReservation:
        resolver = ResourcePoolResolver(self.pool, ReservationConfig(retry_interval=0.01))
        return await asyncio.wait_for(resolver.resolve_action_location(
            "step", self.system_map.get_location("pad1"), self.coordinator, self.system_map,
        ), timeout=10)


@pytest_asyncio.fixture
async def lab() -> AsyncIterator[_Lab]:
    shakers = {name: create_test_device(name) for name in ("shaker_1", "shaker_2")}
    transporter = create_test_transporter("robot1", ["shaker_1", "shaker_2", "pad1"])
    _, system_map = await create_simple_system_map([transporter], dict(shakers), parking_pads={"pad1": PlatePad("pad1")})
    coordinator = ThreadReservationCoordinator(system_map, Mock(), exclusion_siblings_of=system_map.exclusion_siblings_of)
    ticker = asyncio.create_task(coordinator.start_tick_loop())
    try:
        yield _Lab(system_map, coordinator, shakers, ResourcePool("shakers", list(shakers.values())))
    finally:
        coordinator.stop_tick_loop()
        ticker.cancel()
        await asyncio.gather(ticker, return_exceptions=True)


async def test_a_step_is_given_one_member_and_the_other_stays_free(lab: _Lab) -> None:
    reserved = await lab.resolve()

    used = lab.owner_of(reserved)
    other = "shaker_2" if used == "shaker_1" else "shaker_1"
    assert not lab.is_free(used)
    assert lab.is_free(other)


async def test_a_busy_member_is_passed_over_for_a_free_one(lab: _Lab) -> None:
    await lab.occupy("shaker_1")

    assert lab.owner_of(await lab.resolve()) == "shaker_2"


async def test_an_offline_member_is_never_given_even_when_it_is_free(lab: _Lab) -> None:
    await lab.occupy("shaker_2")
    lab.shakers["shaker_1"].mark_offline(True)
    waiting = asyncio.ensure_future(lab.resolve())
    try:
        await let_the_loop_run()
        assert not waiting.done(), "the step took the offline shaker"
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_with_every_member_busy_the_step_waits_and_takes_the_member_that_frees(lab: _Lab) -> None:
    await lab.occupy("shaker_1")
    await lab.occupy("shaker_2")
    waiting = asyncio.ensure_future(lab.resolve())
    await let_the_loop_run()
    assert not waiting.done(), "the step did not wait for a busy pool"

    lab.free("shaker_2")

    assert lab.owner_of(await waiting) == "shaker_2"

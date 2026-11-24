"""A location a deadlock yield freed is kept for one thread, and only for as long as it waits for it.

A promise that outlived its thread would refuse the location to everyone else for good.
"""
import asyncio
from unittest.mock import Mock

import pytest

from orca.config import ReservationConfig
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.system.reservation_manager.reservation_manager import ThreadReservationCoordinator
from orca.workflow_models.actions.move_handler import MoveHandler
from tests.test_helpers import wait_until

_OUTPUT = "stacker_output"


class _Bench:
    def __init__(self) -> None:
        self.output = Location(_OUTPUT, resource=PlatePad(_OUTPUT))
        registry = Mock()
        registry.get_location.return_value = self.output
        self.coordinator = ThreadReservationCoordinator(registry, Mock())
        self.manager = self.coordinator._reservation_manager
        self.handler = MoveHandler(
            self.coordinator, Mock(), Mock(), ReservationConfig(retry_interval=0.05, move_reservation_timeout=5.0),
        )

    async def reserves(self, thread_id: str, plate: LabwareInstance | None = None) -> bool:
        return await self.coordinator.try_reserve_location(thread_id, _OUTPUT, LocationReservation(self.output, plate))


def _plate(name: str) -> LabwareInstance:
    return LabwareInstance(name, "96_well")


@pytest.mark.asyncio
async def test_a_promised_location_is_refused_to_everyone_but_the_thread_it_is_promised_to() -> None:
    bench = _Bench()
    bench.manager.promise(_OUTPUT, "reagent")

    assert not await bench.reserves("next-sample", _plate("next"))
    assert await bench.reserves("reagent", _plate("reagent"))


@pytest.mark.asyncio
async def test_the_promise_ends_when_the_promised_thread_is_granted_the_location() -> None:
    bench = _Bench()
    bench.manager.promise(_OUTPUT, "reagent")
    assert await bench.reserves("reagent", _plate("reagent"))

    bench.manager.release_reservation(_OUTPUT)

    assert bench.manager.promised_to(_OUTPUT) is None
    assert await bench.reserves("next-sample", _plate("next"))


@pytest.mark.asyncio
async def test_the_plate_still_standing_on_a_promised_location_may_hold_it_until_it_leaves() -> None:
    bench = _Bench()
    leaving = _plate("leaving")
    bench.output.resource.initialize_labware(leaving)
    bench.manager.promise(_OUTPUT, "reagent")

    assert await bench.reserves("leaving-sample", leaving)


@pytest.mark.asyncio
async def test_a_placement_wait_that_is_given_up_takes_its_promise_with_it() -> None:
    bench = _Bench()
    bench.output.resource.initialize_labware(_plate("in-the-way"))
    waiting = asyncio.ensure_future(bench.handler.acquire_placement_reservation("reagent", _plate("reagent"), bench.output))
    await wait_until(
        lambda: "reagent" in bench.coordinator._deadlock_detector._location_waits,
        timeout=5.0, message="the placement wait never registered",
    )
    bench.manager.promise(_OUTPUT, "reagent")

    waiting.cancel()
    await asyncio.gather(waiting, return_exceptions=True)

    assert bench.manager.promised_to(_OUTPUT) is None


@pytest.mark.asyncio
async def test_a_promise_to_a_thread_that_is_gone_is_withdrawn() -> None:
    bench = _Bench()
    bench.manager.promise(_OUTPUT, "reagent")

    bench.coordinator.forget_threads({"reagent"})

    assert await bench.reserves("next-sample", _plate("next"))


@pytest.mark.asyncio
async def test_a_grant_with_no_thread_does_not_touch_a_location_nobody_promised() -> None:
    bench = _Bench()

    await bench.manager.attempt_reservation(_OUTPUT, LocationReservation(bench.output))

    assert bench.manager.get_reservation_at(_OUTPUT) is not None

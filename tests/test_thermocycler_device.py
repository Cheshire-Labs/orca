"""Tests for the orca-core `Thermocycler` device.

Covers the no-driver SDK ctor (a forwarding driver, simulated by the check
run's orca-client) and verifies `run_protocol` reaches the simulator,
plus a getter that unwraps its typed Response into the semantic value.
"""

import pytest

from cheshire_drivers.sims import SimThermocyclerDriver
from cheshire_drivers.thermocycler_models import Protocol, Stage, Step

from orca.devices.thermocycler import Thermocycler
from orca.gateway.remote_drivers import RemoteThermocyclerDriver
from tests.real_device_path import connected, the_real_device_path


def _protocol() -> Protocol:
    return Protocol(
        stages=[
            Stage(
                steps=[Step(temperature=[95.0], hold_seconds=30.0)],
                repeats=2,
            ),
        ],
    )


class TestThermocyclerBuild:
    async def test_the_ctor_builds_a_forwarding_driver_that_a_check_run_simulates(self) -> None:
        device = await connected(Thermocycler("t1"))
        assert device.name == "t1"
        assert isinstance(device.driver, RemoteThermocyclerDriver)
        assert isinstance(the_real_device_path().simulator("t1"), SimThermocyclerDriver)

    def test_kind_is_thermocycler(self) -> None:
        assert Thermocycler.KIND == "thermocycler"


class TestThermocyclerDispatch:
    @pytest.mark.asyncio
    async def test_run_protocol_dispatches_through_sim(self) -> None:
        device = await connected(Thermocycler("t1"))
        assert isinstance(the_real_device_path().simulator("t1"), SimThermocyclerDriver)

        await device.run_protocol(_protocol(), block_max_volume=25.0)

        # The sim tracks total steps (2 repeats x 1 step) after run_protocol.
        assert await device.get_total_step_count() == 2

    @pytest.mark.asyncio
    async def test_getters_unwrap_semantic_values(self) -> None:
        device = await connected(Thermocycler("t1"))

        await device.set_block_temperature([60.0])
        assert await device.get_block_target_temperature() == [60.0]

        await device.open_lid()
        assert await device.get_lid_open() is True
        await device.close_lid()
        assert await device.get_lid_open() is False

        assert isinstance(await device.get_lid_status(), str)
        assert isinstance(await device.get_hold_time(), float)


class TestRemoteThermocyclerDriverInterfaces:
    def test_advertises_ithermocycler(self) -> None:
        assert RemoteThermocyclerDriver.interfaces == frozenset({"IThermocycler"})

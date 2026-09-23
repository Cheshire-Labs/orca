"""Tests for the orca-core `Thermocycler` device.

Covers the no-driver SDK ctor (unbound factory falls back to paired sim
drivers) and verifies `run_protocol` dispatches through the sim driver,
plus a getter that unwraps its typed Response into the semantic value.
"""

import pytest

from cheshire_drivers.sims import SimThermocyclerDriver
from cheshire_drivers.thermocycler_models import Protocol, Stage, Step

from orca.devices.thermocycler import Thermocycler
from orca.gateway.remote_drivers import RemoteThermocyclerDriver


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
    def test_unbound_ctor_pairs_sim_drivers(self) -> None:
        device = Thermocycler("t1")
        assert device.name == "t1"
        assert isinstance(device._sim_manager._live_driver, SimThermocyclerDriver)
        assert isinstance(device._sim_manager._sim_driver, SimThermocyclerDriver)

    def test_kind_is_thermocycler(self) -> None:
        assert Thermocycler.KIND == "thermocycler"


class TestThermocyclerDispatch:
    @pytest.mark.asyncio
    async def test_run_protocol_dispatches_through_sim(self) -> None:
        device = Thermocycler("t1")
        # PURE_SIM (unseeded default) routes `.driver` to the sim slot.
        assert isinstance(device.driver, SimThermocyclerDriver)

        await device.run_protocol(_protocol(), block_max_volume=25.0)

        # The sim tracks total steps (2 repeats x 1 step) after run_protocol.
        assert await device.get_total_step_count() == 2

    @pytest.mark.asyncio
    async def test_getters_unwrap_semantic_values(self) -> None:
        device = Thermocycler("t1")

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

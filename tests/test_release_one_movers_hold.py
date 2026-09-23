"""Freeing one mover the record says is holding a plate.

An abort taken while a plate is genuinely in the jaws leaves a real hold, and a
mover holding one refuses every later pick with `MoverAlreadyHoldingError`. The
only built-in clear was `reset_labware_state`, reachable through
`clear_all_labware`, which wipes every other labware in the runtime as well.

The mover is what the refusal names, so the mover is what this verb takes: the
operator reading the error does not have to translate a labware name into an id
before they can act on it.
"""

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest

from orca.operations._protocol import OperationError
from orca.operations.labware import ReleaseMoverHoldOperation
from orca.operations.labware_models import ReleaseMoverHoldRequest
from orca.resource_models.device_error import SlotOccupiedError
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.transporter_base import TransporterBase
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.runtime_interface import (
    ActiveExecutionRefusedError,
    MoverHoldRelease,
    MoverHoldsNothingError,
)
from orca.runtime.system_runtime import SystemRuntime
from tests.test_system_runtime import _build_simple_system


@pytest.fixture
async def runtime() -> AsyncGenerator[SystemRuntime, None]:
    system, _ = await _build_simple_system()
    rt = SystemRuntime(system, labware_store=InMemoryLabwareStore())
    await rt.start()
    try:
        yield rt
    finally:
        await rt.shutdown()


async def _plate_in_the_jaws(
    rt: SystemRuntime,
) -> tuple[TransporterBase, LabwareInstance]:
    """Put a plate in a mover's jaws the way an interrupted move leaves one."""
    mover = rt._system.movers[0]
    instance = LabwareInstance("plate_96", "96_well")
    rt._system.add_labware(instance)
    await rt.labware._store.register(instance)
    await mover.gripper_location.place_labware(instance)
    mover._picked_from = "pad2"
    rt.labware._location_service.update(instance, mover.gripper_location)
    return mover, instance


class TestReleasingAHold:
    async def test_a_stated_position_frees_the_jaws(self, runtime: SystemRuntime) -> None:
        mover, instance = await _plate_in_the_jaws(runtime)

        result = await runtime.labware.release_mover_hold(
            mover.name, "pad1", reason="taken out by hand", confirm=True,
        )

        assert mover.labware is None, "the jaws must read empty after the release"
        assert mover.picked_from_position_id is None, (
            "empty jaws must not still name where the plate was picked from"
        )
        assert result.labware_id == instance.id
        assert result.released_to == "pad1"
        assert result.discharged is False
        assert runtime._system.system_map.get_location("pad1").labware is instance

    async def test_no_position_discharges_the_labware(self, runtime: SystemRuntime) -> None:
        """The answer when the jaws are empty and the record is wrong: there is
        nowhere to say the plate is, because there is no plate."""
        mover, instance = await _plate_in_the_jaws(runtime)

        result = await runtime.labware.release_mover_hold(
            mover.name, reason="jaws were empty", confirm=True,
        )

        assert mover.labware is None
        assert mover.picked_from_position_id is None
        assert result.discharged is True
        assert result.released_to is None
        assert not [lw for lw in runtime._system.labwares if lw.id == instance.id]

    async def test_a_mover_holding_nothing_says_so(self, runtime: SystemRuntime) -> None:
        """Its own refusal, because nothing is wrong: the jaws were already
        free and whatever blocked the operator was something else."""
        mover = runtime._system.movers[0]

        with pytest.raises(MoverHoldsNothingError) as exc_info:
            await runtime.labware.release_mover_hold(
                mover.name, reason="nothing to do", confirm=True,
            )

        assert exc_info.value.mover_name == mover.name

    async def test_an_unknown_mover_lists_the_ones_there_are(
        self, runtime: SystemRuntime,
    ) -> None:
        with pytest.raises(KeyError) as exc_info:
            await runtime.labware.release_mover_hold(
                "no_such_arm", reason="typo", confirm=True,
            )

        assert "no_such_arm" in str(exc_info.value)
        assert runtime._system.movers[0].name in str(exc_info.value)

    async def test_the_jaws_are_not_a_place_the_plate_can_be(
        self, runtime: SystemRuntime,
    ) -> None:
        """Placing onto the position it already occupies is a no-op, so this
        would have reported a release while the hold stood."""
        mover, instance = await _plate_in_the_jaws(runtime)

        with pytest.raises(ValueError, match="not a place a plate can be"):
            await runtime.labware.release_mover_hold(
                mover.name, mover.gripper_location.position_id,
                reason="the plate is in the jaws", confirm=True,
            )

        assert mover.labware is instance, "a refused release must keep the hold"

    async def test_an_occupied_target_refuses_and_keeps_the_hold(
        self, runtime: SystemRuntime,
    ) -> None:
        """A refused placement must not half-release: the plate is still in the
        jaws, and the record has to keep saying so."""
        mover, _instance = await _plate_in_the_jaws(runtime)
        occupant = LabwareInstance("plate_96", "96_well")
        runtime._system.system_map.get_location("pad1").initialize_labware(occupant)

        with pytest.raises(SlotOccupiedError):
            await runtime.labware.release_mover_hold(
                mover.name, "pad1", reason="wrong pad", confirm=True,
            )

        assert mover.labware is not None


class TestTheDischargeRefusalIsTheSameOne:
    async def test_a_live_thread_carrying_it_refuses_without_force(
        self, runtime: SystemRuntime,
    ) -> None:
        """Discharging drops the labware out of the world, which strands a
        thread still carrying it. Stating a position does not, so only this
        path carries the refusal."""
        mover, instance = await _plate_in_the_jaws(runtime)
        original = runtime.labware._has_active_thread_for
        runtime.labware._has_active_thread_for = lambda lw: lw is instance

        try:
            with pytest.raises(ActiveExecutionRefusedError):
                await runtime.labware.release_mover_hold(
                    mover.name, reason="jaws were empty", confirm=True,
                )

            result = await runtime.labware.release_mover_hold(
                mover.name, reason="jaws were empty", force=True, confirm=True,
            )
            assert result.discharged is True
        finally:
            runtime.labware._has_active_thread_for = original

    async def test_stating_a_position_does_not_refuse_on_a_carrier(
        self, runtime: SystemRuntime,
    ) -> None:
        mover, instance = await _plate_in_the_jaws(runtime)
        original = runtime.labware._has_active_thread_for
        runtime.labware._has_active_thread_for = lambda lw: lw is instance

        try:
            result = await runtime.labware.release_mover_hold(
                mover.name, "pad1", reason="taken out by hand", confirm=True,
            )
            assert result.released_to == "pad1"
        finally:
            runtime.labware._has_active_thread_for = original


class TestWhatTheOperatorReads:
    """The Operation is what REST, MCP and the CLI all raise through, so its
    message is the sentence the operator actually sees."""

    async def test_a_mover_holding_nothing_reads_as_a_sentence(self) -> None:
        """`str()` on a KeyError wraps its message in quotes, so a refusal
        built on one arrives at the operator inside stray quotation marks."""
        runtime = MagicMock()
        runtime.labware.release_mover_hold = AsyncMock(
            side_effect=MoverHoldsNothingError("pf400_1"))
        op = ReleaseMoverHoldOperation(runtime=runtime)

        with pytest.raises(OperationError) as exc_info:
            await op.run(ReleaseMoverHoldRequest(
                mover_name="pf400_1", reason="nothing to do",
            ))

        error = exc_info.value
        assert error.wire_code == "mover_holds_nothing"
        assert error.message.startswith("The record has"), error.message

    async def test_a_release_comes_back_on_the_wire(self) -> None:
        runtime = MagicMock()
        runtime.labware.release_mover_hold = AsyncMock(return_value=MoverHoldRelease(
            mover_name="pf400_1", labware_id="lw-1", labware_name="plate_1",
            released_to="pad1", discharged=False,
        ))
        op = ReleaseMoverHoldOperation(runtime=runtime)

        response = await op.run(ReleaseMoverHoldRequest(
            mover_name="pf400_1", to_location="pad1", reason="taken out by hand",
        ))

        assert response.labware_id == "lw-1"
        assert response.released_to == "pad1"
        assert response.discharged is False

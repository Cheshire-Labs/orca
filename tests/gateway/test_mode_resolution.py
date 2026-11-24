"""One owner for "which world is this device in".

The sim/live choice is made twice, in two processes: orca-core picks
sim-driver-vs-wire, and the on-prem device bridge picks Sim-backend-vs-real off
the `effective_mode` stamped on the command. While those two answers came from
two separate pieces of code they drifted, and a device set to sim could
still move a real instrument. These pin that they come from one place.

Every resolution combines a BASE mode with the device's sim or live setting. The
suite-wide conftest seeds `current_run_mode`, so the cases about a caller's own
base run in a fresh `contextvars.Context`. That is the path with no submission
under it, and it is the one an operator command takes.
"""

from unittest.mock import MagicMock

import pytest

from orca.gateway.controller.exceptions import ModeUnresolvableError
from orca.gateway.mode_resolution import mode_of, resolve_device_mode
from orca.resource_models.resources import IResource
from orca.modes.device_modes import DeviceMode
from orca.modes.run_modes import WorkflowRunMode, current_run_mode
from tests.gateway.mode_doubles import DeclaredDevice, unseeded_await


class _Undeclared(IResource):
    """A gateway-only resource the topology never declared."""

    @property
    def name(self) -> str:
        return "undeclared"


@pytest.mark.asyncio
async def test_an_operator_command_reaches_a_device_set_to_live():
    """The whole point of `when_unseeded=LIVE`: a bench device set to live gets
    the real instrument, not the metadata fallback that the gateway controller
    refuses."""
    device = DeclaredDevice(DeviceMode.LIVE)

    assert await unseeded_await(
        mode_of(device, when_unseeded=WorkflowRunMode.LIVE)
    ) is WorkflowRunMode.LIVE


@pytest.mark.asyncio
async def test_a_device_set_to_sim_is_never_dispatched_live():
    """The sim setting is how a device is kept off the hardware, and it has to
    hold against the caller that most wants the hardware."""
    sim = DeclaredDevice(DeviceMode.SIM)

    assert await unseeded_await(
        mode_of(sim, when_unseeded=WorkflowRunMode.LIVE)
    ) is WorkflowRunMode.SIM


@pytest.mark.asyncio
async def test_a_device_nobody_has_set_is_sim_to_an_operator_command():
    assert await unseeded_await(
        mode_of(DeclaredDevice(), when_unseeded=WorkflowRunMode.LIVE)
    ) is WorkflowRunMode.SIM


@pytest.mark.asyncio
async def test_outside_a_run_each_device_runs_as_its_own_setting():
    """A check run is a whole run in its own runtime, so nothing outside a run resolves to CHECK_RUN."""
    assert await unseeded_await(
        mode_of(DeclaredDevice(DeviceMode.LIVE))
    ) is WorkflowRunMode.LIVE
    assert await unseeded_await(
        mode_of(DeclaredDevice(DeviceMode.SIM))
    ) is WorkflowRunMode.SIM


@pytest.mark.asyncio
async def test_the_seeded_submission_mode_wins_over_the_callers_base():
    """Inside an execution there IS a base, so no caller fallback applies."""
    token = current_run_mode.set(WorkflowRunMode.SIM)
    try:
        assert await mode_of(
            DeclaredDevice(DeviceMode.LIVE), when_unseeded=WorkflowRunMode.LIVE
        ) is WorkflowRunMode.SIM
        assert await mode_of(
            _Undeclared(), when_unseeded=WorkflowRunMode.LIVE
        ) is WorkflowRunMode.SIM
    finally:
        current_run_mode.reset(token)


@pytest.mark.asyncio
async def test_an_undeclared_device_takes_the_callers_base_unchanged():
    """Nothing to combine with, so the caller's base is the whole answer."""
    assert await unseeded_await(
        mode_of(_Undeclared(), when_unseeded=WorkflowRunMode.LIVE)
    ) is WorkflowRunMode.LIVE
    assert await unseeded_await(
        mode_of(None, when_unseeded=WorkflowRunMode.CHECK_RUN)
    ) is WorkflowRunMode.CHECK_RUN


@pytest.mark.asyncio
async def test_only_a_real_mode_aware_resource_gets_to_answer():
    """The check is nominal, not "has an `effective_mode` attribute". A duck
    check passes any test double and puts its mock attribute on the wire as the
    mode, and it does that on some Python versions and not others."""
    stub = MagicMock()

    assert await unseeded_await(
        mode_of(stub, when_unseeded=WorkflowRunMode.LIVE)
    ) is WorkflowRunMode.LIVE


@pytest.mark.asyncio
async def test_an_unknown_name_in_a_readable_system_takes_the_callers_base():
    """A gateway-only device the topology never declared still has to dispatch.
    It has no setting to lose, so the caller's base stands."""
    system = MagicMock()
    system.has_resource.return_value = False

    assert await unseeded_await(
        resolve_device_mode(
            system, "ghost", when_unseeded=WorkflowRunMode.LIVE,
        )
    ) is WorkflowRunMode.LIVE
    system.get_resource.assert_not_called()


@pytest.mark.asyncio
async def test_no_system_at_all_is_refused_rather_than_answered():
    """The two look alike and are not: a readable system that omits a device
    has told us it holds no setting for it, while an absent one has told us
    nothing. Treating the second like the first is how a device set to sim gets
    driven live while a rebuild is broken."""
    with pytest.raises(ModeUnresolvableError) as exc:
        await unseeded_await(
            resolve_device_mode(
                None, "pf400_1", when_unseeded=WorkflowRunMode.LIVE,
            )
        )

    assert "pf400_1" in str(exc.value)


@pytest.mark.asyncio
async def test_a_name_resolves_through_the_same_owner_an_object_would():
    """`resolve_device_mode` is `mode_of` with a lookup in front, so the two
    cannot disagree about the same device."""
    declared = DeclaredDevice(DeviceMode.SIM)
    system = MagicMock()
    system.has_resource.return_value = True
    system.get_resource.return_value = declared

    assert await unseeded_await(
        resolve_device_mode(
            system, "dev", when_unseeded=WorkflowRunMode.LIVE,
        )
    ) is await unseeded_await(
        mode_of(declared, when_unseeded=WorkflowRunMode.LIVE)
    )

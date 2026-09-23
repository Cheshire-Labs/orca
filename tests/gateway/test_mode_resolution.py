"""One owner for "which world is this device in".

The sim/live choice is made twice, in two processes: orca-core picks
sim-driver-vs-wire, and the on-prem device bridge picks Sim-backend-vs-real off
the `effective_mode` stamped on the command. While those two answers came from
two separate pieces of code they drifted, and a device declared DEVICE_SIM could
still move a real instrument. These pin that they come from one place.

Every resolution combines a BASE mode with the device's topology override. The
suite-wide conftest seeds `current_run_mode`, so the cases about a caller's own
base run in a fresh `contextvars.Context`. That is the path with no submission
under it, and it is the one an operator command takes.
"""

from unittest.mock import MagicMock

import pytest

from orca.gateway.controller.exceptions import ModeUnresolvableError
from orca.gateway.mode_resolution import mode_of, resolve_device_mode
from orca.resource_models.resources import IResource
from orca.runtime.run_modes import WorkflowRunMode, current_run_mode
from tests.gateway.mode_doubles import DeclaredDevice, unseeded


class _Undeclared(IResource):
    """A gateway-only resource the topology never declared."""

    @property
    def name(self) -> str:
        return "undeclared"


def test_an_operator_command_reaches_a_declared_device_with_no_override():
    """The whole point of `when_unseeded=LIVE`: a bench device the topology
    declares but leaves un-overridden gets the real instrument, not the
    metadata fallback that the gateway controller refuses."""
    device = DeclaredDevice()

    assert unseeded(
        lambda: mode_of(device, when_unseeded=WorkflowRunMode.LIVE)
    ) is WorkflowRunMode.LIVE


def test_a_device_declaring_sim_is_never_dispatched_live():
    """Declaring an override is how a device is kept off the hardware, and it
    has to hold against the caller that most wants the hardware."""
    device_sim = DeclaredDevice(WorkflowRunMode.DEVICE_SIM)
    pure_sim = DeclaredDevice(WorkflowRunMode.PURE_SIM)

    assert unseeded(
        lambda: mode_of(device_sim, when_unseeded=WorkflowRunMode.LIVE)
    ) is WorkflowRunMode.DEVICE_SIM
    assert unseeded(
        lambda: mode_of(pure_sim, when_unseeded=WorkflowRunMode.LIVE)
    ) is WorkflowRunMode.PURE_SIM


def test_a_metadata_read_stays_off_the_hardware():
    """The other caller: reading a device must not dispatch anywhere, so its
    base keeps even a plain device sim-side."""
    assert unseeded(
        lambda: mode_of(DeclaredDevice())
    ) is WorkflowRunMode.PURE_SIM
    assert unseeded(
        lambda: mode_of(DeclaredDevice(WorkflowRunMode.DEVICE_SIM))
    ) is WorkflowRunMode.PURE_SIM


def test_the_seeded_submission_mode_wins_over_the_callers_base():
    """Inside an execution there IS a base, so no caller fallback applies."""
    token = current_run_mode.set(WorkflowRunMode.DEVICE_SIM)
    try:
        assert mode_of(
            DeclaredDevice(), when_unseeded=WorkflowRunMode.LIVE
        ) is WorkflowRunMode.DEVICE_SIM
        assert mode_of(
            _Undeclared(), when_unseeded=WorkflowRunMode.LIVE
        ) is WorkflowRunMode.DEVICE_SIM
    finally:
        current_run_mode.reset(token)


def test_an_undeclared_device_takes_the_callers_base_unchanged():
    """Nothing to combine with, so the caller's base is the whole answer."""
    assert unseeded(
        lambda: mode_of(_Undeclared(), when_unseeded=WorkflowRunMode.LIVE)
    ) is WorkflowRunMode.LIVE
    assert unseeded(
        lambda: mode_of(None, when_unseeded=WorkflowRunMode.PURE_SIM)
    ) is WorkflowRunMode.PURE_SIM


def test_only_a_real_mode_aware_resource_gets_to_answer():
    """The check is nominal, not "has an `effective_mode` attribute". A duck
    check passes any test double and puts its mock attribute on the wire as the
    mode, and it does that on some Python versions and not others."""
    stub = MagicMock()

    assert unseeded(
        lambda: mode_of(stub, when_unseeded=WorkflowRunMode.LIVE)
    ) is WorkflowRunMode.LIVE


def test_an_unknown_name_in_a_readable_system_takes_the_callers_base():
    """A gateway-only device the topology never declared still has to dispatch.
    There is no override to lose, so nothing is being overridden."""
    system = MagicMock()
    system.has_resource.return_value = False

    assert unseeded(
        lambda: resolve_device_mode(
            system, "ghost", when_unseeded=WorkflowRunMode.LIVE,
        )
    ) is WorkflowRunMode.LIVE
    system.get_resource.assert_not_called()


def test_no_system_at_all_is_refused_rather_than_answered():
    """The two look alike and are not: a readable system that omits a device
    has told us there is no override, while an absent one has told us nothing.
    Treating the second like the first is how a device declared DEVICE_SIM gets
    driven live while a rebuild is broken."""
    with pytest.raises(ModeUnresolvableError) as exc:
        unseeded(
            lambda: resolve_device_mode(
                None, "pf400_1", when_unseeded=WorkflowRunMode.LIVE,
            )
        )

    assert "pf400_1" in str(exc.value)


def test_a_name_resolves_through_the_same_owner_an_object_would():
    """`resolve_device_mode` is `mode_of` with a lookup in front, so the two
    cannot disagree about the same device."""
    declared = DeclaredDevice(WorkflowRunMode.DEVICE_SIM)
    system = MagicMock()
    system.has_resource.return_value = True
    system.get_resource.return_value = declared

    assert unseeded(
        lambda: resolve_device_mode(
            system, "dev", when_unseeded=WorkflowRunMode.LIVE,
        )
    ) is unseeded(
        lambda: mode_of(declared, when_unseeded=WorkflowRunMode.LIVE)
    )

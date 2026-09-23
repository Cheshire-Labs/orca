"""Round 5 S2: per-observer dispatch timeout in ``Location._fan_out``.

The handoff's wedge symptom (``labware_discharge`` hangs ~4 minutes
during active execution) is consistent with a Transporter observer's
wire op to orca-client hanging while orca-client is busy executing
a long-running command. ``asyncio.gather`` over all observers means a
single hung observer wedges the entire fan-out, which holds the
dispose path open until the MCP client (Claude Desktop) gives up at
its 4-minute default.

This test pins the post-fix contract: a hung observer is bounded by
``_FAN_OUT_OBSERVER_TIMEOUT`` (default 30s, but the test patches it
down so the test runs fast); fan-out completes after the timeout
elapses; other observers fire normally; the location's state
mutation continues.
"""

import asyncio
from unittest.mock import Mock

import pytest

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import ILabwareLocationObserver, LabwareLocationEvent, Location
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models import location as location_module


class _HangingObserver(ILabwareLocationObserver):
    """Implements the labware-location observer protocol but never returns.

    Stands in for the worst-case wire op (Transporter ``ensure_seeded``
    against a busy orca-client) that the timeout exists to bound.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def notify_labware_location_change(
        self, event: str, location: Location, labware: LabwareInstance,
    ) -> None:
        self.calls += 1
        await asyncio.Event().wait()  # never set


class _FastObserver(ILabwareLocationObserver):
    """Returns immediately; pins that the timeout on a sibling does not
    cancel observers that complete normally."""

    def __init__(self) -> None:
        self.calls = 0

    async def notify_labware_location_change(
        self, event: str, location: Location, labware: LabwareInstance,
    ) -> None:
        self.calls += 1


@pytest.fixture
def short_timeout(monkeypatch):
    """Trim the 30s production default to 100ms so the test stays fast."""
    monkeypatch.setattr(location_module, "_FAN_OUT_OBSERVER_TIMEOUT", 0.1)


@pytest.mark.asyncio
async def test_fan_out_completes_when_one_observer_hangs(short_timeout) -> None:
    """Round 5 S2: a single hung observer cannot wedge fan-out forever.

    Pre-fix, ``asyncio.gather`` would await all observers indefinitely;
    one hung observer (e.g. a Transporter wire op to a busy orca-client)
    held the whole dispose path open until MCP-remote's 4-minute client
    timeout. Post-fix, each observer dispatch is wrapped by
    ``_FAN_OUT_OBSERVER_TIMEOUT``; the hung observer is logged-and-skipped
    and ``_fan_out`` returns.
    """
    pad = Location("pad_1", PlatePad("pad_1"))
    hanging = _HangingObserver()
    fast = _FastObserver()
    pad.add_observer(hanging)
    pad.add_observer(fast)
    plate = LabwareInstance("plate", "plate")

    # The fan_out call should COMPLETE despite the hanging observer.
    await asyncio.wait_for(pad._fan_out(LabwareLocationEvent.PICKED, plate), timeout=2.0)

    assert hanging.calls == 1  # the dispatch was attempted
    assert fast.calls == 1     # the sibling fired normally


@pytest.mark.asyncio
async def test_fan_out_dispose_completes_under_hung_observer(short_timeout) -> None:
    """End-to-end through ``dispose_labware``: the wedge symptom path.

    ``dispose_labware`` calls ``_fan_out("picked", labware)`` then
    notifies ``_availability_condition``. Pre-fix a hung observer
    blocked the fan_out and the availability notify never fired,
    leaving waiting threads parked forever. Post-fix the dispose
    completes after the per-observer budget, the condition notifies,
    and downstream waiters can resume.
    """
    pad = Location("pad_1", PlatePad("pad_1"))
    plate = LabwareInstance("plate", "plate")
    pad.initialize_labware(plate)
    hanging = _HangingObserver()
    pad.add_observer(hanging)

    await asyncio.wait_for(pad.dispose_labware(plate), timeout=2.0)

    # Dispose succeeded -> resource cleared -> location reports empty.
    assert pad.labware is None


@pytest.mark.asyncio
async def test_fan_out_no_observers_is_fast_noop(monkeypatch) -> None:
    """No observers -> ``_fan_out`` returns without entering the per-observer
    timeout path (regression guard for the ``if not observers: return`` early
    exit).

    Proven structurally, not by a wall-clock band: with the per-observer budget
    set absurdly high, a true no-op still returns immediately, while a
    regression that awaited the budget even with zero observers would exceed the
    generous ceiling below.
    """
    monkeypatch.setattr(location_module, "_FAN_OUT_OBSERVER_TIMEOUT", 3600.0)
    pad = Location("pad_1", PlatePad("pad_1"))
    plate = LabwareInstance("plate", "plate")

    await asyncio.wait_for(
        pad._fan_out(LabwareLocationEvent.PICKED, plate), timeout=1.0
    )


@pytest.mark.asyncio
async def test_fan_out_only_fast_observers_does_not_block(short_timeout) -> None:
    """Multiple fast observers all complete in parallel; no timeout fires."""
    pad = Location("pad_1", PlatePad("pad_1"))
    plate = LabwareInstance("plate", "plate")
    observers = [_FastObserver() for _ in range(5)]
    for obs in observers:
        pad.add_observer(obs)

    await pad._fan_out(LabwareLocationEvent.PLACED, plate)

    for obs in observers:
        assert obs.calls == 1

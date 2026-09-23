"""Tests for the per-command timeout source.

DeviceCommandClock replaces the per-device-kind ``DEFAULT_TIMEOUTS`` table
with a per-command lookup against the driver's advertised
``MethodInfo.duration``.
"""

from datetime import datetime

import pytest

from cheshire_drivers.command_timings import CommandTiming
from cheshire_drivers.driver_introspection import MethodInfo

from orca.gateway.controller.command_clock import DeviceCommandClock
from orca.gateway.registry.snapshot import DeviceSnapshot


def _snapshot(methods: dict[str, MethodInfo]) -> DeviceSnapshot:
    """Build a DeviceSnapshot with the given methods dict.

    Other DeviceSnapshot fields are filled with the cheapest valid values
    so the test stays focused on the timeout-source behavior.
    """
    return DeviceSnapshot(
        name="dev1",
        type="shaker",
        interfaces=["IShaker"],
        capabilities=[],
        provides_state=False,
        methods=methods,
        site="boston",
        lab="molbio",
        workcell=None,
        status="ready",
        last_seen=datetime.utcnow(),
    )


class TestDeviceCommandClock:
    def test_returns_driver_advertised_max_when_present(self) -> None:
        clock = DeviceCommandClock()
        snapshot = _snapshot({
            "shake": MethodInfo(
                kind="method",
                params={},
                duration=CommandTiming(typical_seconds=60.0, max_seconds=7200.0),
            ),
        })

        assert clock.timeout_for(snapshot, "shake") == 7200.0

    def test_falls_back_when_command_not_in_methods(self) -> None:
        clock = DeviceCommandClock()
        snapshot = _snapshot({})

        assert clock.timeout_for(snapshot, "shake") == clock.fallback_seconds

    def test_falls_back_when_duration_is_none(self) -> None:
        """Pre-cheshire-drivers-#6 orca-clients advertise MethodInfo without
        a duration. The clock must fall back rather than crash on ``None``."""
        clock = DeviceCommandClock()
        snapshot = _snapshot({
            "shake": MethodInfo(kind="method", params={}, duration=None),
        })

        assert clock.timeout_for(snapshot, "shake") == clock.fallback_seconds

    def test_custom_fallback_honored(self) -> None:
        clock = DeviceCommandClock(fallback_seconds=120.0)
        snapshot = _snapshot({})

        assert clock.fallback_seconds == 120.0
        assert clock.timeout_for(snapshot, "shake") == 120.0

    def test_zero_or_negative_fallback_rejected(self) -> None:
        with pytest.raises(ValueError):
            DeviceCommandClock(fallback_seconds=0.0)
        with pytest.raises(ValueError):
            DeviceCommandClock(fallback_seconds=-1.0)

    def test_per_command_resolution_within_same_device(self) -> None:
        """A shaker's ``shake`` and its ``stop`` get wildly different
        timeouts when both advertise durations. The whole point of the
        refactor is that a single per-device default cannot express this."""
        clock = DeviceCommandClock()
        snapshot = _snapshot({
            "shake": MethodInfo(
                kind="method",
                params={},
                duration=CommandTiming(typical_seconds=60.0, max_seconds=7200.0),
            ),
            "stop": MethodInfo(
                kind="method",
                params={},
                duration=CommandTiming(typical_seconds=2.0, max_seconds=10.0),
            ),
        })

        assert clock.timeout_for(snapshot, "shake") == 7200.0
        assert clock.timeout_for(snapshot, "stop") == 10.0

    def test_default_fallback_is_conservative(self) -> None:
        """The default fallback must outlast typical operator-facing
        operations so a pre-upgrade orca-client doesn't immediately
        time out commands the driver supports."""
        assert DeviceCommandClock.DEFAULT_FALLBACK_SECONDS >= 300.0

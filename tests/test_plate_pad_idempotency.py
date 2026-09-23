"""PlatePad.initialize_labware idempotent on same instance.

Reuse-bind passes an already-resident LabwareInstance back through the
auto-spawn path, which calls `start_location.initialize_labware(...)`.
The earlier unconditional `DeviceBusyError` raise tripped this no-op
case. Same-instance now returns early; different-instance still raises.
"""

import pytest

from orca.resource_models.device_error import DeviceBusyError
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.plate_pad import PlatePad


class TestPlatePadIdempotency:

    def test_same_instance_initialize_is_noop(self) -> None:
        pad = PlatePad("pad_1")
        labware = LabwareInstance("plate_x", "96_well")

        pad.initialize_labware(labware)
        # Second call with the SAME instance must not raise; pad's labware
        # ref unchanged.
        pad.initialize_labware(labware)
        assert pad.labware is labware

    def test_different_instance_initialize_raises(self) -> None:
        pad = PlatePad("pad_1")
        first = LabwareInstance("plate_x", "96_well")
        second = LabwareInstance("plate_x", "96_well")

        pad.initialize_labware(first)
        with pytest.raises(DeviceBusyError):
            pad.initialize_labware(second)
        # The original labware remains; failed init did not swap.
        assert pad.labware is first

    def test_initial_initialize_succeeds(self) -> None:
        pad = PlatePad("pad_1")
        labware = LabwareInstance("plate_x", "96_well")

        assert pad.labware is None
        pad.initialize_labware(labware)
        assert pad.labware is labware

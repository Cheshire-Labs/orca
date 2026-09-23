"""A Venus carries the kind label the on-prem device bridge reports for it.

The device bridge accepts a venus driver only on a liquid_handler device, so
that is the type it reports. Any other KIND logs kind drift on every boot and
shows the device under two labels.
"""

from orca.devices.devices import LiquidHandler
from orca.devices.venus import Venus


def test_a_venus_carries_the_liquid_handler_kind() -> None:
    assert Venus.KIND == LiquidHandler.KIND == "liquid_handler"

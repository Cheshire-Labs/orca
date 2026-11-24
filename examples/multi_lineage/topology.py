"""Physical topology for the multi-lineage example.

Layout: start_pad -- station -- reservoir_pad -- reservoir_station -- waste,
connected by one human-style transporter ``robot1``.

``build_topology()`` returns a fresh ``Topology`` on every call for test
isolation -- two runs must never share device objects.
"""

from orca.sdk.devices import LiquidHandlerProtocol, PlatePad, Shaker, Transporter
from orca.sdk.build import Topology


SYSTEM_NAME = "Multi-Lineage"


def build_topology() -> Topology:
    """Build a fresh multi-lineage topology.

    Devices use the no-driver SDK constructor: a bound factory supplies the
    (live, sim) pair, and the sim default applies when none is bound.
    """
    # The mix converges two labware (sample + reservoir), so its device needs one
    # position per input; a protocol-driven liquid handler declares them via site_names.
    station = LiquidHandlerProtocol(
        "station", site_names=["sample-site", "reservoir-site"],
    )
    reservoir_station = Shaker("reservoir_station")

    start_pad = PlatePad("start_pad")
    reservoir_pad = PlatePad("reservoir_pad")
    waste = PlatePad("waste")

    robot1 = Transporter("robot1")

    return Topology(
        locations={
            "start_pad": start_pad,
            "station": station,
            "reservoir_pad": reservoir_pad,
            "reservoir_station": reservoir_station,
            "waste": waste,
        },
        transporters=[robot1],
    )

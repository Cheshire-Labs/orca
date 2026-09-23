"""Physical topology for the multi-lineage example.

Layout: start_pad -- station -- reservoir_pad -- reservoir_station -- waste,
connected by one human-style transporter ``robot1``.

``build_topology()`` returns a fresh ``Topology`` on every call for test
isolation -- two runs must never share device objects.
"""

from typing import List

from cheshire_drivers import (
    AccessConfig,
    CartesianCoordinates,
    Teachpoint,
)

from orca.devices.devices import LiquidHandlerProtocol
from orca.devices.shaker import Shaker
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.runtime.store_factory import IRuntimeStoreFactory
from orca.sdk.build import Topology


def _make_teachpoints(
    names: List[str], access: AccessConfig,
) -> List[Teachpoint]:
    """Cartesian-coordinate teachpoints for sim; one per name."""
    pts: List[Teachpoint] = []
    for i, name in enumerate(names):
        coords = CartesianCoordinates(
            x=float(i * 50 + 100), y=0.0, z=50.0,
            yaw=180.0, pitch=90.0, roll=0.0,
        )
        pts.append(Teachpoint(
            position_id=name, coordinates=coords,
            orientation="right", access=access,
        ))
    return pts


def build_topology(stores: IRuntimeStoreFactory) -> Topology:
    """Build a fresh multi-lineage topology.

    Devices use the no-driver SDK ctor; the active factory (SimDeviceFactory
    by default for standalone, RemoteDeviceFactory under a hosted deployment) supplies the (live,
    sim) pair.
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

    default_vertical = AccessConfig(name="default_vertical", access_type="vertical")
    stores.access_configs(seed=[default_vertical])
    location_names = [
        "start_pad", "station", "reservoir_pad", "reservoir_station", "waste",
    ]
    robot1 = Transporter(
        "robot1",
        teachpoint_store=stores.teachpoints(
            "robot1", seed=_make_teachpoints(location_names, default_vertical),
        ),
    )

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

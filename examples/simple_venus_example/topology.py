"""Physical topology for the Venus example.

One Hamilton Venus liquid handler plus a human transporter for manual
pick/place between plate pads taught in the teachpoints JSON.

Every location a teachpoint names must be declared here: a taught name
resolves to a registered location or a device (unknown names fail loud
at build), so the pads are explicit Topology entries.
"""

from pathlib import Path

from orca.devices.venus import Venus
from orca.driver_management.drivers.human_transfer import HumanTransfer
from orca.resource_models.plate_pad import PlatePad
from orca.runtime.store_factory import IRuntimeStoreFactory
from orca.sdk.build import Topology


def build_topology(stores: IRuntimeStoreFactory) -> Topology:
    """Build a fresh Venus example topology.

    `stores` is unused here because `HumanTransfer` loads its teachpoints
    directly from a JSON file. Real deployments would route the same
    positions through the factory so operator CRUD reaches the registry.
    """
    del stores
    # The plate stamp uses both plates at once, so the Venus has a site for each.
    # Each site has its own teachpoint, so the operator is told which one to use.
    ml_star = Venus("ml_star", site_names=["sample_site", "transfer_site"])
    # Beside this file, not beside the shell: the daemon builds a topology in
    # whatever directory it was started from.
    teachpoints = (
        Path(__file__).parent / "teachpoints" / "human_transfer_teachpoints.json"
    )
    human_transfer = HumanTransfer("human_transfer", str(teachpoints))

    return Topology(
        locations={
            "ml_star_position_1": ml_star,
            "plate_pad_1": PlatePad("plate_pad_1"),
            "plate_pad_2": PlatePad("plate_pad_2"),
            "plate_pad_3": PlatePad("plate_pad_3"),
            "plate_pad_4": PlatePad("plate_pad_4"),
        },
        transporters=[human_transfer],
    )

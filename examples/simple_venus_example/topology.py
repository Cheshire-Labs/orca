"""Physical topology for the Venus example.

One Hamilton Venus liquid handler plus a human transporter for manual
pick/place between plate pads taught in `teachpoints/human_transfer.json`.

Every location a teachpoint names must be declared here: a taught name
resolves to a registered location or a device (unknown names fail loud
at build), so the pads are explicit Topology entries.
"""

from orca.sdk.devices import HumanTransfer, PlatePad, Venus
from orca.sdk.build import Topology


SYSTEM_NAME = "Venus Example"
SYSTEM_DESCRIPTION = "Venus Example System"


def build_topology() -> Topology:
    # The plate stamp uses both plates at once, so the Venus has a site for each.
    # Each site has its own teachpoint, so the operator is told which one to use.
    ml_star = Venus("ml_star", site_names=["sample_site", "transfer_site"])
    human_transfer = HumanTransfer("human_transfer")

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

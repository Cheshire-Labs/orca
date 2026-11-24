"""Physical topology for the index-plate pairing example.

Layout: sample_pad, a tagging station, two hotel positions that hold index plates, and waste,
connected by one transporter ``robot1``.
"""

from orca.sdk.build import Topology
from orca.sdk.devices import LiquidHandlerProtocol, PlatePad, Transporter


SYSTEM_NAME = "Index Plate Pairing"


def build_topology() -> Topology:
    """A fresh topology on every call, so two runs never share device objects."""
    # Tagging brings a sample plate and its index plate together, so the station needs a position for each.
    tag_station = LiquidHandlerProtocol("tag_station", site_names=["sample-site", "index-site"])
    return Topology(
        locations={
            "sample_pad": PlatePad("sample_pad"),
            "tag_station": tag_station,
            "index_hotel_1": PlatePad("index_hotel_1"),
            "index_hotel_2": PlatePad("index_hotel_2"),
            "waste": PlatePad("waste"),
        },
        transporters=[Transporter("robot1")],
    )

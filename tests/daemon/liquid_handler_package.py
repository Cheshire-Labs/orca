"""A daemon package: the fixture's shaker, arm and pads, plus a liquid handler `lh` whose setup file a test writes."""

import json
from pathlib import Path

from pydantic import JsonValue

from orca.devices.devices import LiquidHandlerProtocol
from orca.sdk.build import Topology
from tests.daemon.daemon_test_fixture_topology import build_topology as build_fixture_topology
from tests.daemon.fixture_package import SIMPLE_WORKFLOW, write_package


def build_topology() -> Topology:
    """The daemon fixture's topology plus `lh`, which the simple workflow does not use."""
    fixture = build_fixture_topology()
    return Topology(
        locations={**fixture.locations, "lh": LiquidHandlerProtocol("lh")},
        transporters=fixture.transporters,
        pools=fixture.pools,
    )


def write_liquid_handler_package(setup: dict[str, JsonValue]) -> Path:
    """Write the package and `devices/lh.json` into the default package folder, where the fixture's arm keeps its teachpoints."""
    root = write_package(topology=f"{__name__}:build_topology", workflows=SIMPLE_WORKFLOW)
    (root / "devices").mkdir(exist_ok=True)
    (root / "devices" / "lh.json").write_text(json.dumps(setup), encoding="utf-8")
    return root

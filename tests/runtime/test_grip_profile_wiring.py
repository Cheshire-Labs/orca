"""A profile an operator writes has to be the profile the arm reads.

The registry is worthless if the runtime binds a different store than the one
the operator surfaces write to: every edit would report success and change
nothing. Slice A shipped exactly that bug against the move defaults, so these
pin the wiring rather than the resolver.
"""

import pytest
from cheshire_drivers.move_parameters import MoveParameterPatch
from cheshire_drivers.teachpoints import AccessConfig, CartesianCoordinates, Teachpoint

from orca.resource_models.transporter import Transporter
from orca.runtime.db import create_memory_engine
from orca.runtime.deployment_registries import build_in_memory_deployment_layer
from orca.runtime.grip_profile_service import GripProfileService
from orca.runtime.sqlite_grip_profile_store import SqliteGripProfileStore
from orca.runtime.system_runtime import SystemRuntime
from tests.test_system_runtime import _build_simple_system


def _teachpoint() -> Teachpoint:
    return Teachpoint(
        position_id="pad_1",
        coordinates=CartesianCoordinates(x=1.0, y=2.0, z=3.0, yaw=0.0, pitch=90.0, roll=-180.0),
        orientation="left",
        access=AccessConfig(
            name="taught_vertical",
            access_type="vertical",
            gripper_offset=20.0,
            vertical_clearance=20.0,
            horizontal_clearance=100.0,
        ),
    )


@pytest.mark.asyncio
async def test_a_started_runtime_binds_every_transporter_to_the_stored_profiles() -> None:
    """A topology that never mentions grip profiles must still read the stored rows.

    Leaving the wiring to the topology author is how a deployment ends up quietly
    gripping every labware at the seed width while an operator edits rows nothing
    ever consults.
    """
    system, _ = await _build_simple_system()
    service = GripProfileService(SqliteGripProfileStore(create_memory_engine()))
    await service.set("costar_96", MoveParameterPatch(resource_width=76.0))

    runtime = SystemRuntime(system, grip_profile_service=service)
    await runtime.start()
    try:
        transporter = system.transporters[0]
        resolved = await transporter.resolve_handling(_teachpoint(), "costar_96")
    finally:
        await runtime.shutdown(confirm=True)

    assert resolved.parameters.resource_width == 76.0
    assert resolved.sources["resource_width"] == "labware"


@pytest.mark.asyncio
async def test_the_daemon_runtime_reads_the_profiles_the_operator_writes() -> None:
    """The daemon holds one store factory precisely so the registry an operator
    edits and the runtime that dispatches never diverge. A runtime that mints its
    own store instead would read an empty table and silently grip at the default
    width, which looks exactly like the feature working.
    """
    factory, registries = build_in_memory_deployment_layer()
    profile = MoveParameterPatch(resource_width=82.0)
    await registries.grip_profiles.apply("tip_box_1000ul", profile, confirm=True)

    assert await factory.grip_profiles().get("tip_box_1000ul") == profile


@pytest.mark.asyncio
async def test_a_transporter_outside_a_runtime_still_answers() -> None:
    """A transporter built for a unit test or an inspected topology has no
    profiles bound. It resolves without the labware layer rather than failing."""
    transporter = Transporter("unbound")

    resolved = await transporter.resolve_handling(_teachpoint(), "costar_96")

    assert resolved.sources["resource_width"] == "seed"

"""Tests for Transporter gripper location tracking."""

from orca.resource_models.gripper_pad import GripperPad
from orca.resource_models.transporter import Transporter


def test_gripper_location_snapshot() -> None:
    """One snapshot pinning every gripper-location property of a fresh Transporter."""
    t = Transporter("robotic_arm")
    loc = t.gripper_location
    assert loc is not None
    assert loc.name == "robotic_arm/gripper"
    assert isinstance(loc.resource, GripperPad)
    assert loc.resource.supports_deadlock_resolution is False

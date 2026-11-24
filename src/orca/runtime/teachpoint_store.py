"""Teachpoint write-time authoring rule.

A teachpoint file names its access config and never copies its fields,
so ``validate_persistable_access`` refuses a teachpoint with inline access fields.
"""

from cheshire_drivers.teachpoints import Teachpoint


def validate_persistable_access(teachpoint: Teachpoint) -> None:
    """Reject teachpoints with inline access fields; raise on violation.

    A teachpoint built with ``access=AccessConfig(name=...)`` is stored as that
    name. Inline ``access_type=...`` / ``gripper_offset=...`` have no name to
    store, and dropping or copying them would let a wrong ``gripper_offset``
    reach the arm. Raises ``ValueError`` naming the fix.
    """
    if teachpoint.access_type is not None and teachpoint.access_config_name is None:
        raise ValueError(
            f"Teachpoint {teachpoint.position_id!r} has access_type="
            f"{teachpoint.access_type!r} but no named AccessConfig. "
            "Teachpoint stores cannot round-trip inline access fields; "
            "construct the teachpoint with access=<AccessConfig> instead."
        )

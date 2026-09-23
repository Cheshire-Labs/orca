"""Teachpoint write-time authoring rule.

The persistent teachpoint stores (orca's ``SqliteTeachpointStore`` and a hosted
deployment's ``PostgresTeachpointStore``) hold raw rows; the ``TeachpointService`` that wraps
either store enforces the authoring rule below at write time:
``validate_persistable_access`` requires a teachpoint to carry a named
``AccessConfig`` rather than inline access fields, so a topology written for one
backend also works against the other.
"""

from cheshire_drivers.teachpoints import Teachpoint


def validate_persistable_access(teachpoint: Teachpoint) -> None:
    """Reject teachpoints with inline access fields; raise on violation.

    A teachpoint constructed with ``access=AccessConfig(name=..., ...)``
    is persistable: the access fields live in a separate AccessConfig row
    and the teachpoint stores the FK name. A teachpoint constructed with
    inline ``access_type=...`` / ``gripper_offset=...`` etc. has no
    AccessConfig name; persistence backends would either have to invent
    one, drop the values silently, or duplicate calibration data.

    Silent calibration drift in a robotics context (a wrong
    ``gripper_offset`` crashes the gripper into the deck) is the kind of
    failure mode we want to catch at write time, not read time. Both the
    source-available SqliteTeachpointStore and a hosted deployment's PostgresTeachpointStore wrap a
    TeachpointService that calls this, so a topology written against either
    backend works against the other.

    Raises ``ValueError`` with a message that points the caller at the
    fix.
    """
    if teachpoint.access_type is not None and teachpoint.access_config_name is None:
        raise ValueError(
            f"Teachpoint {teachpoint.position_id!r} has access_type="
            f"{teachpoint.access_type!r} but no named AccessConfig. "
            "Teachpoint stores cannot round-trip inline access fields; "
            "construct the teachpoint with access=<AccessConfig> instead."
        )

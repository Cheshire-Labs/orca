"""Which commands a device may be sent: its interfaces' commands from the command table, plus its vendor extras.

Per-command timeouts live in ``orca.gateway.controller.command_clock``.
"""

from cheshire_drivers.command_table import COMMANDS


def validate_capability_for_device(
    interfaces_advertised: frozenset[str],
    capabilities_advertised: frozenset[str],
    command: str,
) -> bool:
    """True if one of the device's interfaces declares `command`, or it is one of the device's vendor extras.

    A readable property such as `is_initialized` counts; engine-read metadata such as `name` does not.
    """
    return any(command in COMMANDS.commands_of(name) for name in interfaces_advertised) or (
        command in capabilities_advertised
    )

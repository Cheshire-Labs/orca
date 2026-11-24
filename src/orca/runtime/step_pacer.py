"""The time the engine budgets for a driver command, which its simulator waits out."""

from cheshire_drivers.command_table import DriverKwargs
from cheshire_drivers.command_timings import collect_command_timings

from orca.modes.sim_pacing import nominal_step_seconds


def nominal_seconds_for(driver_cls: type, command: str, kwargs: DriverKwargs) -> float | None:
    """Simulated seconds the engine budgets for one driver command, which its simulator waits out.

    None for a command the driver class does not have.
    """
    method = getattr(driver_cls, command, None)
    if method is None:
        return None
    timing = collect_command_timings(driver_cls).get(command)
    typical = None if timing is None else timing.typical_seconds
    return nominal_step_seconds(method, (), kwargs, typical_seconds=typical)

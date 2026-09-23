"""The sealer's link budget has to outlast the seal cycle it waits out.

The A4S has one wait budget and it bounds both a single response read and the
loop that waits for the seal cycle to finish. This device wrapper used to hardcode
20s, while `seal` is declared to the engine at max=600s, so a normal cycle came
back to the operator as a transport error instead of reaching the engine's
recoverable-timeout decision.
"""

from cheshire_drivers.command_timings import collect_command_timings
from cheshire_drivers.plr import A4SSealerDriver

from orca.driver_management.drivers.a4s_sealer import A4SSealer


def _declared_seal_max() -> float:
    return collect_command_timings(A4SSealerDriver)["seal"].max_seconds


def test_a_sealer_waits_out_the_seal_cycle_the_engine_allows() -> None:
    sealer = A4SSealer(name="sealer_1", port="COM3")

    driver = sealer.live_driver
    assert isinstance(driver, A4SSealerDriver)
    assert driver._backend.timeout >= _declared_seal_max()

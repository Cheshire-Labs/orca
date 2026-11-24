"""System configuration for orca-core runtime behavior.

Read from the deployment package's optional `system_config.json`. A missing file
or setting keeps the default; a misspelled setting is refused.

Physical timing context:
- Plate moves take ~10 seconds
- Location actions can take hours (e.g., 2-hour shaker incubation)
- Devices are occupied for the full action duration
- Reservation contention is normal and can last minutes to hours
- Simulation is fast; hardware is slow. Defaults must work for both.
"""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

SYSTEM_CONFIG_FILE = "system_config.json"


class ReservationConfig(BaseModel):
    """Reservation acquisition retry behavior."""

    model_config = ConfigDict(extra="forbid")

    move_reservation_timeout: float | None = Field(
        default=None,
        description=(
            "Max seconds to wait for a move reservation before raising. "
            "None = wait indefinitely (production default). "
            "On real hardware, devices can be occupied for hours."
        ),
    )

    action_reservation_timeout: float | None = Field(
        default=None,
        description=(
            "Max seconds to wait for an action location reservation. "
            "None = wait indefinitely (production default)."
        ),
    )

    site_vacate_patience: float = Field(
        default=15.0,
        gt=0,
        description=(
            "Seconds a rejected move waits before its collection widens to "
            "include deadlock-resolution pads, when the labware is sitting on "
            "a device-owned working site. Not a timeout: the real targets "
            "stay in the collection and win whenever they free; the pads only "
            "grant while the targets stay blocked. Physically this is 'clear "
            "the shared deck if you cannot move on' - a plate must not occupy "
            "a device other threads need while it waits (possibly hours) for "
            "its own destination. Must stay below the stall detector's "
            "quiescence window (at least 20s: 3 ticks of 10s, the first of which "
            "can land just after the state began) or a stalled-looking system "
            "pauses before the vacate can break the jam."
        ),
    )

    retry_interval: float = Field(
        default=0.5,
        gt=0,
        description=(
            "Retry interval for a rejected reservation waiter. A location has "
            "two gates: the reservation gate and the physical-occupancy gate. "
            "A waiter blocked on the RESERVATION gate wakes the moment one of "
            "its own contended locations is released, so for that gate this is "
            "only a safety cap (bounds a missed wakeup; keeps re-submissions "
            "feeding the cross-tick deadlock detector when a genuine deadlock "
            "yields no releases). A waiter blocked on OCCUPANCY (labware "
            "physically resident; cleared by a pick, which fires no release) "
            "has no wake path yet, so this interval IS its retry cadence. "
            "Must be > 0: at 0 the release-wait returns immediately WITHOUT "
            "yielding (unlike the asyncio.sleep(0) it replaced), turning the "
            "retry loop into a non-yielding hot spin."
        ),
    )


class CoordinationConfig(BaseModel):
    """Thread coordination timeouts."""

    model_config = ConfigDict(extra="forbid")

    co_labware_timeout: float | None = Field(
        default=None,
        description=(
            "Max seconds a thread waits for co-labware to arrive at a shared "
            "action location. None = wait indefinitely (production default): a "
            "co-thread may run a multi-hour action before it can move its "
            "labware. This wait is NOT reservation acquisition, so the "
            "reservation deadlock detector does not see it; a contributor that "
            "dies is surfaced by execution-failure escalation, and a genuinely "
            "wedged wait is recovered by operator stop. When it runs out, the "
            "thread pauses at CO_LABWARE_WAIT; RETRY waits again with the whole timeout."
        ),
    )

    event_timeout: float | None = Field(
        default=None,
        description=(
            "Default max seconds a thread waits for an event (orca.on / orca.branch). "
            "None = wait indefinitely (production default). "
            "Per-step timeout overrides this. When it runs out, the thread pauses at "
            "WAIT_EVENT_TIMEOUT; RETRY waits again with the whole timeout."
        ),
    )


class OrcaConfig(BaseModel):
    """Top-level orca-core configuration: the contents of `system_config.json`."""

    model_config = ConfigDict(extra="forbid")

    reservation: ReservationConfig = Field(default_factory=ReservationConfig)
    coordination: CoordinationConfig = Field(default_factory=CoordinationConfig)

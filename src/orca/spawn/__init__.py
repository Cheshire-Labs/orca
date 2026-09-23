"""SDK pre-rolls for `@orca.thread` spawn / end action sentinels.

Five sentinels carry author intent from `@orca.thread(start=...)` /
`end=...` into the engine's spawn-action dispatch:

Start-side (acquire labware at thread entry):
  - `MANUAL_PLACE` -- operator places labware at the slot. Bare-string
    default. Sim modes auto-fulfill; LIVE mode parks the thread at
    `AWAITING_MANUAL_PLACE` until `labware_register(template, location=X)`
    fires.
  - `DISPENSE` -- stacker / hotel / source backed by `IPlateSource`
    dispenses the next plate physically (via `device.dispense()`) before
    the engine writes the slot.
  - `REUSE_EXISTING` -- bind to whatever labware is already at the
    location, creating a fresh one only on first use. Persists the
    labware across executions so reagent troughs and calibration plates
    can be reused. Routed through `ExecutingWorkflow._resolve_reuse_bind`,
    not a spawn-action strategy.

End-side (release labware at thread completion):
  - `MANUAL_REMOVE` -- operator removes labware from the slot at thread
    end. Bare-string default. Sim modes auto-dispose; LIVE mode parks
    the thread at `AWAITING_MANUAL_REMOVE` until `labware_discharge`
    fires.
  - `LEAVE_IN_PLACE` -- skip auto-dispose when the thread terminates.
    Paired with `REUSE_EXISTING` for deck-resident labware that should
    outlive any single execution.

Used as the second element of `@orca.thread(..., start=("loc", SENTINEL))`
or `end=("loc", SENTINEL)`. Plain string constants instead of strategy
classes.
"""

REUSE_EXISTING = "reuse_existing"
DISPENSE = "dispense"
MANUAL_PLACE = "manual_place"

LEAVE_IN_PLACE = "leave_in_place"
MANUAL_REMOVE = "manual_remove"

_VALID_START_SENTINELS = frozenset({REUSE_EXISTING, DISPENSE, MANUAL_PLACE})
_VALID_END_SENTINELS = frozenset({LEAVE_IN_PLACE, MANUAL_REMOVE})

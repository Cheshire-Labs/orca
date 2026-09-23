from typing import Generic, List, TypeVar

from orca.runtime.run_modes import (
    UNSEEDED_FALLBACK_MODE,
    WorkflowRunMode,
    current_run_mode,
    resolve_effective_mode_for_device,
)

T = TypeVar("T")


class SimulationManager(Generic[T]):
    """Holds the (live_driver, sim_driver) pair for a Device or Transporter.

    Dispatch consults the v3.4 12-row resolver: it combines the per-task
    `current_run_mode` ContextVar with the device's own
    `sim_override` declared at topology construction time. The resolved
    per-device mode picks `sim_driver` for PURE_SIM and `live_driver` for
    DEVICE_SIM / LIVE. DEVICE_SIM runs against the local live driver
    because the sim/live distinction at orca-core dispatch is "do we hit
    the wire?", and both DEVICE_SIM and LIVE do (the gateway swaps
    Sim* / live backends on the orca-client side per
    `CommandMessage.effective_mode`).
    """

    def __init__(
        self,
        live_driver: T,
        sim_driver: T,
        sim_override: WorkflowRunMode | None = None,
    ) -> None:
        self._live_driver = live_driver
        self._sim_driver = sim_driver
        self._sim_override = sim_override

    def mode_under(self, base: WorkflowRunMode) -> WorkflowRunMode:
        """The mode this device dispatches under if `base` is the submission mode.

        THE one place that combines a base mode with the device's topology
        override. Everything that needs to know which world a device is in --
        the driver swap here, the snapshot facades, and the `effective_mode`
        stamped onto every wire command -- resolves through this, so those
        answers cannot drift apart.

        `base` exists because outside an execution there is no submission to
        read: the caller states what it means instead. An override only ever
        ratchets toward sim, so declaring DEVICE_SIM keeps a command off the
        hardware whatever base the caller supplies, and no override can turn a
        sim-side base into a wire command.
        """
        return resolve_effective_mode_for_device(base, self._sim_override).resolved

    @property
    def effective_mode(self) -> WorkflowRunMode:
        """`mode_under` applied to whatever run mode is in force right now.

        Inside an execution that is the submission's mode. Outside one there is
        no base to read, so this falls through to `UNSEEDED_FALLBACK_MODE` and
        answers as a metadata read: never dispatching to hardware by accident.
        A caller that means something else states its own base through
        `mode_under` (see `orca.gateway.mode_resolution.mode_of`).
        """
        return self.mode_under(current_run_mode.get(UNSEEDED_FALLBACK_MODE))

    def driver_under(self, base: WorkflowRunMode) -> T:
        """The driver a dispatch under `base` lands on: `mode_under` applied
        to the slot pick, so mode and driver cannot disagree."""
        if self.mode_under(base) is WorkflowRunMode.PURE_SIM:
            return self._sim_driver
        return self._live_driver

    @property
    def driver(self) -> T:
        """The active driver for the resolved per-device run_mode."""
        return self.driver_under(current_run_mode.get(UNSEEDED_FALLBACK_MODE))

    @property
    def all_drivers(self) -> List[T]:
        """Both managed drivers, regardless of which is currently active.

        Used by callers that must keep both drivers in sync (e.g. priming
        teachpoints) so they observe coherent state regardless of which
        run_mode is active.
        """
        return [self._live_driver, self._sim_driver]

    @property
    def live_driver(self) -> T:
        """The live driver, unconditionally - for metadata-only reads.

        Topology-card builders and introspection routes read driver
        class-level metadata (`interfaces`, `derive_capabilities`,
        `describe_driver`) that describes the deployment's actual
        capability surface. The live driver is the production target;
        the sim driver is a stub that may declare a different interface
        set. Reading through `driver` (the dispatch property) would
        leak the sim driver's surface under an unseeded ContextVar
        because dispatch falls back to PURE_SIM there. Metadata callers
        must use `live_driver` instead so the introspection card matches
        the deployment's intended runtime behavior.
        """
        return self._live_driver

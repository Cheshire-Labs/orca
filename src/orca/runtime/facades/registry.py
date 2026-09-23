"""RegistryFacade: read-only inventory of every internal registry plus a
small set of targeted mutation ops that belong here organizationally
(reservation cancel).

Every list method builds fresh snapshots from live internal objects on
demand. Nothing is cached, so consumers always see current state.

Full topology mutation (add/remove devices, locations, resource pools
mid-run) requires live device-config instantiation which is a separate
design + serialization effort; the facade intentionally does not expose
those surfaces until that work lands.
"""

from typing import Callable, List

from orca.gateway.adhoc import fault_summary
from orca.resource_models.deck_site import DeckSite
from orca.resource_models.devices import Device
from orca.resource_models.transporter_base import TransporterBase
from orca.runtime.danger import DangerLevel, dangerous
from orca.runtime.registries.device_link import DeviceLinkReader
from orca.runtime.run_modes import OPERATOR_DEVICE_WRITE_BASE
from orca.runtime.runtime_interface import IDeviceConnectionSource, IRegistryFacade
from orca.runtime.status_models import (
    DeviceSnapshot,
    LabwareTemplateSnapshot,
    LocationSnapshot,
    MethodTemplateSnapshot,
    ReservationSnapshot,
    ResourcePoolSnapshot,
    SystemInfoSnapshot,
    ThreadTemplateSnapshot,
    MoverSnapshot,
    TransporterSnapshot,
    WorkflowTemplateSnapshot,
)
from orca.system.system_interface import ISystem
from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.workflow_templates import WorkflowTemplate


ReservationLister = Callable[[str], List[ReservationSnapshot]]
ReservationCanceller = Callable[[str, str], None]


class RegistryFacade(IRegistryFacade):
    """Concrete RegistryFacade implementation.

    Reservation listing and cancellation are delegated to caller-supplied
    callables (typically `SystemRuntime.list_reservations` and
    `SystemRuntime.cancel_reservation`) so the facade doesn't need direct
    access to the per-execution ExecutingWorkflow or the global reservation
    coordinator.
    """

    def __init__(
        self,
        system: ISystem,
        list_reservations_fn: ReservationLister,
        cancel_reservation_fn: ReservationCanceller,
        connections: IDeviceConnectionSource,
    ) -> None:
        self._system = system
        self._list_reservations_fn = list_reservations_fn
        self._cancel_reservation_fn = cancel_reservation_fn
        self._links = DeviceLinkReader(connections, system)

    # -- System-level --------------------------------------------------------

    def system_info(self) -> SystemInfoSnapshot:
        # Sim-hierarchy v3.4: per-submission run_mode supersedes a
        # system-wide "is_simulating" flag. SystemInfoSnapshot is now
        # identity-only; operators query effective_mode on per-device or
        # per-submission snapshots.
        return SystemInfoSnapshot(
            name=self._system.name,
            description=self._system.description,
            version=self._system.version,
        )

    # -- Resources -----------------------------------------------------------

    def list_devices(self) -> List[DeviceSnapshot]:
        return [self._device_snapshot(d) for d in self._system.devices]

    def list_transporters(self) -> List[TransporterSnapshot]:
        return [self._transporter_snapshot(t) for t in self._system.transporters]

    def list_movers(self) -> List[MoverSnapshot]:
        return [self._mover_snapshot(m) for m in self._system.movers]

    def list_resource_pools(self) -> List[ResourcePoolSnapshot]:
        snapshots: List[ResourcePoolSnapshot] = []
        for pool in self._system.resource_pools:
            members = pool.resources
            available = sum(
                1 for m in members
                if isinstance(m, Device) and not m.in_use
            )
            snapshots.append(ResourcePoolSnapshot(
                name=pool.name,
                member_names=tuple(m.name for m in members),
                available_count=available,
            ))
        return snapshots

    def list_locations(self) -> List[LocationSnapshot]:
        snapshots: List[LocationSnapshot] = []
        for loc in self._system.locations:
            resource_name = loc.resource.name if loc.resource is not None else None
            # Combine both surfaces so single-slot resources (PlatePad's
            # ``labware``) and multi-slot resources / bridges
            # (``loaded_labware``) and stage-held labware (bridge's
            # ``staged_labware``, exposed via ``labware``) all show up.
            # Pre-Epic-4 the snapshot only looked at Device.all_loaded_labware,
            # which blanked out PlatePads AND operator-registered plates
            # sitting in a bridge's stage. Same shape the deadlock manager
            # uses to enumerate location occupants.
            seen: set[str] = set()
            ordered: list[str] = []
            if loc.labware is not None and loc.labware.id not in seen:
                seen.add(loc.labware.id)
                ordered.append(loc.labware.id)
            for lw in loc.loaded_labware:
                if lw.id not in seen:
                    seen.add(lw.id)
                    ordered.append(lw.id)
            # Discovery surface for site-qualified names (CLI / REST / MCP /
            # KB): the device's owned sites, listed on its mutex row.
            deck_sites = tuple(
                site.position_id
                for site in self._system.system_map.sites_of(loc.position_id)
            )
            snapshots.append(LocationSnapshot(
                name=loc.name,
                resource_name=resource_name,
                loaded_labware_ids=tuple(ordered),
                deck_sites=deck_sites,
            ))
        return snapshots

    # -- Templates -----------------------------------------------------------

    def list_labware_templates(self) -> List[LabwareTemplateSnapshot]:
        return [
            LabwareTemplateSnapshot(
                name=t.name,
                type_name=type(t).__name__,
            )
            for t in self._system.labware_templates
        ]

    def list_method_templates(self) -> List[MethodTemplateSnapshot]:
        return [
            MethodTemplateSnapshot(
                workflow_name=workflow_name,
                name=name,
                failure_policy=template.method_failure_policy,
            )
            for (workflow_name, name), template in self._system.get_method_templates().items()
        ]

    def get_method_template(self, workflow_name: str, name: str) -> MethodTemplate:
        return self._system.get_method_template(workflow_name, name)

    def list_workflow_templates(self) -> List[WorkflowTemplateSnapshot]:
        return [
            WorkflowTemplateSnapshot(
                name=wf.name,
                entry_thread_template_names=tuple(
                    t.name for t in wf.entry_thread_templates
                ),
            )
            for wf in self._system.get_workflow_templates().values()
        ]

    def get_workflow_template(self, name: str) -> WorkflowTemplate:
        return self._system.get_workflow_template(name)

    @dangerous(
        name="workflow_template.add",
        level=DangerLevel.OPERATOR,
        message="Register a workflow template on the runtime. Replaces any "
                "existing template with the same name; cascade drops "
                "bundled methods/threads of the prior template.",
        requires_reason=False,
    )
    async def add_workflow_template(
        self, template: WorkflowTemplate, *,
        source_sha: str | None = None,
        reason: str | None = None,
    ) -> None:
        del reason, source_sha
        await self._add_workflow_template_impl(template)

    async def _add_workflow_template_impl(self, template: WorkflowTemplate) -> None:
        # REPLACE semantics: if name exists, drop the old (cascade) and add
        # the new. Always-replace is the locked design rule (git is the
        # rollback path).
        existing = self._system.get_workflow_templates().get(template.name)
        if existing is not None:
            self._cascade_remove(existing)
        # Use the standalone helper so location resolution + name-collision
        # checks for bundled methods/threads stay in one place.
        from orca.sdk.build import add_workflow_template as _add_wf_to_system
        await _add_wf_to_system(self._system, template)

    @dangerous(
        name="workflow_template.remove",
        level=DangerLevel.CRITICAL,
        message="Remove workflow template {name} from the runtime. Cascade "
                "drops the workflow's bundled methods and threads.",
        requires_reason=True,
    )
    async def remove_workflow_template(
        self, name: str, *,
        reason: str | None = None,
    ) -> None:
        del reason
        existing = self._system.get_workflow_templates().get(name)
        if existing is None:
            raise KeyError(f"Workflow template '{name}' not found")
        self._cascade_remove(existing)

    def _cascade_remove(self, template: WorkflowTemplate) -> None:
        """Remove a workflow + its bundled method/thread templates.

        Bundle membership is the cascade rule: only templates listed in
        `bundled_methods` / `bundled_threads` (decorations that ran inside
        this workflow's build_workflow closure) are dropped. This stops
        a removal from clobbering templates that another workflow happens
        to share by name (with identical object identity that the add
        path treats as a no-op).
        """
        for method_template in template.bundled_methods:
            self._system.remove_method_template(template.name, method_template.name)
        for thread_template in template.bundled_threads:
            self._system.remove_labware_thread_template(template.name, thread_template.name)
        self._system.remove_workflow_template(template.name)

    def list_thread_templates(self) -> List[ThreadTemplateSnapshot]:
        return [
            ThreadTemplateSnapshot(
                workflow_name=workflow_name,
                name=t.name,
                labware_template_name=t.labware_template.name,
                start_position_id=t.start_position_id,
                end_position_ids=tuple(t.end_position_ids),
            )
            for (workflow_name, _name), t in self._system.get_labware_thread_templates().items()
        ]

    # -- Reservations --------------------------------------------------------

    def list_reservations(self, execution_id: str) -> List[ReservationSnapshot]:
        return self._list_reservations_fn(execution_id)

    @dangerous(
        name="reservation.cancel",
        level=DangerLevel.CRITICAL,
        message="Cancel reservation '{reservation_id}' in execution '{execution_id}'. "
                "The owning thread's next action on this location will likely fail, "
                "leaving the thread error-paused for recovery.",
        requires_reason=True,
    )
    async def cancel_reservation(
        self, execution_id: str, reservation_id: str,
        reason: str | None = None,
    ) -> None:
        del reason  # consumed by @dangerous audit; facade ignores body
        self._cancel_reservation_fn(execution_id, reservation_id)

    # -- Snapshot builders ---------------------------------------------------

    def _device_snapshot(self, d: Device) -> DeviceSnapshot:
        # One world per row: the same base the link flags beside it resolve.
        effective_mode = d.mode_under(OPERATOR_DEVICE_WRITE_BASE)
        return DeviceSnapshot(
            name=d.name,
            type_name=type(d).__name__,
            # Sourced through the link reader so this row cannot contradict
            # the registry route's answer for the same device.
            is_initialized=self._links.is_initialized(d.name),
            is_busy=d.in_use,
            effective_mode=effective_mode,
            position_ids=tuple(loc.name for loc in d.locations),
            loaded_labware_ids=d.all_loaded_labware_ids,
            under_external_control=d.under_external_control,
            external_control_hold=d.external_control_hold,
            fault=fault_summary(d.name),
        )

    @staticmethod
    def _mover_snapshot(m: TransporterBase) -> MoverSnapshot:
        held = m.labware
        return MoverSnapshot(
            name=m.name,
            type_name=type(m).__name__,
            is_busy=m.in_use,
            gripper_position_id=m.gripper_location.position_id,
            current_labware_id=held.id if held is not None else None,
            under_external_control=m.under_external_control,
            external_control_hold=m.external_control_hold,
        )

    @staticmethod
    def _transporter_snapshot(t: TransporterBase) -> TransporterSnapshot:
        # Read the attributes directly. These were getattr-with-a-default, which
        # turned a rename into a snapshot that answered "nothing in the jaws"
        # with a plate in them, for as long as nobody checked.
        held = t.labware
        return TransporterSnapshot(
            name=t.name,
            type_name=type(t).__name__,
            is_busy=t.in_use,
            # Empty on purpose: a mover is not mounted at a location the way a
            # device is. It reaches positions through its teachpoints.
            position_ids=(),
            current_labware_id=held.id if held is not None else None,
            under_external_control=t.under_external_control,
            external_control_hold=t.external_control_hold,
        )

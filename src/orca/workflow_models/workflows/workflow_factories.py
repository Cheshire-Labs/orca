from typing import Iterable, Sequence

from orca.resource_models.labware import LabwareInstance
from orca.state.contents import LabwareContentsLedger
from orca.resource_models.sharing import GroupSharing, SubmissionBatching
from orca.workflow_models.labware_group import LabwareGroup
from orca.modes.run_modes import WorkflowRunMode
from orca.workflow_models.submission import ResolvedAcquisition
from orca.workflow_models.submission_modes import BatchMode
from orca.workflow_models.interfaces import IMethod
from orca.workflow_models.labware_threads.labware_thread import (
    LabwareThreadInstance,
)
from orca.workflow_models.method import ExecutingMethod, MethodInstance
from orca.workflow_models.method_template import IMethodTemplate, MethodTemplate
from orca.workflow_models.thread_template import ThreadTemplate, threads_a_group_names
from orca.workflow_models.workflows.workflow import WorkflowInstance
from orca.workflow_models.workflow_templates import WorkflowTemplate


def _resolved_for(
    resolved_map: dict[tuple[str, str], ResolvedAcquisition], group_id: str, named: dict[str, str], thread: ThreadTemplate,
) -> ResolvedAcquisition | None:
    member = named.get(thread.name)
    return None if member is None else resolved_map.get((group_id, member))


def _left_out_by(named: dict[str, str], named_by_group: Iterable[dict[str, str]], workflow: WorkflowTemplate) -> dict[str, str]:
    """The optional entry threads whose plate will never reach this group, by labware name."""
    left_out: dict[str, str] = {}
    every_group = list(named_by_group)
    for thread in workflow.entry_thread_templates:
        labware = thread.labware_template
        if (
            thread.required
            or thread.name in named
            or thread.name in workflow.auto_spawn_registry
            or labware.submission_batching is SubmissionBatching.BATCHABLE
        ):
            continue
        if labware.group_sharing is GroupSharing.SHARED_ACROSS_GROUPS and any(thread.name in g for g in every_group):
            continue
        left_out[thread.name] = thread.func_name
    return left_out


class MethodFactory:

    def create_instance(self, template: IMethodTemplate) -> IMethod:
        if isinstance(template, MethodTemplate):
            return MethodInstance(template.name, failure_policy=template.method_failure_policy)
        raise TypeError(f"Unknown method template type: {type(template)}")


class ThreadFactory:
    def __init__(self,
                 method_factory: MethodFactory,
                 labware_contents: LabwareContentsLedger) -> None:
        self._method_factory = method_factory
        self._labware_contents = labware_contents

    async def create_instance(
        self,
        template: ThreadTemplate,
        *,
        run_mode: WorkflowRunMode,
        shared_method: ExecutingMethod | None = None,
        group_id: str | None = None,
        submission_id: str | None = None,
        batch_mode: BatchMode = BatchMode.STANDALONE,
        resolved: ResolvedAcquisition | None = None,
    ) -> LabwareThreadInstance:
        """Construct a LabwareThreadInstance.

        ``resolved`` is populated at submit time (see
        SystemRuntime._resolve_acquisitions). When non-None:
        - ``labware_instance``: the thread attaches this existing labware
          (BarcodeAcquisition path) instead of minting a fresh one from
          the template.
        - ``start_location``: overrides the thread template's default
          start_location for this group (LocationAcquisition path).

        """
        if resolved is not None and resolved.labware_instance is not None:
            labware_instance = resolved.labware_instance
        else:
            labware_instance = await template.labware_template.create_instance()
        # Every labware gets its opening ledger entry here, whether this thread
        # minted it or adopted one that was already standing on the deck. The
        # write is a no-op once the record holds one, so adopting a consumed
        # rack cannot reset it to full.
        await labware_instance.enter_record(self._labware_contents)

        if resolved is not None and resolved.start_location is not None:
            start_location = resolved.start_location
        else:
            start_location = template.start_location

        thread = LabwareThreadInstance(
            labware_instance,
            start_location,
            template.end_locations,
            run_mode=run_mode,
            group_id=group_id,
            submission_id=submission_id,
            batch_mode=batch_mode,
        )
        thread.set_labware_template(template.labware_template)
        thread.set_thread_template(template)

        thread.set_yield_func(template.func)

        if shared_method is not None:
            thread.set_shared_executing_method(shared_method)
            shared_method.assign_thread(template.labware_template, thread)

        return thread


class WorkflowFactory:
    def __init__(self, thread_factory: ThreadFactory) -> None:
        self._thread_factory = thread_factory

    async def create_instance(
        self,
        template: WorkflowTemplate,
        *,
        run_mode: WorkflowRunMode,
        id: str | None = None,
    ) -> WorkflowInstance:
        """Single-lineage workflow instance, used when no submission is available.

        `id` is injected by SystemRuntime so the WorkflowInstance's id equals
        the execution_id returned from submit_workflow. See WorkflowInstance.__init__.
        """
        workflow = WorkflowInstance(template.name, template=template, id=id)
        for start_thread_template in template.entry_thread_templates:
            start_thread = await self._thread_factory.create_instance(
                start_thread_template, run_mode=run_mode,
            )
            workflow.add_entry_thread(start_thread)

        for event in template.event_hooks:
            workflow.add_event_hook(event)
        return workflow

    async def build_entry_threads_for(
        self,
        template: WorkflowTemplate,
        submission_id: str,
        groups: Sequence[LabwareGroup] = (),
        batch_mode: BatchMode = BatchMode.STANDALONE,
        resolved_acquisitions: dict[tuple[str, str], ResolvedAcquisition] | None = None,
        *,
        run_mode: WorkflowRunMode,
    ) -> list[LabwareThreadInstance]:
        """Build entry threads for a submission without constructing a new
        WorkflowInstance. Used by both initial boot and mid-run injection.

        Empty ``groups`` falls back to single-lineage (one thread per entry
        template, untagged by group).

        ``resolved_acquisitions``: per-(group_id, thread_template_name) map
        populated at submit time. When a member has a non-Pool acquisition,
        its ResolvedAcquisition is forwarded to ThreadFactory.create_instance
        so the factory can attach a pre-resolved LabwareInstance (Barcode
        case) or override start_location (Location case).
        """
        threads: list[LabwareThreadInstance] = []
        resolved_map = resolved_acquisitions or {}
        if not groups:
            for start_thread_template in template.entry_thread_templates:
                threads.append(await self._thread_factory.create_instance(
                    start_thread_template,
                    run_mode=run_mode,
                    submission_id=submission_id,
                    batch_mode=batch_mode,
                ))
        else:
            entry_templates = template.entry_thread_templates
            named_by_group = {group.id: threads_a_group_names(group, template.thread_templates) for group in groups}
            for group in groups:
                named = named_by_group[group.id]
                # A barcode the group gave for a partner thread names that partner for the group's own threads.
                named_partners: dict[str, str] = {}
                for partner_template in template.thread_templates:
                    if any(partner_template is entry for entry in entry_templates):
                        continue
                    partner = _resolved_for(resolved_map, group.id, named, partner_template)
                    if partner is not None and partner.labware_instance is not None and partner.labware_instance.barcode:
                        named_partners[partner_template.labware_template.name] = partner.labware_instance.barcode
                left_out = _left_out_by(named, named_by_group.values(), template)
                for start_thread_template in template.entry_thread_templates:
                    if not start_thread_template.required and start_thread_template.name not in named:
                        continue
                    resolved = _resolved_for(resolved_map, group.id, named, start_thread_template)
                    threads.append(await self._thread_factory.create_instance(
                        start_thread_template,
                        run_mode=run_mode,
                        submission_id=submission_id,
                        group_id=group.id,
                        batch_mode=batch_mode,
                        resolved=resolved,
                    ))
                    threads[-1].set_named_partners(named_partners)
                    threads[-1].set_left_out(left_out)
        return threads

    async def create_instance_for_submission(
        self,
        template: WorkflowTemplate,
        submission_id: str,
        groups: Sequence[LabwareGroup] = (),
        batch_mode: BatchMode = BatchMode.STANDALONE,
        resolved_acquisitions: dict[tuple[str, str], ResolvedAcquisition] | None = None,
        id: str | None = None,
        *,
        run_mode: WorkflowRunMode,
    ) -> WorkflowInstance:
        """Multi-group workflow instance.

        For each group, spawns one LabwareThreadInstance per entry thread
        template. A workflow with E entry templates and N groups produces
        E * N entry threads. Empty ``groups`` falls back to single-lineage
        behavior (one thread per template, untagged).

        `id` lets SystemRuntime tie WorkflowInstance.id to execution_id so the
        event forwarder can stay a simple set-membership check.
        """
        workflow = WorkflowInstance(template.name, template=template, id=id)
        for thread in await self.build_entry_threads_for(
            template, submission_id, groups, batch_mode,
            resolved_acquisitions=resolved_acquisitions,
            run_mode=run_mode,
        ):
            workflow.add_entry_thread(thread)

        for event in template.event_hooks:
            workflow.add_event_hook(event)
        return workflow
    

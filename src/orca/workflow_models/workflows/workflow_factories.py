from typing import Sequence

from orca.resource_models.labware import LabwareInstance
from orca.state.contents import LabwareContentsLedger
from orca.state.ops_history import OpsHistory
from orca.runtime.labware_group import LabwareGroup
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.submission import ResolvedAcquisition
from orca.runtime.submission_modes import BatchMode
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.actions.dynamic_resource_action import UnresolvedLocationAction
from orca.workflow_models.interfaces import IMethod
from orca.workflow_models.labware_threads.labware_thread import (
    LabwareThreadInstance,
)
from orca.workflow_models.method import ExecutingMethod, MethodInstance
from orca.workflow_models.method_template import IMethodTemplate, MethodTemplate
from orca.workflow_models.thread_template import ThreadTemplate
from orca.workflow_models.workflows.workflow import WorkflowInstance
from orca.workflow_models.workflow_templates import WorkflowTemplate


class MethodActionFactory:

    def __init__(self, template: ActionTemplate) -> None:
        self._template = template

    def create_instance(self) -> UnresolvedLocationAction:

        instance = UnresolvedLocationAction(self._template.resource_pool,
                                        self._template.get_location_action(),
                                        self._template.inputs,
                                        self._template.outputs,
                                        self._template.options,
                                        self._template.failure_policy,
                                        self._template.deck_positions,
                                        self._template.well_selectors,
                                        self._template.declares,
                                        tag=self._template.tag,
                                        )
        return instance


class MethodFactory:

    def create_instance(self, template: IMethodTemplate) -> IMethod:
        if isinstance(template, MethodTemplate):
            return MethodInstance(template.name, failure_policy=template.method_failure_policy)
        raise TypeError(f"Unknown method template type: {type(template)}")


class ThreadFactory:
    def __init__(self,
                 method_factory: MethodFactory,
                 ops_history: OpsHistory) -> None:
        self._method_factory = method_factory
        # Stateless over the same OpsHistory, so it answers identically to the
        # System's own; holding one here keeps the constructor unchanged.
        self._labware_contents = LabwareContentsLedger(ops_history)

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
        """Single-lineage workflow instance (pre-T6 path, still used when
        no submission is available).

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
        template, untagged by group) — preserves the legacy pre-T6 path.

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
            for group in groups:
                for start_thread_template in template.entry_thread_templates:
                    # Members may reference a thread by its func_name OR its
                    # labware name. Try both when looking up the resolved
                    # acquisition captured at submit time.
                    resolved = (
                        resolved_map.get((group.id, start_thread_template.name))
                        or resolved_map.get((group.id, start_thread_template.func_name))
                    )
                    threads.append(await self._thread_factory.create_instance(
                        start_thread_template,
                        run_mode=run_mode,
                        submission_id=submission_id,
                        group_id=group.id,
                        batch_mode=batch_mode,
                        resolved=resolved,
                    ))
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
    



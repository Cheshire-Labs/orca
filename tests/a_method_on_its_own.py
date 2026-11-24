"""Run one method on its own through `runtime.submit_method`, on a system built from a registry and system map.

Call it after the test decorates its actions and method: the workflow it declares collects the methods
decorated since the last workflow, which is how a workflow file bundles its methods.
"""
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass

import orca.orca as orca
from orca.config import OrcaConfig
from orca.events.event_bus import EventBus
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.labware import LabwareInstance, LabwareTemplate
from orca.resource_models.resource_registry import ResourceRegistry
from orca.runtime.execution_record import ExecutionRecord
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system_interface import ISystem
from orca.system.system_map import SystemMap
from orca.workflow_models.workflow_context import WorkflowContext
from tests.labware_catalog import bundled_labware_catalog
from tests.memory_activity import memory_labware_service, memory_unsaved_positions
from tests.memory_activity import memory_ops_store, memory_runtime, memory_variable_store

_WORKFLOW = "holds_the_method"


@dataclass(frozen=True)
class MethodRun:
    system: ISystem
    ended: ExecutionRecord
    labware: dict[str, LabwareInstance]
    """The run's labware by template name. A plate the run took away is no longer in ``system.labwares``."""


def _holds_the_method(wf: WorkflowContext) -> None:
    return None


@asynccontextmanager
async def a_method_run_on_its_own(
    registry: ResourceRegistry,
    system_map: SystemMap,
    labwares: Sequence[LabwareTemplate],
    method_name: str,
    start: dict[str, str],
    end: dict[str, str],
) -> AsyncIterator[MethodRun]:
    """`start` and `end` map each labware template's name to a location name. The run is a check run."""
    workflow = orca.workflow(name=_WORKFLOW)(_holds_the_method)
    builder = SdkToSystemBuilder(
        name="one_method", description="", labwares=list(labwares), resources_registry=registry,
        system_map=system_map, workflows=[workflow], event_bus=EventBus(), config=OrcaConfig(),
        ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(), unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    runtime = await memory_runtime(builder.get_system())
    await runtime.start()
    try:
        record = await runtime.submit_method(_WORKFLOW, method_name, start, end, mode=WorkflowRunMode.CHECK_RUN)
        ended = await runtime.wait(record.id)
        executing = runtime._executions[record.id].executing_workflow
        assert executing is not None
        labware = {t.labware.template_name: t.labware for t in executing.threads if t.labware is not None}
        yield MethodRun(runtime.system, ended, labware)
    finally:
        await runtime.shutdown(confirm=True)

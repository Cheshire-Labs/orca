"""Load the labware a run needs: the New-run dialog's load step, and `orca labware load-run`."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar

from cheshire_drivers.labware_seed import PlateSeedEntry
from cheshire_drivers.liquid_handler_models import TROUGH_WELL_ID
from pydantic import BaseModel

from orca.operations.binding import OperationStep, RestBinding
from orca.operations.error_mapping import known_operation_error
from orca.operations.labware_models import (
    ClearAllLabwareRequest,
    ClearAllLabwareResponse,
    DischargeLabwareRequest,
    DischargeLabwareResponse,
    LabwareMutationResponse,
    RegisterLabwareRequest,
    RegisterLabwareResponse,
    SetWellVolumesRequest,
)
from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError, message_of
from orca.operations.run_loading_models import (
    LoadedRunItem,
    LoadRunLabwareRequest,
    LoadRunLabwareResponse,
    NotLoadedRunItem,
    RunLabwareItem,
)
from orca.runtime.facades.labware_catalog import ILabwareCatalogFacade
from orca.resource_models.labware_catalog_protocol import LabwareNotFound
from orca.runtime.runtime_interface import ISystemRuntime
from orca.runtime.status_models import LabwareSnapshot, ThreadTemplateSnapshot
from orca.operations.run_loading_models import (
    LOAD_RUN_LABWARE_ROUTE,
)

logger = logging.getLogger(__name__)

_VOLUME_REASON = "loaded before a run"


@dataclass(frozen=True)
class LoadSteps:
    """The labware Operations a load runs, each served on its own route too."""

    discharge: OperationStep[DischargeLabwareRequest, DischargeLabwareResponse]
    register: OperationStep[RegisterLabwareRequest, RegisterLabwareResponse]
    set_well_volumes: OperationStep[SetWellVolumesRequest, LabwareMutationResponse]
    clear_all: OperationStep[ClearAllLabwareRequest, ClearAllLabwareResponse]


@dataclass(frozen=True)
class _Workflow:
    """The threads a workflow has put down before a run, and which of them are entry threads."""

    name: str
    thread_names: frozenset[str]
    needed: dict[str, ThreadTemplateSnapshot]
    entry_names: frozenset[str]


class LoadRunLabwareOperation:
    """Per thread: keep reused labware already in place, or take off what is there, record it, set its volume."""

    Request: ClassVar[type[BaseModel]] = LOAD_RUN_LABWARE_ROUTE.request
    Response: ClassVar[type[BaseModel]] = LOAD_RUN_LABWARE_ROUTE.response
    binding: ClassVar[RestBinding] = LOAD_RUN_LABWARE_ROUTE.binding

    def __init__(self, *, runtime: ISystemRuntime, catalog: ILabwareCatalogFacade, steps: LoadSteps) -> None:
        self._runtime = runtime
        self._catalog = catalog
        self._steps = steps

    async def run(self, req: LoadRunLabwareRequest) -> LoadRunLabwareResponse:
        workflow = self._workflow(req.workflow_name)
        items = req.items or [RunLabwareItem(thread_template_name=name) for name in workflow.needed]
        cleared: list[str] = []
        labware: list[LabwareSnapshot] = []
        if req.clear_all_first:
            cleared = (await self._steps.clear_all.run(ClearAllLabwareRequest())).cleared
        else:
            labware = await self._runtime.labware.list_all()
        loaded: list[LoadedRunItem] = []
        kept: list[LoadedRunItem] = []
        not_loaded: list[NotLoadedRunItem] = []
        for item in items:
            name = item.thread_template_name
            try:
                labware_id, was_kept = await self._load_item(workflow, item, labware)
            except Exception as exc:
                failure = _as_operation_error(exc, name)
                not_loaded.append(NotLoadedRunItem(
                    thread_template_name=name, code=failure.surface_code(), message=failure.message,
                ))
                continue
            (kept if was_kept else loaded).append(LoadedRunItem(thread_template_name=name, labware_id=labware_id))
        return LoadRunLabwareResponse(cleared=cleared, loaded=loaded, kept=kept, not_loaded=not_loaded)

    def _workflow(self, name: str) -> _Workflow:
        registry = self._runtime.registry
        snapshot = next((w for w in registry.list_workflow_templates() if w.name == name), None)
        if snapshot is None:
            raise OperationError.of(ErrorCode.WORKFLOW_NOT_FOUND, f"workflow {name!r} not found", workflow_name=name)
        threads = [t for t in registry.list_thread_templates() if t.workflow_name == name]
        return _Workflow(
            name=name,
            thread_names=frozenset(t.name for t in threads),
            needed={t.name: t for t in threads if t.placed_before_run},
            entry_names=frozenset(snapshot.entry_thread_template_names),
        )

    async def _load_item(self, workflow: _Workflow, item: RunLabwareItem, labware: Sequence[LabwareSnapshot]) -> tuple[str, bool]:
        """The labware id the thread starts on, and whether it was already there and kept."""
        thread = _needed_thread(workflow, item.thread_template_name)
        resident = _resident(thread, workflow, labware)
        if resident is not None:
            return resident, True
        occupant = next((lw.id for lw in labware if lw.current_location == thread.start_position_id), None)
        return await self._load(thread, item.volume_ul, occupant), False

    async def _load(self, thread: ThreadTemplateSnapshot, volume_ul: float | None, occupant_id: str | None) -> str:
        volumes = await self._volumes(thread, volume_ul)
        if occupant_id is not None:
            await self._steps.discharge.run(DischargeLabwareRequest(labware_id=occupant_id))
        registered = await self._steps.register.run(RegisterLabwareRequest(
            template_name=thread.labware_template_name, location=thread.start_position_id,
        ))
        if volumes:
            await self._steps.set_well_volumes.run(SetWellVolumesRequest(
                labware_id=registered.labware.id, well_volumes=volumes, reason=_VOLUME_REASON,
            ))
        return registered.labware.id

    async def _volumes(self, thread: ThreadTemplateSnapshot, volume_ul: float | None) -> dict[str, float]:
        """The volume to set in every well, or none when it is the workflow's own figure."""
        declared = thread.initial_state.uniform_volume if thread.initial_state is not None else None
        if volume_ul is None or volume_ul == declared:
            return {}
        return {well: volume_ul for well in await self._wells(thread.labware_type)}

    async def _wells(self, labware_type: str) -> list[str]:
        try:
            entry = await self._catalog.get(labware_type)
        except LabwareNotFound as exc:
            raise OperationError.validation_error(
                f"the labware catalog does not hold {labware_type}, so it takes no volume", labware_type=labware_type,
            ) from exc
        if entry.category == "trough":
            return [TROUGH_WELL_ID]
        if entry.category != "plate":
            raise OperationError.validation_error(
                f"{labware_type} is a {entry.category}; only a plate or a trough takes a volume",
                labware_type=labware_type,
            )
        wells = [well.identifier for well in PlateSeedEntry.model_validate(entry.geometry).wells]
        if not wells:
            raise OperationError.validation_error(f"the catalog lists no wells for {labware_type}", labware_type=labware_type)
        return wells


def _needed_thread(workflow: _Workflow, name: str) -> ThreadTemplateSnapshot:
    thread = workflow.needed.get(name)
    if thread is not None:
        return thread
    if name in workflow.thread_names:
        raise OperationError.validation_error(
            f"thread {name!r} of workflow {workflow.name!r} is not put down before a run", thread_name=name,
        )
    raise OperationError.of(ErrorCode.THREAD_TEMPLATE_NOT_FOUND, f"workflow {workflow.name!r} has no thread {name!r}", thread_name=name)


def _resident(thread: ThreadTemplateSnapshot, workflow: _Workflow, labware: Sequence[LabwareSnapshot]) -> str | None:
    """Reused labware of the thread's own template on its start position, kept so it keeps its tips and volumes.

    An entry plate is never kept: a run refuses one an earlier run used, and the record does not say which were used.
    """
    if thread.name in workflow.entry_names:
        return None
    return next(
        (lw.id for lw in labware
         if lw.current_location == thread.start_position_id and lw.template_name == thread.labware_template_name),
        None,
    )


def _as_operation_error(exc: Exception, thread_name: str) -> OperationError:
    known = known_operation_error(exc)
    if known is not None:
        return known
    logger.exception("loading thread %r before a run failed", thread_name)
    return OperationError(ErrorCode.INTERNAL_ERROR, message_of(exc))

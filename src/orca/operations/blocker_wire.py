"""Turn the runtime's blockers into the wire list a surface sends.

A remedy step names an action. Each surface says where that action is served,
so a remedy never points a daemon user at a hosted route, or the reverse.
"""

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from orca.operations.binding import BoundOperation, check_binding
from orca.operations.blocker_models import (
    BlockerDTO,
    BlockersPayload,
    RemedyDTO,
    RemedyStepDTO,
    payload_of,
    unknown_blockers,
)
from orca.runtime.blockers import Blocker
from orca.runtime.remedies import Remedy
from orca.runtime.runtime_interface import ISystemRuntime

logger = logging.getLogger("orca.blockers")

_PATH_PARAM = re.compile(r"\{(\w+)(?::path)?\}")


@dataclass(frozen=True)
class ActionRoute:
    """Where one remedy action is served on one surface."""

    rest: str
    query: tuple[str, ...] = ()
    tool: str | None = None


ActionRoutes = Mapping[str, ActionRoute]


def route_of(op_class: type[BoundOperation], *, tool: str | None = None) -> ActionRoute:
    """The route an Operation is bound at, in the form a remedy step carries."""
    binding = check_binding(op_class)
    query: tuple[str, ...] = ()
    if binding.fields_in_query:
        query = tuple(n for n in op_class.Request.model_fields if n not in binding.path_params)
    path = _PATH_PARAM.sub(r"<\1>", binding.url)
    return ActionRoute(rest=f"{binding.method} {path}", query=query, tool=tool)


def to_dto(blocker: Blocker, routes: ActionRoutes) -> BlockerDTO:
    """A remedy with a step this surface cannot serve is left out."""
    return BlockerDTO(
        id=blocker.id,
        kind=blocker.kind.value,
        severity=blocker.severity.value,
        since=blocker.since,
        headline=blocker.headline,
        detail=blocker.detail,
        device_name=blocker.device_name,
        execution_id=blocker.execution_id,
        thread_id=blocker.thread_id,
        labware_id=blocker.labware_id,
        incident_id=blocker.incident_id,
        may_still_be_moving=blocker.may_still_be_moving,
        blocks=list(blocker.blocks),
        remedies=[dto for r in blocker.remedies if (dto := _remedy(r, routes)) is not None],
    )


def _remedy(remedy: Remedy, routes: ActionRoutes) -> RemedyDTO | None:
    steps: list[RemedyStepDTO] = []
    for step in remedy.steps:
        route = routes.get(step.action)
        if route is None:
            return None
        steps.append(RemedyStepDTO(
            verb=step.verb,
            action=str(step.action),
            label=step.label,
            args=step.args,
            rest=route.rest,
            tool=route.tool,
            cli=step.cli,
            needs=list(step.needs),
            query=list(route.query),
        ))
    return RemedyDTO(
        id=remedy.id,
        label=remedy.label,
        explain=remedy.explain,
        recommended=remedy.recommended,
        confirm=remedy.confirm,
        steps=steps,
    )


async def blockers_now(
    runtime: ISystemRuntime | None,
    routes: ActionRoutes,
    *,
    without_a_runtime: Callable[[], Blocker],
) -> BlockersPayload:
    """Everything stopping the run, worst first.

    With no runtime, `without_a_runtime()` is the answer: the missing runtime is
    itself what stops the run. It is called only then.
    """
    if runtime is None:
        return payload_of([to_dto(without_a_runtime(), routes)])
    try:
        derived = await runtime.blockers()
        return payload_of([to_dto(b, routes) for b in derived])
    except Exception:
        # A recovery call that already succeeded must not fail because this
        # follow-up read did; the caller is told the list is unknown instead.
        logger.exception("could not read the blocker list")
        return unknown_blockers()

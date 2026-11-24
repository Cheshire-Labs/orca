"""Every remedy step the blocker list offers carries arguments its route's request accepts."""

from datetime import datetime

import pytest
from pydantic import JsonValue, TypeAdapter, ValidationError
from pydantic.fields import FieldInfo

from orca.operations.remedy_operations import REMEDY_OPERATIONS
from orca.runtime.blockers import Blocker, LabwareOccupant, derive_blockers
from orca.runtime.incident_store import IncidentCategory
from orca.runtime.remedies import RemedyAction, RemedyStep, known_error_types
from orca.runtime.status_models import PendingManualStepRecord, ReservationSnapshot
from orca.workflow_models.status_enums import PauseSite
from tests.test_one_list_says_what_is_stopping_the_run import (
    FakeSources,
    action_failed,
    device,
    execution,
    fault,
    incident,
    thread,
)


def _every_kind_of_blocker() -> list[FakeSources]:
    return [
        FakeSources(devices=[device("pf400_1", fault=fault("initialize"))]),
        FakeSources(devices=[device("pf400_1", fault=fault("pick_plate", outcome="unknown"))]),
        FakeSources(devices=[device("pf400_1", held="console")]),
        *(
            FakeSources(executions=[execution([
                thread("t1", pause_reason="error", pause_site=site, paused_device_command="pick_plate"),
            ])])
            for site in PauseSite
        ),
        *(
            FakeSources(
                executions=[execution([
                    thread("t1", pause_reason="error", pause_site=site, paused_device_command="aspirate"),
                ])],
                incidents=[incident(IncidentCategory.ACTION_FAILED, thread_id="t1", detail=action_failed(error_type))],
            )
            for error_type in sorted(known_error_types())
            for site in PauseSite
        ),
        FakeSources(executions=[execution([
            thread("t1", status="AWAITING_MANUAL_PLACE", waiting_for="pad_1", current_location="shelf_3"),
        ])]),
        FakeSources(executions=[execution([
            thread("t1", status="AWAITING_MANUAL_PLACE", waiting_for="pad_1", current_location="pad_1"),
        ])]),
        FakeSources(
            executions=[execution([
                thread("plate_1", name="plate_1", status="AWAITING_MANUAL_REMOVE", waiting_for="flex_1/C4"),
                thread("plate_2", name="plate_2", status="AWAITING_ACTION_RESERVATION", waiting_for="flex_1"),
            ])],
            reservations=[ReservationSnapshot(position_id="flex_1", reservation_id="rsv-1", thread_id="plate_1")],
        ),
        FakeSources(
            executions=[execution([
                thread("plate_2", name="plate_2", status="AWAITING_MOVE_RESERVATION", waiting_for="pad_2"),
            ])],
            occupants={"pad_2": LabwareOccupant(labware_id="lw-stray", labware_name="stray_plate")},
        ),
        FakeSources(executions=[execution([], paused=True, pause_reason="system")]),
        FakeSources(manual_steps=[PendingManualStepRecord(
            execution_id="exec-1", step_id="step-1", instruction="Top up the reservoir", emitted_at=datetime.now(),
        )]),
    ]


def _steps(blockers: list[Blocker]) -> list[RemedyStep]:
    return [step for blocker in blockers for remedy in blocker.remedies for step in remedy.steps]


def _what_is_wrong_with(step: RemedyStep) -> list[str]:
    fields = REMEDY_OPERATIONS[RemedyAction(step.action)].Request.model_fields
    unknown = [f"{step.verb} sends {name!r}, which its route does not take" for name in step.args if name not in fields]
    wrong = [
        f"{step.verb} sends {name!r} as {value!r}, which its route refuses"
        for name, value in step.args.items() if name in fields and not _accepts(fields[name], value)
    ]
    unknown += [f"{step.verb} asks for {name!r}, which its route does not take" for name in step.needs if name not in fields]
    given = {*step.args, *step.needs}
    missing = [
        f"{step.verb} leaves out {name!r}, which its route needs"
        for name, info in fields.items() if info.is_required() and name not in given
    ]
    return unknown + wrong + missing


def _accepts(field: FieldInfo, value: JsonValue) -> bool:
    try:
        TypeAdapter(field.rebuild_annotation()).validate_python(value)
    except ValidationError:
        return False
    return True


@pytest.mark.asyncio
async def test_every_remedy_step_sends_what_its_route_takes() -> None:
    steps = [step for sources in _every_kind_of_blocker() for step in _steps(await derive_blockers(sources))]

    assert {RemedyAction(step.action) for step in steps} == set(REMEDY_OPERATIONS)
    assert [problem for step in steps for problem in _what_is_wrong_with(step)] == []

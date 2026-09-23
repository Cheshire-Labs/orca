"""Tests for the daemon's submission Operation bindings.

Every submission URL lives on the unified ``/operations/*`` surface:

- ``POST /operations/submit-execution`` accepts a workflow name + zero-or-more groups.
- ``POST /operations/list-submissions`` (with optional ``execution_id``
  filter in the body) lists known submissions.
- ``POST /operations/get-submission`` fetches one by id.

Closing an execution lives at ``POST /operations/close-execution`` (see
``test_routes_executions.py``). A submission is a unit of work inside
an execution, not a closable container.
"""

from httpx import AsyncClient

from orca.resource_models.labware import LabwareInstance
from orca.runtime.sim_labware import SimPlateTemplate
from orca.state.records import ObservationGapCause
from orca.runtime.system_runtime import SystemRuntime


# -- Gating ------------------------------------------------------------------


async def test_submissions_submit_gated(empty_client: AsyncClient) -> None:
    """No system loaded -> 503 service_unavailable.

    The SubmitExecutionOperation binder maps
    ``OperationError.service_unavailable`` to 503 to align with the
    REST convention for "service not ready". The gating contract --
    submissions are rejected without a runtime -- is preserved
    regardless of the URL the request lands at.
    """
    resp = await empty_client.post(
        "/operations/submit-execution",
        json={"workflow_name": "simple_workflow", "run_mode": "PURE_SIM"},
    )
    assert resp.status_code == 503


async def test_submissions_list_gated(empty_client: AsyncClient) -> None:
    resp = await empty_client.post(
        "/operations/list-submissions", json={},
    )
    assert resp.status_code == 503


async def test_submissions_get_gated(empty_client: AsyncClient) -> None:
    resp = await empty_client.post(
        "/operations/get-submission",
        json={"submission_id": "any-id"},
    )
    assert resp.status_code == 503


# -- Submit ------------------------------------------------------------------


async def test_submissions_submit_unknown_workflow_returns_404(
    client: AsyncClient,
) -> None:
    resp = await client.post(
        "/operations/submit-execution",
        json={"workflow_name": "no_such_workflow", "run_mode": "PURE_SIM"},
    )
    assert resp.status_code == 404


async def test_submissions_submit_groupless_accepts(
    client: AsyncClient,
) -> None:
    """Groupless submit is the legacy one-shot path; accepts and returns
    a SubmitExecutionResponse with the same field shape as a
    SubmissionDTO."""
    resp = await client.post(
        "/operations/submit-execution",
        json={"workflow_name": "simple_workflow", "run_mode": "PURE_SIM"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["workflow_name"] == "simple_workflow"
    assert body["group_count"] == 0
    assert body["batch_mode"] == "STANDALONE"
    assert "id" in body and "execution_id" in body


async def test_submissions_submit_invalid_batch_mode_returns_400(
    client: AsyncClient,
) -> None:
    resp = await client.post(
        "/operations/submit-execution",
        json={"workflow_name": "simple_workflow", "batch_mode": "NOT_A_MODE"},
    )
    # Pydantic rejects at validation layer (422) because batch_mode is a Literal.
    # Accept either 400 or 422 as valid client-error codes.
    assert resp.status_code in (400, 422)


async def test_a_submission_says_what_nobody_has_settled(
    client: AsyncClient,
) -> None:
    """Committing a deck to a run is the one moment an operator is certainly
    looking, so what a person still owes the system rides back with it.

    The same list is readable at `/operations/unsettled`, and that needs
    somebody to think of asking. This does not.
    """
    resp = await client.post(
        "/operations/submit-execution",
        json={"workflow_name": "simple_workflow", "run_mode": "PURE_SIM"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "unsettled" in body, (
        "a submission that does not carry the worklist leaves it behind a call "
        "nobody makes"
    )
    for row in body["unsettled"]:
        assert row["provenance"] in ("unknown", "stale")
        # A row either names the verb that settles it, or names none because
        # only ending an unfinished action helps. Both say what to do; a row
        # that does neither asks for nothing.
        assert row["settle_with"] or "settle the action" in row["detail"]


async def test_a_rack_nobody_has_looked_at_rides_back_with_the_submission(
    runtime: SystemRuntime, client: AsyncClient,
) -> None:
    """The row itself, not just the key.

    A system with nothing on it answers with an empty list, so a test that
    only checks the key is present passes whether the read runs or returns
    `[]` unconditionally. This one puts something unsettled on the deck first.
    """
    plate = await SimPlateTemplate("assay_plate").create_instance()
    await plate.enter_record(runtime.system.labware_contents)
    runtime.system.add_labware(plate)
    await runtime.system.labware_contents.assert_volumes(plate, {"A1": 100.0})
    await plate.note_observation_gap(ObservationGapCause.RUNTIME_RESTART)

    resp = await client.post(
        "/operations/submit-execution",
        json={"workflow_name": "simple_workflow", "run_mode": "PURE_SIM"},
    )

    assert resp.status_code == 200, resp.text
    listed = {row["subject"]: row for row in resp.json()["unsettled"]}
    assert plate.name in listed, (
        f"a labware nobody has looked at did not ride back; got {listed}"
    )
    assert listed[plate.name]["provenance"] == "stale"
    # The id the verb takes rides with the row: two live instances can share a
    # name, so a client resolving one from the other can hit the wrong plate.
    assert listed[plate.name]["subject_id"] == plate.id
    # Stale from a restart alone, so the record is still the best value there
    # is and agreeing with it settles the row.
    assert listed[plate.name]["settle_with"] == "confirm-well-volumes"


async def test_reading_the_worklist_never_fails_a_submission(
    client: AsyncClient, monkeypatch,
) -> None:
    """The run was accepted. Refusing it over a report ABOUT the run would
    trade real work for a piece of paper."""
    async def _explode(self) -> list[object]:
        raise RuntimeError("the worklist read fell over")

    monkeypatch.setattr(SystemRuntime, "unsettled_state", _explode)

    resp = await client.post(
        "/operations/submit-execution",
        json={"workflow_name": "simple_workflow", "run_mode": "PURE_SIM"},
    )

    assert resp.status_code == 200, resp.text
    # The shape does not change either: a caller reading `unsettled` gets an
    # empty list, not a missing key it has to guard for.
    assert resp.json()["unsettled"] == []


# -- List / Get --------------------------------------------------------------


async def test_submissions_list_after_submit(client: AsyncClient) -> None:
    submit_resp = await client.post(
        "/operations/submit-execution",
        json={"workflow_name": "simple_workflow", "run_mode": "PURE_SIM"},
    )
    assert submit_resp.status_code == 200
    submission_id = submit_resp.json()["id"]
    execution_id = submit_resp.json()["execution_id"]

    resp = await client.post(
        "/operations/list-submissions", json={},
    )
    assert resp.status_code == 200
    items = resp.json()["submissions"]
    assert any(s["id"] == submission_id for s in items)

    filtered = await client.post(
        "/operations/list-submissions",
        json={"execution_id": execution_id},
    )
    assert filtered.status_code == 200
    filtered_items = filtered.json()["submissions"]
    assert all(s["execution_id"] == execution_id for s in filtered_items)
    assert any(s["id"] == submission_id for s in filtered_items)


async def test_submissions_get_unknown_returns_404(
    client: AsyncClient,
) -> None:
    resp = await client.post(
        "/operations/get-submission",
        json={"submission_id": "no-such-submission"},
    )
    assert resp.status_code == 404


async def test_submissions_get_after_submit(client: AsyncClient) -> None:
    submit_resp = await client.post(
        "/operations/submit-execution",
        json={"workflow_name": "simple_workflow", "run_mode": "PURE_SIM"},
    )
    submission_id = submit_resp.json()["id"]

    resp = await client.post(
        "/operations/get-submission",
        json={"submission_id": submission_id},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["submission"]["id"] == submission_id


# -- same-workflow back-to-back submission ---------------------------------


async def test_submission_refused_when_start_location_occupied(
    runtime: SystemRuntime, client: AsyncClient,
) -> None:
    """Review item L9: pin the wire-level 409 + START_LOCATION_OCCUPIED
    envelope when a submission targets a start_location whose `labware`
    slot is already occupied (the canonical case: a prior execution
    left a plate behind).

    Without the pre-submit check, the engine would silently retry
    `DeviceBusyError` from `initialize_labware` forever (the original
    stall bug). The check now refuses the submission with a typed
    envelope the operator can act on. This daemon test pins the wire
    shape end-to-end; the unit-level coverage lives in
    `tests/test_submit_start_location_check.py`.

    Pre-populating pad1 directly via the system map sidesteps the
    timing-sensitive "submit twice; race the first thread's
    initialize_labware" alternative -- we want to pin the wire shape,
    not test scheduler timing.
    """
    pad1 = runtime.system.system_map.get_location("pad1")
    leftover = LabwareInstance("plate_96", "96_well")
    runtime.system.add_labware(leftover)
    pad1.initialize_labware(leftover)
    assert pad1.labware is leftover

    resp = await client.post(
        "/operations/submit-execution",
        json={"workflow_name": "simple_workflow", "run_mode": "PURE_SIM"},
    )
    assert resp.status_code == 409, resp.text
    body = resp.json()
    detail = body.get("detail", body)
    # ``SubmitExecutionOperation`` maps RuntimeError -> OperationError.conflict
    # -> 409. The structured envelope (code + extras) is the hosted-side
    # surface; the daemon's job is to surface the typed message faithfully
    # so the hosted error-translation layer can pick it up. The Operation
    # binder reshapes the daemon envelope to
    # ``{detail: {code, message, extras}}`` so the assertion reads the
    # message off the nested structure.
    if isinstance(detail, dict):
        detail = detail.get("message", "")
    assert isinstance(detail, str), body
    assert "start_location" in detail.lower()
    assert "pad1" in detail



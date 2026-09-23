"""Route tests for /executions endpoints. Each test targets a specific
bug-shape; no tautologies. Bodies parsed through DTOs -- no magic strings.

Covered:
- Submit + list wiring: proves the POST route's ExecutionRecord lands in the
  runtime's registry (same instance observable via GET). If submit accidentally
  called a different runtime method (or a no-op), list would be empty.
- Error mappings written by hand in routes.py: KeyError -> 404 for unknown
  execution on GET / DELETE / threads subpath.
- Request-body validation at the HTTP boundary: missing workflow_name -> 422,
  ensures the SubmitWorkflowRequest DTO is actually enforced by FastAPI.
"""

from httpx import AsyncClient

from orca.daemon.schemas import (
    ErrorResponse,
    ExecutionRecordDTO,
    ValidationErrorResponse,
)


async def test_submit_then_list_round_trips_the_execution(
    client: AsyncClient,
) -> None:
    """Proves route handler actually reached runtime.submit_workflow AND that
    the returned id is the same id list_executions surfaces.

    If the POST handler accidentally no-op'd, GET would return []. If it
    returned a fabricated id, the id wouldn't appear in the GET response.
    """
    submit = await client.post(
        "/executions", json={"workflow_name": "simple_workflow", "run_mode": "PURE_SIM"},
    )
    assert submit.status_code == 200
    submitted = ExecutionRecordDTO.model_validate(submit.json())

    listing = await client.get("/operations/list-executions")
    assert listing.status_code == 200
    records = [
        ExecutionRecordDTO.model_validate(item)
        for item in listing.json()["executions"]
    ]
    assert submitted.id in {r.id for r in records}


async def test_get_execution_by_unknown_id_returns_404(
    client: AsyncClient,
) -> None:
    """KeyError from get_execution_detail must map to 404, not 500."""
    resp = await client.post(
        "/operations/get-execution-detail",
        json={"execution_id": "does-not-exist"},
    )
    assert resp.status_code == 404


async def test_delete_execution_by_unknown_id_returns_404(
    client: AsyncClient,
) -> None:
    """Same mapping on the stop path."""
    resp = await client.post(
        "/operations/stop-execution",
        json={"execution_id": "does-not-exist"},
    )
    assert resp.status_code == 404


async def test_list_threads_for_unknown_execution_returns_404(
    client: AsyncClient,
) -> None:
    """Same mapping on the thread subpath."""
    resp = await client.get("/executions/does-not-exist/threads")
    assert resp.status_code == 404


async def test_submit_with_missing_workflow_name_returns_422(
    client: AsyncClient,
) -> None:
    """Pydantic request-body validation must reject malformed inputs.

    This proves SubmitWorkflowRequest is actually bound as the body model,
    not accepted as a loose dict. If someone loosened the signature to
    `body: dict`, we'd silently accept missing fields.
    """
    resp = await client.post("/executions", json={})
    assert resp.status_code == 422
    body = ValidationErrorResponse.model_validate(resp.json())
    missing_fields = {
        str(item.loc[-1]) for item in body.detail if item.type == "missing"
    }
    assert "workflow_name" in missing_fields


# -- Close -------------------------------------------------------------------


async def test_execution_close_gated(empty_client: AsyncClient) -> None:
    """No system loaded -> the Operations binder surfaces
    ``service_unavailable`` (503), not the legacy 409."""
    resp = await empty_client.post(
        "/operations/close-execution",
        json={"execution_id": "any-id"},
    )
    assert resp.status_code == 503


async def test_execution_close_unknown_returns_404(
    client: AsyncClient,
) -> None:
    resp = await client.post(
        "/operations/close-execution",
        json={"execution_id": "no-such-execution"},
    )
    assert resp.status_code == 404


async def test_execution_close_transitions_to_draining(
    client: AsyncClient,
) -> None:
    """Happy path: submit an execution, close it, assert phase=draining."""
    submit = await client.post(
        "/executions", json={"workflow_name": "simple_workflow", "run_mode": "PURE_SIM"},
    )
    assert submit.status_code == 200
    execution_id = submit.json()["id"]

    resp = await client.post(
        "/operations/close-execution",
        json={"execution_id": execution_id},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["execution_id"] == execution_id
    assert body["phase"] == "draining"

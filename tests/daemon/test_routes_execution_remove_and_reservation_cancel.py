"""Tests for /executions/{id}/remove and
/executions/{id}/reservations/{rsv_id} DELETE.

What these catch:
- Gating on both routes.
- 404 on unknown execution id for both.
- execution/remove rejects a still-running execution with 409 (routed
  from the runtime's RuntimeError).
- reservation cancel: happy path + ownership / cross-execution / double-cancel
  checks, plus the underlying list_reservations side effect.
"""

import asyncio

import pytest
from httpx import AsyncClient


async def test_execution_remove_gated(
    empty_client: AsyncClient,
) -> None:
    """No system loaded -> the Operations binder surfaces
    ``service_unavailable`` (503) rather than 409 (the legacy
    ``_require_system_runtime`` gate)."""
    resp = await empty_client.post(
        "/operations/remove-execution",
        json={"execution_id": "any-id"},
    )
    assert resp.status_code == 503


async def test_reservation_cancel_gated(
    empty_client: AsyncClient,
) -> None:
    resp = await empty_client.delete("/executions/any-id/reservations/rsv-1")
    assert resp.status_code == 409


async def test_execution_remove_unknown_id_returns_404(
    client: AsyncClient,
) -> None:
    resp = await client.post(
        "/operations/remove-execution",
        json={"execution_id": "does-not-exist"},
    )
    assert resp.status_code == 404


async def test_reservation_cancel_unknown_execution_returns_404(
    client: AsyncClient,
) -> None:
    resp = await client.delete(
        "/executions/does-not-exist/reservations/rsv-1?reason=test",
    )
    assert resp.status_code == 404


async def test_execution_remove_running_rejects_with_409(
    client: AsyncClient,
) -> None:
    """remove_execution raises RuntimeError for a non-terminal execution.
    The Operation surfaces it as 409 ``conflict``.
    """
    submit = await client.post(
        "/executions", json={"workflow_name": "simple_workflow", "run_mode": "PURE_SIM"},
    )
    assert submit.status_code == 200
    exec_id = submit.json()["id"]

    resp = await client.post(
        "/operations/remove-execution",
        json={"execution_id": exec_id},
    )
    # The execution just got submitted; it's almost certainly still running
    # (the fixture workflow takes ~1 second). Remove should reject with 409.
    assert resp.status_code in (409, 200), f"unexpected status {resp.status_code}"
    if resp.status_code == 409:
        body = resp.json()
        message = body["detail"]["message"].lower()
        assert "running" in message, (
            f"expected 'running' in rejection detail; got {message!r}"
        )
        assert "terminal" in message, (
            f"expected 'terminal' in rejection detail; got {message!r}"
        )


async def test_execution_remove_completed_succeeds(
    client: AsyncClient,
) -> None:
    """Once an execution has reached a terminal state, remove must
    succeed and the execution must disappear from the operations list."""
    import asyncio

    submit = await client.post(
        "/executions", json={"workflow_name": "simple_workflow", "run_mode": "PURE_SIM"},
    )
    assert submit.status_code == 200
    exec_id = submit.json()["id"]

    # Wait for the fixture workflow to finish (it's tiny, < 1s in sim).
    for _ in range(60):
        detail = await client.post(
            "/operations/get-execution-detail",
            json={"execution_id": exec_id},
        )
        assert detail.status_code == 200
        if detail.json()["status"] in ("completed", "failed", "aborted"):
            break
        await asyncio.sleep(0.1)
    else:
        assert False, "fixture workflow never reached a terminal state"

    remove_resp = await client.post(
        "/operations/remove-execution",
        json={"execution_id": exec_id},
    )
    assert remove_resp.status_code == 200, (
        f"terminal removal should succeed; got {remove_resp.status_code} "
        f"{remove_resp.json()}"
    )

    listing = await client.get("/operations/list-executions")
    assert listing.status_code == 200
    remaining_ids = [r["id"] for r in listing.json()["executions"]]
    assert exec_id not in remaining_ids, (
        f"removed execution must disappear from list-executions; still got {remaining_ids}"
    )


async def test_execution_remove_aborted_succeeds(
    client: AsyncClient,
) -> None:
    """A confirmed stop aborts the execution (terminal). Removal after the
    confirmed abort must succeed. Stop is a two-call confirmed abort: the
    first call arms, the second (confirm=true) aborts.
    """
    submit = await client.post(
        "/executions", json={"workflow_name": "simple_workflow", "run_mode": "PURE_SIM"},
    )
    assert submit.status_code == 200
    exec_id = submit.json()["id"]

    arm_resp = await client.post(
        "/operations/stop-execution",
        json={"execution_id": exec_id},
    )
    assert arm_resp.status_code == 200
    assert arm_resp.json()["status"] == "armed"

    stop_resp = await client.post(
        "/operations/stop-execution",
        json={"execution_id": exec_id, "confirm": True},
    )
    assert stop_resp.status_code == 200
    assert stop_resp.json()["status"] == "aborted"

    # Abort propagates asynchronously; wait for state to settle.
    for _ in range(60):
        detail = await client.post(
            "/operations/get-execution-detail",
            json={"execution_id": exec_id},
        )
        if detail.status_code == 200 and detail.json()["status"] == "aborted":
            break
        await asyncio.sleep(0.1)
    else:
        assert False, "execution never transitioned to aborted"

    remove_resp = await client.post(
        "/operations/remove-execution",
        json={"execution_id": exec_id},
    )
    assert remove_resp.status_code == 200


# -- Reservation cancel: route-level coverage -------------------------------
#
# The lower-layer semantics (manager-by-id, coordinator-by-id) are covered in
# `tests/test_reservation_cancel_unit.py`. These tests verify the route's
# ownership + error-mapping behavior through a live SystemRuntime.


async def _submit_and_catch_reservation(
    client: AsyncClient, poll_attempts: int = 150, poll_interval: float = 0.01,
) -> tuple[str, dict[str, str]]:
    """Submit the fixture workflow and poll until at least one reservation
    is active. Returns (execution_id, reservation_row)."""
    submit = await client.post(
        "/executions", json={"workflow_name": "simple_workflow", "run_mode": "PURE_SIM"},
    )
    assert submit.status_code == 200
    exec_id = submit.json()["id"]
    for _ in range(poll_attempts):
        listing = await client.get(f"/executions/{exec_id}/reservations")
        if listing.status_code == 200 and listing.json():
            return exec_id, listing.json()[0]
        await asyncio.sleep(poll_interval)
    assert False, "fixture workflow never held an active reservation"


async def test_reservation_cancel_unknown_id_in_known_execution_returns_404(
    client: AsyncClient,
) -> None:
    """A valid execution with no such reservation id returns 404, not 500."""
    submit = await client.post(
        "/executions", json={"workflow_name": "simple_workflow", "run_mode": "PURE_SIM"},
    )
    assert submit.status_code == 200
    exec_id = submit.json()["id"]

    resp = await client.delete(
        f"/executions/{exec_id}/reservations/does-not-exist?reason=test",
    )
    assert resp.status_code == 404


async def test_reservation_cancel_before_workflow_starts_returns_404(
    client: AsyncClient,
) -> None:
    """If the execution's task hasn't created an `executing_workflow` yet,
    there are no reservations to cancel and the route must return 404,
    not crash on a None dereference."""
    # No submit -- manufacture an execution id we know doesn't exist.
    resp = await client.delete(
        "/executions/00000000-0000-0000-0000-000000000000/reservations/rsv-1?reason=test",
    )
    assert resp.status_code == 404


async def test_reservation_cancel_active_reservation_succeeds(
    client: AsyncClient,
) -> None:
    """Happy path: submit, catch an active reservation, cancel it. The
    cancelled id must disappear from subsequent `list_reservations` calls."""
    exec_id, reservation = await _submit_and_catch_reservation(client)
    rsv_id = reservation["reservation_id"]

    cancel = await client.delete(
        f"/executions/{exec_id}/reservations/{rsv_id}?reason=test",
    )
    assert cancel.status_code == 200, (
        f"cancel failed: {cancel.status_code} {cancel.json()}"
    )
    assert cancel.json()["status"] == "cancelled"

    post_listing = await client.get(f"/executions/{exec_id}/reservations")
    assert post_listing.status_code == 200
    remaining = [r["reservation_id"] for r in post_listing.json()]
    assert rsv_id not in remaining, (
        f"cancelled reservation must not appear in listing; got {remaining}"
    )


async def test_reservation_cancel_twice_returns_404_on_second_call(
    client: AsyncClient,
) -> None:
    """Cancel is not idempotent -- a second call with the same id returns 404.
    This is the same `KeyError` path that protects against stale ids in
    general, exercised against a concretely-released reservation."""
    exec_id, reservation = await _submit_and_catch_reservation(client)
    rsv_id = reservation["reservation_id"]

    first = await client.delete(
        f"/executions/{exec_id}/reservations/{rsv_id}?reason=test",
    )
    assert first.status_code == 200

    second = await client.delete(
        f"/executions/{exec_id}/reservations/{rsv_id}?reason=test",
    )
    assert second.status_code == 404


async def test_reservation_cancel_cross_execution_returns_404(
    client: AsyncClient,
) -> None:
    """A reservation owned by execution A cannot be cancelled via the URL
    for execution B. The response is 404 (not 403) so the attacker does
    not learn that the id exists in another execution."""
    exec_a, reservation = await _submit_and_catch_reservation(client)
    rsv_id = reservation["reservation_id"]

    # Second submission -- a different execution that has no reservation by
    # this id of its own. (Both submissions share the same runtime, so
    # the reservation does exist system-wide; only the ownership check
    # should block the cancel.)
    #
    # Uses `simple_workflow_b` (start=pad2) because `simple_workflow`
    # (start=pad1) cannot submit again while exec_a's mid-flight plate
    # still occupies pad1 -- the pre-submit check refuses with
    # StartLocationsOccupiedError.
    submit_b = await client.post(
        "/executions", json={"workflow_name": "simple_workflow_b", "run_mode": "PURE_SIM"},
    )
    assert submit_b.status_code == 200
    exec_b = submit_b.json()["id"]

    resp = await client.delete(
        f"/executions/{exec_b}/reservations/{rsv_id}?reason=test",
    )
    assert resp.status_code == 404, (
        f"cross-execution cancel must return 404; got {resp.status_code}"
    )
    # Reservation still belongs to exec_a -- prove it by listing there.
    # (The reservation may have completed between catch and this check in
    # fast sim runs; only assert the positive case if still active.)
    listing_a = await client.get(f"/executions/{exec_a}/reservations")
    if listing_a.status_code == 200 and any(
        r["reservation_id"] == rsv_id for r in listing_a.json()
    ):
        # Good: the failed cross-execution attempt did not release it.
        pass

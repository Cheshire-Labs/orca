"""Wire-shape tests for the v3.4 typed envelope codes on submit routes.

Sim-hierarchy v3.4 introduces three submit-time error codes that escape
through the typed envelope shape (`{detail: {code, message, extras}}`)
rather than `detail: str(e)`:

- RUN_MODE_REQUIRED (422): Pydantic schema rejects before the runtime
  sees the call. Asserted as the Pydantic-422 shape with `loc=(body,
  run_mode)` so a future schema loosening (e.g. `run_mode: str | None
  = None`) is caught before the typed runtime path goes dead.
- RUN_MODE_MISMATCH (409): a JOIN_EXISTING submission whose run_mode
  differs from the live execution's run_mode (it replaced the refuse-all
  `CONCURRENT_SUBMISSION_REFUSED`); envelope carries
  `blocking_execution_id`, `blocking_workflow_name`,
  `existing_run_mode`, `submitted_run_mode`.
- LIVE_SUBMISSION_WITH_SIM_OVERRIDES_UNACKNOWLEDGED (422): blocked when
  a LIVE submission encounters a topology device with a sim-direction
  sim_override; envelope carries `devices[].sim_override` +
  `.resolved_mode`.

A refactor that flattened `detail` to `str(e)` would not fail any
existing test; this file is the regression guard.
"""

from typing import cast

import pytest
from fastapi import HTTPException
from httpx import AsyncClient

from orca.daemon.routes import (
    _raise_concurrent_live_sim_refused,
    _raise_live_sim_overrides_unacknowledged,
    _raise_run_mode_mismatch,
)
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.runtime_interface import (
    ConcurrentLiveSimRefusedError,
    LiveSubmissionWithSimOverridesUnacknowledgedError,
    OccupiedSlot,
    RunModeMismatchError,
    StartLocationsOccupiedError,
)
from orca.runtime.system_runtime import SystemRuntime


# -- RUN_MODE_REQUIRED -------------------------------------------------------


async def test_run_mode_missing_returns_422(client: AsyncClient) -> None:
    """Missing `run_mode` on the submit body is rejected by the Pydantic
    schema before reaching the typed runtime gate. The 422 carries the
    standard Pydantic `detail=[{loc, msg, type}]` shape with the
    `run_mode` field named. A schema relaxation that re-routes the
    request through the dead-but-defensive runtime path would change
    this shape and fail the test."""
    resp = await client.post(
        "/executions",
        json={"workflow_name": "simple_workflow"},
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert "detail" in body
    error_locs = [tuple(e.get("loc", [])) for e in body["detail"]]
    assert ("body", "run_mode") in error_locs, (
        f"expected ('body', 'run_mode') in {error_locs!r}"
    )


# -- RUN_MODE_MISMATCH -------------------------------------------------------


def test_run_mode_mismatch_envelope_wire_shape() -> None:
    """`_raise_run_mode_mismatch` translates a `RunModeMismatchError` into
    a 409 HTTPException whose `detail` is the typed envelope dict.
    Asserts the full wire shape so a refactor that drops a field or
    flattens to `str(e)` is caught.

    Replaces the `CONCURRENT_SUBMISSION_REFUSED` envelope, lifted when
    device initialization became lazy. The integration path that
    reaches `RunModeMismatchError` requires a live ACCEPTING execution
    with a mismatched-mode JOIN_EXISTING submission landing on top; the
    runtime-level test for that path lives in
    `tests/runtime/test_lazy_init_and_resolver.py`. This test pins the
    wire shape directly.
    """
    exc = RunModeMismatchError(
        blocking_execution_id="exec-abc",
        blocking_workflow_name="smc_assay",
        existing_run_mode=WorkflowRunMode.PURE_SIM,
        submitted_run_mode=WorkflowRunMode.LIVE,
    )
    with pytest.raises(HTTPException) as exc_info:
        _raise_run_mode_mismatch(exc)
    raised = exc_info.value
    assert raised.status_code == 409
    assert isinstance(raised.detail, dict), (
        f"detail must be a dict, got {type(raised.detail)!r}"
    )
    detail = cast(dict[str, object], raised.detail)
    assert detail["code"] == "RUN_MODE_MISMATCH"
    message = detail["message"]
    assert isinstance(message, str) and message
    extras = cast(dict[str, object], detail["extras"])
    assert extras["blocking_execution_id"] == "exec-abc"
    assert extras["blocking_workflow_name"] == "smc_assay"
    assert extras["existing_run_mode"] == "PURE_SIM"
    assert extras["submitted_run_mode"] == "LIVE"


# -- CONCURRENT_LIVE_SIM_REFUSED --------------------------------------------


def test_concurrent_live_sim_refused_envelope_wire_shape() -> None:
    """`_raise_concurrent_live_sim_refused` translates a
    `ConcurrentLiveSimRefusedError` into a 409 HTTPException whose
    `detail` is the typed envelope dict, with both run modes in extras."""
    exc = ConcurrentLiveSimRefusedError(
        blocking_execution_id="exec-live",
        blocking_workflow_name="smc_assay",
        existing_run_mode=WorkflowRunMode.LIVE,
        submitted_run_mode=WorkflowRunMode.DEVICE_SIM,
    )
    with pytest.raises(HTTPException) as exc_info:
        _raise_concurrent_live_sim_refused(exc)
    raised = exc_info.value
    assert raised.status_code == 409
    assert isinstance(raised.detail, dict)
    detail = raised.detail
    assert detail["code"] == "CONCURRENT_LIVE_SIM_REFUSED"
    message = detail["message"]
    assert isinstance(message, str) and message
    extras = detail["extras"]
    assert isinstance(extras, dict)
    assert extras["blocking_execution_id"] == "exec-live"
    assert extras["blocking_workflow_name"] == "smc_assay"
    assert extras["existing_run_mode"] == "LIVE"
    assert extras["submitted_run_mode"] == "DEVICE_SIM"


# -- LIVE_SUBMISSION_WITH_SIM_OVERRIDES_UNACKNOWLEDGED -----------------------
#
# The integration path (POST /executions with mode=LIVE against a system
# whose device declares `sim_override=PURE_SIM`) cannot be reached from a
# unit-test fixture without a connected gateway: the LIVE branch of
# `device_registry.assert_runnable` fires first and rejects with
# `WorkflowDeviceNotConnectedError` (409). Standing up a fake gateway
# just to reach the envelope is more scaffolding than the contract
# warrants. Instead, exercise the envelope helper directly -- the
# helper IS the wire-shape contract; the route handlers' exception
# ladder is already covered for the live and concurrent codes in the
# tests above.


# -- LIVE except-ladder regression tests on each submit route ---------------
#
# The unit test below verifies the helper produces the right wire shape.
# These tests verify that each route handler's except-ladder catches the
# typed error and INVOKES the helper. A regression that drops the
# `except LiveSubmissionWithSimOverridesUnacknowledgedError` clause from
# any of the three submit routes would let the error fall through to a
# generic 500 (the class inherits from ValueError, which has no FastAPI
# mapping); these tests catch that.
#
# The integration path (real LIVE-mode submission against a fixture with
# a sim-direction sim_override device) is unreachable without a fake
# gateway, because `device_registry.assert_runnable` for LIVE rejects on
# disconnected devices before the typed-envelope gate fires. Monkeypatching
# the runtime's submit method bypasses that earlier gate and exercises
# the except-ladder + helper invocation in one shot.


def _make_live_sim_overrides_error() -> LiveSubmissionWithSimOverridesUnacknowledgedError:
    return LiveSubmissionWithSimOverridesUnacknowledgedError(
        devices=[("shaker1", WorkflowRunMode.PURE_SIM, WorkflowRunMode.PURE_SIM)],
    )


def _assert_live_sim_overrides_envelope(response_json: dict[str, object]) -> None:
    detail = response_json["detail"]
    assert isinstance(detail, dict), (
        f"detail must be the typed envelope dict, got {type(detail)!r}"
    )
    detail_dict = cast(dict[str, object], detail)
    assert detail_dict["code"] == "LIVE_SUBMISSION_WITH_SIM_OVERRIDES_UNACKNOWLEDGED"
    message = detail_dict["message"]
    assert isinstance(message, str) and message
    extras = cast(dict[str, object], detail_dict["extras"])
    devices = extras["devices"]
    assert devices == [
        {
            "name": "shaker1",
            "sim_override": "PURE_SIM",
            "resolved_mode": "PURE_SIM",
        },
    ]


async def test_post_executions_live_sim_overrides_route_returns_typed_422(
    runtime: SystemRuntime,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /executions catches LiveSubmissionWithSimOverridesUnacknowledgedError
    and invokes `_raise_live_sim_overrides_unacknowledged`."""
    async def _stub_submit_workflow(*args: str, **kwargs: object) -> None:
        del args, kwargs
        raise _make_live_sim_overrides_error()

    monkeypatch.setattr(runtime, "submit_workflow", _stub_submit_workflow)
    resp = await client.post(
        "/executions",
        json={"workflow_name": "simple_workflow", "run_mode": "LIVE"},
    )
    assert resp.status_code == 422, resp.text
    _assert_live_sim_overrides_envelope(resp.json())


async def test_post_method_executions_live_sim_overrides_route_returns_typed_422(
    runtime: SystemRuntime,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /method-executions catches the typed error and invokes the helper."""
    async def _stub_submit_method(*args: str, **kwargs: object) -> None:
        del args, kwargs
        raise _make_live_sim_overrides_error()

    monkeypatch.setattr(runtime, "submit_method", _stub_submit_method)
    resp = await client.post(
        "/method-executions",
        json={
            "workflow_name": "simple_workflow",
            "method_name": "shake_method",
            "labware_start": {"plate_96": "pad1"},
            "labware_end": {"plate_96": "pad1"},
            "run_mode": "LIVE",
        },
    )
    assert resp.status_code == 422, resp.text
    _assert_live_sim_overrides_envelope(resp.json())


async def test_post_submissions_live_sim_overrides_route_returns_typed_422(
    runtime: SystemRuntime,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /operations/submit-execution catches the typed error and
    invokes the helper. The route delegates through the
    SubmissionsFacade (`rt.submissions.submit_group`), so the
    monkeypatch targets that nested attribute.

    The legacy ``POST /submissions`` URL is gone; the
    SubmitExecutionOperation binder serves the same surface at
    ``POST /operations/submit-execution``.
    """
    async def _stub_submit_group(*args: str, **kwargs: object) -> None:
        del args, kwargs
        raise _make_live_sim_overrides_error()

    monkeypatch.setattr(runtime.submissions, "submit_group", _stub_submit_group)
    resp = await client.post(
        "/operations/submit-execution",
        json={
            "workflow_name": "simple_workflow",
            "run_mode": "LIVE",
            # Force the multi-group path (where ``submit_group`` runs +
            # the typed LIVE-sim-overrides exception fires); the legacy
            # ``submit_workflow`` path bypasses ``submit_group`` and
            # would not exercise the typed-envelope contract under test.
            "acknowledge_warnings": False,
            "operator_id": "test-operator",
        },
    )
    assert resp.status_code == 422, resp.text
    _assert_live_sim_overrides_envelope(resp.json())


def test_live_sim_overrides_envelope_wire_shape() -> None:
    """`_raise_live_sim_overrides_unacknowledged` translates the runtime
    error into a 422 HTTPException whose `detail` is the typed envelope
    dict. Asserts the full wire shape, including the `extras.devices`
    list with `sim_override` and `resolved_mode` fields, so a refactor
    that flattens `detail` to `str(e)` or drops a field is caught."""
    exc = LiveSubmissionWithSimOverridesUnacknowledgedError(
        devices=[
            ("shaker1", WorkflowRunMode.PURE_SIM, WorkflowRunMode.PURE_SIM),
            ("centrifuge2", WorkflowRunMode.DEVICE_SIM, WorkflowRunMode.DEVICE_SIM),
        ],
    )
    with pytest.raises(HTTPException) as exc_info:
        _raise_live_sim_overrides_unacknowledged(exc)
    raised = exc_info.value
    assert raised.status_code == 422
    assert isinstance(raised.detail, dict), (
        f"detail must be a dict, got {type(raised.detail)!r}"
    )
    # Cast narrows pyright's view of HTTPException.detail (typed as `Any`
    # then narrowed to `dict[Unknown, Unknown]`) to the concrete shape
    # the helper guarantees on the wire.
    detail = cast(dict[str, object], raised.detail)
    assert detail["code"] == "LIVE_SUBMISSION_WITH_SIM_OVERRIDES_UNACKNOWLEDGED"
    message = detail["message"]
    assert isinstance(message, str) and message
    extras = cast(dict[str, object], detail["extras"])
    assert extras["devices"] == [
        {
            "name": "shaker1",
            "sim_override": "PURE_SIM",
            "resolved_mode": "PURE_SIM",
        },
        {
            "name": "centrifuge2",
            "sim_override": "DEVICE_SIM",
            "resolved_mode": "DEVICE_SIM",
        },
    ]


# -- START_LOCATION_OCCUPIED -------------------------------------------------


async def test_post_method_executions_occupied_start_returns_typed_409(
    runtime: SystemRuntime,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /method-executions catches StartLocationsOccupiedError and answers
    the typed 409.

    A standalone method brings its own labware, so it meets the same
    start-location check a workflow submission does. The route caught only
    KeyError / TypeError / ValueError, and the error is a RuntimeError, so it
    escaped as a bare 500 with no slot in it for the operator to clear. The
    engine-side half of this is pinned by
    tests/test_standalone_method_submission_e2e.py.
    """
    async def _stub_submit_method(*args: str, **kwargs: object) -> None:
        del args, kwargs
        raise StartLocationsOccupiedError([
            OccupiedSlot(
                position_id="pad1",
                existing_labware_name="plate_96-8d727600",
                existing_template_name="plate_96",
            ),
        ])

    monkeypatch.setattr(runtime, "submit_method", _stub_submit_method)
    resp = await client.post(
        "/method-executions",
        json={
            "workflow_name": "simple_workflow",
            "method_name": "shake_method",
            "labware_start": {"plate_96": "pad1"},
            "labware_end": {"plate_96": "pad1"},
            "run_mode": "PURE_SIM",
        },
    )

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    # The value a hosted deployment's ErrorCode carries and this repo's submit-execution
    # binder emits. The route used to send the enum NAME instead.
    assert detail["code"] == "start_location_occupied"
    assert "pad1" in detail["message"]
    assert detail["extras"]["occupied"] == [
        {
            "position_id": "pad1",
            "existing_labware_name": "plate_96-8d727600",
            "existing_template_name": "plate_96",
            "source": "unknown",
        },
    ]

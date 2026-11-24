"""Tests for variable HTTP routes.

What these catch:
- Gating (pre-load 503) on every variable route.
- Wire-up: round-trip via variables.set_global (no execution id needed)
  and variables.get on the same name.
- Error mapping: unknown execution id -> 404, unknown var name -> 404.
"""

from pathlib import Path

import pytest
from httpx import AsyncClient

from orca.runtime.system_runtime import SystemRuntime
from orca.daemon.app import InjectedSystem
from orca.variables import VariableDefinition
from tests.device_modes import run_by_an_orca_client


# -- Gating ------------------------------------------------------------------


async def test_variables_list_gated(
    empty_client: AsyncClient,
) -> None:
    resp = await empty_client.get("/api/executions/any-id/variables")
    assert resp.status_code == 503


async def test_variables_get_gated(
    empty_client: AsyncClient,
) -> None:
    resp = await empty_client.get("/api/executions/any-id/variables/any-name")
    assert resp.status_code == 503


async def test_variables_set_gated(
    empty_client: AsyncClient,
) -> None:
    resp = await empty_client.put(
        "/api/executions/any-id/variables/any-name", json={"value": 1},
    )
    assert resp.status_code == 503


async def test_variables_set_global_gated(
    empty_client: AsyncClient,
) -> None:
    resp = await empty_client.put(
        "/api/variables/global/any-name", json={"value": 1},
    )
    assert resp.status_code == 503


async def test_variables_unset_gated(
    empty_client: AsyncClient,
) -> None:
    resp = await empty_client.delete("/api/executions/any-id/variables/any-name")
    assert resp.status_code == 503


# -- Wire-up -----------------------------------------------------------------


async def test_set_global_then_list_roundtrip(
    client: AsyncClient,
) -> None:
    """PUT a global var; list for any execution must include it under the
    `global.` prefix. The variable store explicitly does NOT fall back from
    bare names to global -- operators must write `global.<name>` to reach
    globals. `get_all()` surfaces globals with that prefix intact for
    explicit visibility in listings.
    """
    # Set a global var via the set_global route.
    set_resp = await client.put(
        "/api/variables/global/flag1", json={"value": "hello"},
    )
    assert set_resp.status_code == 200
    # Submit an execution (so we have one to ask about).
    submit = await client.post(
        "/api/executions", json={"workflow_name": "simple_workflow", "run_mode": "CHECK_RUN"},
    )
    assert submit.status_code == 200
    exec_id = submit.json()["execution_id"]
    # List vars for this execution.
    listing = await client.get(f"/api/executions/{exec_id}/variables")
    assert listing.status_code == 200
    body = listing.json()
    # Globals appear under the `global.` prefix in listings.
    assert body.get("global.flag1") == "hello", (
        f"expected global.flag1=hello, got {body}"
    )
    # Bare-name access must NOT return the global value.
    assert "flag1" not in body, (
        f"bare 'flag1' must not appear in listing when only global is set; got {body}"
    )


async def test_variables_get_unknown_execution_returns_404(
    client: AsyncClient,
) -> None:
    resp = await client.get("/api/executions/does-not-exist/variables/some-var")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "execution_not_found"


# -- Profile submission ------------------------------------------------------


async def test_submit_with_a_profile_name_applies_before_threads_run(
    client: AsyncClient,
) -> None:
    """`POST /executions` with `deployment_profile` loads the saved profile into
    the new execution's variables before its threads start."""
    saved = await client.post("/api/profiles", json={
        "name": "test_profile",
        "description": "smoke",
        "variables": {"shaker_speed": 1200, "shaker_dwell_s": 3.5},
    })
    assert saved.status_code == 201, saved.text

    resp = await client.post(
        "/api/executions",
        json={
            "workflow_name": "simple_workflow",
            "deployment_profile": "test_profile",
            "run_mode": "CHECK_RUN",
        },
    )
    assert resp.status_code == 200, f"submit failed: {resp.status_code} {resp.json()}"
    assert resp.json()["deployment_profile"] == "test_profile"
    exec_id = resp.json()["execution_id"]

    listing = await client.get(f"/api/executions/{exec_id}/variables")
    assert listing.status_code == 200
    body = listing.json()
    assert body.get("shaker_speed") == 1200, (
        f"profile var must be applied; got {body}"
    )
    assert body.get("shaker_dwell_s") == 3.5, (
        f"profile var must be applied; got {body}"
    )


async def test_submit_with_an_unknown_profile_name_returns_404(
    client: AsyncClient,
) -> None:
    """An unknown name is refused and no execution is created."""
    pre_list = await client.get("/api/executions")
    assert pre_list.status_code == 200
    before_ids = {r["id"] for r in pre_list.json()["executions"]}

    resp = await client.post(
        "/api/executions",
        json={
            "workflow_name": "simple_workflow",
            "deployment_profile": "nope",
            "run_mode": "CHECK_RUN",
        },
    )
    assert resp.status_code == 404, (
        f"expected 404 for unknown profile; got {resp.status_code}"
    )
    assert resp.json()["detail"]["code"] == "profile_not_found"
    assert "nope" in resp.json()["detail"]["message"]

    post_list = await client.get("/api/executions")
    after_ids = {r["id"] for r in post_list.json()["executions"]}
    assert after_ids == before_ids, (
        "no execution should be created when the profile is unknown"
    )


async def test_submit_with_a_value_the_workflow_refuses_returns_422(
    client: AsyncClient, runtime: SystemRuntime,
) -> None:
    """A profile value a variable definition refuses creates no execution."""
    runtime.system.variable_store.register_workflow_definitions(
        "simple_workflow", {"speed": VariableDefinition(type="int", min=0, max=2000)},
    )
    await client.post("/api/profiles", json={"name": "too_fast", "variables": {"speed": 5000}})
    before = (await client.get("/api/executions")).json()["executions"]

    resp = await client.post(
        "/api/executions",
        json={
            "workflow_name": "simple_workflow",
            "deployment_profile": "too_fast",
            "run_mode": "CHECK_RUN",
        },
    )

    assert resp.status_code == 422, resp.text
    assert "speed" in resp.json()["detail"]["message"]
    assert (await client.get("/api/executions")).json()["executions"] == before


async def test_a_bad_profile_value_is_listed_with_every_other_input_problem(
    client: AsyncClient, runtime: SystemRuntime,
) -> None:
    runtime.system.variable_store.register_workflow_definitions(
        "simple_workflow", {"speed": VariableDefinition(type="int", min=0, max=2000)},
    )
    await client.post("/api/profiles", json={"name": "too_fast", "variables": {"speed": 5000}})

    resp = await client.post(
        "/api/executions",
        json={
            "workflow_name": "simple_workflow",
            "deployment_profile": "too_fast",
            "variables": {"colour": "red"},
            "run_mode": "CHECK_RUN",
        },
    )

    assert resp.status_code == 422, resp.text
    message = resp.json()["detail"]["message"]
    assert "speed" in message
    assert "colour is not a variable" in message


async def test_a_run_naming_a_profile_is_refused_when_a_device_is_not_runnable(
    client: AsyncClient, injected: InjectedSystem,
) -> None:
    """A profile run gets the device pre-flight every plain run gets."""
    run_by_an_orca_client(*injected.runtime.system.resources)
    await client.post("/api/profiles", json={"name": "p", "variables": {"speed": 1}})
    before = (await client.get("/api/executions")).json()["executions"]

    resp = await client.post(
        "/api/executions",
        json={
            "workflow_name": "simple_workflow",
            "deployment_profile": "p",
            "run_mode": "LIVE",
        },
    )

    assert resp.status_code == 409, resp.text
    assert "connected" in resp.json()["detail"]["message"].lower()
    assert (await client.get("/api/executions")).json()["executions"] == before


async def test_a_profile_path_in_the_body_is_refused(
    client: AsyncClient, tmp_path: Path,
) -> None:
    """A run names a saved profile. The daemon reads no path from a request."""
    resp = await client.post(
        "/api/executions",
        json={
            "workflow_name": "simple_workflow",
            "profile_path": str(tmp_path / "profile.json"),
            "run_mode": "CHECK_RUN",
        },
    )

    assert resp.status_code == 422, resp.text


async def test_an_inline_value_outranks_the_profile_for_that_submission(
    client: AsyncClient, runtime: SystemRuntime,
) -> None:
    runtime.system.variable_store.register_workflow_definitions(
        "simple_workflow", {"speed": VariableDefinition(type="int")},
    )
    await client.post("/api/profiles", json={"name": "p", "variables": {"speed": 100}})

    resp = await client.post(
        "/api/executions",
        json={
            "workflow_name": "simple_workflow",
            "deployment_profile": "p",
            "variables": {"speed": 7},
            "run_mode": "CHECK_RUN",
        },
    )
    assert resp.status_code == 200, resp.text
    execution_id = resp.json()["execution_id"]
    # The run task writes the inline value before it attaches the workflow.
    await runtime.require_execution(execution_id).workflow_attached.wait()

    listing = await client.get(f"/api/executions/{execution_id}/variables")
    assert listing.json()["speed"] == 7


# -- Submission scope --------------------------------------------------------
#
# Values supplied on a submit land in that submission's partition, which
# outranks the per-execution partition every operator write goes to. The
# partitions here are seeded directly so the assertions do not race a running
# workflow's teardown; that a real submit populates them is pinned in
# tests/test_submission_scope_variables.py.

EXECUTION_ID = "exec-under-test"
SUBMISSION_ID = "sub-a"


@pytest.fixture
def seeded(runtime: SystemRuntime) -> str:
    """An execution whose one submission overrides `inject_fault` to True."""
    store = runtime.system.variable_store
    store.create_execution(EXECUTION_ID, "simple_workflow")
    store.set_submission("inject_fault", True, EXECUTION_ID, SUBMISSION_ID)
    return EXECUTION_ID


async def test_setting_an_execution_variable_names_the_submissions_that_shadow_it(
    client: AsyncClient, seeded: str,
) -> None:
    """The write succeeds but does not change what the run resolves, so the
    response says which submissions still outrank it."""
    resp = await client.put(
        f"/api/executions/{seeded}/variables/inject_fault", json={"value": False},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["shadowed_by"] == [SUBMISSION_ID]


async def test_reading_a_variable_says_which_layer_it_came_from_and_lists_every_submission_override(
    client: AsyncClient, seeded: str,
) -> None:
    await client.put(f"/api/executions/{seeded}/variables/inject_fault", json={"value": False})

    body = (await client.get(f"/api/executions/{seeded}/variables/inject_fault")).json()

    assert body["value"] is False
    assert body["source"] == "execution"
    assert body["overrides"] == [{"submission_id": SUBMISSION_ID, "value": True}]


async def test_reading_for_one_submission_returns_that_submissions_value(
    client: AsyncClient, seeded: str,
) -> None:
    await client.put(f"/api/executions/{seeded}/variables/inject_fault", json={"value": False})

    body = (await client.get(
        f"/api/executions/{seeded}/variables/inject_fault",
        params={"submission_id": SUBMISSION_ID},
    )).json()

    assert body["value"] is True
    assert body["source"] == "submission"


async def test_clearing_the_submission_override_lets_the_execution_value_resolve(
    client: AsyncClient, seeded: str,
) -> None:
    await client.put(f"/api/executions/{seeded}/variables/inject_fault", json={"value": False})

    cleared = await client.delete(
        f"/api/executions/{seeded}/submissions/{SUBMISSION_ID}/variables/inject_fault",
    )

    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["existed"] is True
    after = (await client.get(
        f"/api/executions/{seeded}/variables/inject_fault",
        params={"submission_id": SUBMISSION_ID},
    )).json()
    assert after["value"] is False
    assert after["source"] == "execution"


async def test_writing_the_submission_layer_changes_what_that_submission_resolves(
    client: AsyncClient, seeded: str,
) -> None:
    resp = await client.put(
        f"/api/executions/{seeded}/submissions/{SUBMISSION_ID}/variables/inject_fault",
        json={"value": False},
    )

    assert resp.status_code == 200, resp.text
    after = (await client.get(
        f"/api/executions/{seeded}/variables/inject_fault",
        params={"submission_id": SUBMISSION_ID},
    )).json()
    assert after["value"] is False


async def test_clearing_a_submission_override_that_was_never_set_reports_it(
    client: AsyncClient, seeded: str,
) -> None:
    resp = await client.delete(
        f"/api/executions/{seeded}/submissions/{SUBMISSION_ID}/variables/never_set",
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["existed"] is False


async def test_a_name_only_submissions_hold_answers_with_no_shared_value_and_lists_the_overrides(
    client: AsyncClient, runtime: SystemRuntime, seeded: str,
) -> None:
    runtime.system.variable_store.set_submission("plate_count", 3, seeded, SUBMISSION_ID)

    resp = await client.get(f"/api/executions/{seeded}/variables/plate_count")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["value"] is None
    assert body["source"] is None
    assert body["overrides"] == [{"submission_id": SUBMISSION_ID, "value": 3}]


async def test_a_name_no_layer_holds_is_a_404_on_the_single_variable_read(
    client: AsyncClient, seeded: str,
) -> None:
    resp = await client.get(f"/api/executions/{seeded}/variables/never_set")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "variable_not_found"


async def test_submission_writes_on_an_unknown_execution_return_404(
    client: AsyncClient,
) -> None:
    resp = await client.put(
        "/api/executions/does-not-exist/submissions/sub-a/variables/any-name", json={"value": 1},
    )
    assert resp.status_code == 404


async def test_variables_set_submission_gated(empty_client: AsyncClient) -> None:
    resp = await empty_client.put(
        "/api/executions/any-id/submissions/sub-a/variables/any-name", json={"value": 1},
    )
    assert resp.status_code == 503


async def test_variables_unset_submission_gated(empty_client: AsyncClient) -> None:
    resp = await empty_client.delete(
        "/api/executions/any-id/submissions/sub-a/variables/any-name",
    )
    assert resp.status_code == 503


async def test_a_run_refused_for_a_missing_orca_client_says_how_to_start_one(
    client: AsyncClient, injected: InjectedSystem,
) -> None:
    run_by_an_orca_client(*injected.runtime.system.resources)

    resp = await client.post("/api/executions", json={"workflow_name": "simple_workflow", "run_mode": "SIM"})

    assert resp.status_code == 409, resp.text
    assert "python -m orca_client --config" in resp.json()["detail"]["message"]


@pytest.mark.parametrize("path", [
    f"/api/executions/{EXECUTION_ID}/variables/dilution_factor",
    f"/api/executions/{EXECUTION_ID}/submissions/{SUBMISSION_ID}/variables/dilution_factor",
])
async def test_a_value_of_the_wrong_type_for_the_variable_is_refused_as_invalid(
    client: AsyncClient, runtime: SystemRuntime, seeded: str, path: str,
) -> None:
    runtime.system.variable_store.register_workflow_definitions(
        "simple_workflow", {"dilution_factor": VariableDefinition(type="float")},
    )

    resp = await client.put(path, json={"value": "notanumber"})

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "validation_error"
    assert "dilution_factor" in resp.json()["detail"]["message"]


async def test_a_global_value_of_the_wrong_type_is_refused_as_invalid(client: AsyncClient, runtime: SystemRuntime) -> None:
    runtime.system.variable_store.register_global_definitions({"dilution_factor": VariableDefinition(type="float")})

    resp = await client.put("/api/variables/global/dilution_factor", json={"value": "notanumber"})

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "validation_error"

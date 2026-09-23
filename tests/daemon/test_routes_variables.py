"""Tests for variable HTTP routes.

What these catch:
- Gating (pre-load 409) on every /variables/* route.
- Wire-up: round-trip via variables.set_global (no execution id needed)
  and variables.get on the same name.
- Error mapping: unknown execution id -> 404, unknown var name -> 404.
"""

import pytest
from httpx import AsyncClient

from orca.runtime.system_runtime import SystemRuntime


# -- Gating ------------------------------------------------------------------


async def test_variables_list_gated(
    empty_client: AsyncClient,
) -> None:
    resp = await empty_client.get("/variables/any-id")
    assert resp.status_code == 409


async def test_variables_get_gated(
    empty_client: AsyncClient,
) -> None:
    resp = await empty_client.get("/variables/any-id/any-name")
    assert resp.status_code == 409


async def test_variables_set_gated(
    empty_client: AsyncClient,
) -> None:
    resp = await empty_client.put(
        "/variables/any-id/any-name", json={"value": 1},
    )
    assert resp.status_code == 409


async def test_variables_set_global_gated(
    empty_client: AsyncClient,
) -> None:
    resp = await empty_client.put(
        "/variables/global/any-name", json={"value": 1},
    )
    assert resp.status_code == 409


async def test_variables_unset_gated(
    empty_client: AsyncClient,
) -> None:
    resp = await empty_client.delete("/variables/any-id/any-name")
    assert resp.status_code == 409


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
        "/variables/global/flag1", json={"value": "hello"},
    )
    assert set_resp.status_code == 200
    # Submit an execution (so we have one to ask about).
    submit = await client.post(
        "/executions", json={"workflow_name": "simple_workflow", "run_mode": "PURE_SIM"},
    )
    assert submit.status_code == 200
    exec_id = submit.json()["id"]
    # List vars for this execution.
    listing = await client.get(f"/variables/{exec_id}")
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
    resp = await client.get("/variables/does-not-exist/some-var")
    assert resp.status_code == 404


# -- Profile submission ------------------------------------------------------


async def test_submit_with_profile_path_applies_before_threads_run(
    client: AsyncClient, tmp_path,
) -> None:
    """`POST /executions` with `profile_path` must load the JSON deployment
    profile into the new execution's variable partition synchronously,
    before the workflow's threads start. After submission, every variable
    from the profile resolves under the execution id.
    """
    import json

    profile = {
        "name": "test_profile",
        "description": "smoke",
        "variables": {"shaker_speed": 1200, "shaker_dwell_s": 3.5},
    }
    profile_file = tmp_path / "profile.json"
    profile_file.write_text(json.dumps(profile), encoding="utf-8")

    resp = await client.post(
        "/executions",
        json={
            "workflow_name": "simple_workflow",
            "profile_path": str(profile_file),
            "run_mode": "PURE_SIM",
        },
    )
    assert resp.status_code == 200, f"submit failed: {resp.status_code} {resp.json()}"
    exec_id = resp.json()["id"]

    listing = await client.get(f"/variables/{exec_id}")
    assert listing.status_code == 200
    body = listing.json()
    assert body.get("shaker_speed") == 1200, (
        f"profile var must be applied; got {body}"
    )
    assert body.get("shaker_dwell_s") == 3.5, (
        f"profile var must be applied; got {body}"
    )


async def test_submit_with_missing_profile_path_returns_400(
    client: AsyncClient, tmp_path,
) -> None:
    """A profile_path pointing at a non-existent file must be rejected
    with 400 Bad Request (and no execution should be created)."""
    missing = tmp_path / "nope.json"

    pre_list = await client.get("/operations/list-executions")
    assert pre_list.status_code == 200
    before_ids = {r["id"] for r in pre_list.json()["executions"]}

    resp = await client.post(
        "/executions",
        json={
            "workflow_name": "simple_workflow",
            "profile_path": str(missing),
            "run_mode": "PURE_SIM",
        },
    )
    assert resp.status_code == 400, (
        f"expected 400 for missing profile; got {resp.status_code}"
    )

    post_list = await client.get("/operations/list-executions")
    after_ids = {r["id"] for r in post_list.json()["executions"]}
    assert after_ids == before_ids, (
        "no execution should be created when profile validation fails"
    )


async def test_submit_with_malformed_profile_returns_400(
    client: AsyncClient, tmp_path,
) -> None:
    """A profile_path pointing at an invalid JSON / wrong-shape file must
    be rejected with 400 Bad Request."""
    bad = tmp_path / "bad.json"
    bad.write_text("this is not json", encoding="utf-8")

    resp = await client.post(
        "/executions",
        json={
            "workflow_name": "simple_workflow",
            "profile_path": str(bad),
            "run_mode": "PURE_SIM",
        },
    )
    assert resp.status_code == 400, (
        f"expected 400 for malformed profile; got {resp.status_code}"
    )



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
        f"/variables/{seeded}/inject_fault", json={"value": False},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["shadowed_by"] == [SUBMISSION_ID]


async def test_reading_a_variable_says_which_layer_it_came_from(
    client: AsyncClient, seeded: str,
) -> None:
    await client.put(f"/variables/{seeded}/inject_fault", json={"value": False})

    body = (await client.get(f"/variables/{seeded}/inject_fault")).json()

    assert body["value"] is False
    assert body["source"] == "execution"
    assert body["shadowed_by"] == [SUBMISSION_ID]


async def test_reading_for_one_submission_returns_that_submissions_value(
    client: AsyncClient, seeded: str,
) -> None:
    await client.put(f"/variables/{seeded}/inject_fault", json={"value": False})

    body = (await client.get(
        f"/variables/{seeded}/inject_fault",
        params={"submission_id": SUBMISSION_ID},
    )).json()

    assert body["value"] is True
    assert body["source"] == "submission"


async def test_the_resolution_route_lists_every_submission_override(
    client: AsyncClient, seeded: str,
) -> None:
    await client.put(f"/variables/{seeded}/inject_fault", json={"value": False})

    body = (await client.get(
        f"/variables/{seeded}/inject_fault/resolution",
    )).json()

    assert body["value"] is False
    assert body["source"] == "execution"
    assert body["overrides"] == [{"submission_id": SUBMISSION_ID, "value": True}]


async def test_clearing_the_submission_override_lets_the_execution_value_resolve(
    client: AsyncClient, seeded: str,
) -> None:
    await client.put(f"/variables/{seeded}/inject_fault", json={"value": False})

    cleared = await client.delete(
        f"/variables/submissions/{seeded}/{SUBMISSION_ID}/inject_fault",
    )

    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["existed"] is True
    after = (await client.get(
        f"/variables/{seeded}/inject_fault",
        params={"submission_id": SUBMISSION_ID},
    )).json()
    assert after["value"] is False
    assert after["source"] == "execution"


async def test_writing_the_submission_layer_changes_what_that_submission_resolves(
    client: AsyncClient, seeded: str,
) -> None:
    resp = await client.put(
        f"/variables/submissions/{seeded}/{SUBMISSION_ID}/inject_fault",
        json={"value": False},
    )

    assert resp.status_code == 200, resp.text
    after = (await client.get(
        f"/variables/{seeded}/inject_fault",
        params={"submission_id": SUBMISSION_ID},
    )).json()
    assert after["value"] is False


async def test_clearing_a_submission_override_that_was_never_set_reports_it(
    client: AsyncClient, seeded: str,
) -> None:
    resp = await client.delete(
        f"/variables/submissions/{seeded}/{SUBMISSION_ID}/never_set",
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["existed"] is False


async def test_a_name_no_layer_holds_is_a_404_on_the_single_variable_read(
    client: AsyncClient, seeded: str,
) -> None:
    resp = await client.get(f"/variables/{seeded}/never_set")
    assert resp.status_code == 404


async def test_submission_writes_on_an_unknown_execution_return_404(
    client: AsyncClient,
) -> None:
    resp = await client.put(
        "/variables/submissions/does-not-exist/sub-a/any-name", json={"value": 1},
    )
    assert resp.status_code == 404


async def test_variables_resolution_gated(empty_client: AsyncClient) -> None:
    resp = await empty_client.get("/variables/any-id/any-name/resolution")
    assert resp.status_code == 409


async def test_variables_set_submission_gated(empty_client: AsyncClient) -> None:
    resp = await empty_client.put(
        "/variables/submissions/any-id/sub-a/any-name", json={"value": 1},
    )
    assert resp.status_code == 409


async def test_variables_unset_submission_gated(empty_client: AsyncClient) -> None:
    resp = await empty_client.delete(
        "/variables/submissions/any-id/sub-a/any-name",
    )
    assert resp.status_code == 409

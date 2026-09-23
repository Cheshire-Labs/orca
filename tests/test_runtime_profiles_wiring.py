"""SystemRuntime resolves a deployment profile against its profile_store at submit.

Operator CRUD on profiles lives on the deployment-registries layer, not the
runtime; the runtime exposes only `profile_store` for resolution. The profile
body is poured into the variable store ONCE at submit; after that, the variable
store is the only resolver (editing the profile does NOT affect a running
execution).
"""

import pytest

from orca.runtime.profile_store import FileDeploymentProfileStore
from orca.runtime.system_runtime import SystemRuntime
from orca.variables.deployment_profile import DeploymentProfile
from orca.runtime.run_modes import WorkflowRunMode

from tests.test_system_runtime import _build_simple_system


async def test_runtime_exposes_its_profile_store(tmp_path) -> None:
    profile_store = FileDeploymentProfileStore(tmp_path)
    system, _ = await _build_simple_system()
    rt = SystemRuntime(system, profile_store=profile_store)
    assert rt.profile_store is profile_store
    assert await rt.profile_store.list() == []


async def test_submit_workflow_with_profile_loads_into_variable_store(
    tmp_path,
) -> None:
    profile_store = FileDeploymentProfileStore(tmp_path)
    await profile_store.add(DeploymentProfile(
        name="dry_test",
        variables={"speed": 7777, "global.retries": 3},
    ))

    system, _ = await _build_simple_system()
    rt = SystemRuntime(system, profile_store=profile_store)
    await rt.start()
    try:
        rec = await rt.submit_workflow(
            "simple_workflow", deployment_profile="dry_test",
            mode=WorkflowRunMode.PURE_SIM,
        )
        store = system.variable_store
        # Bare-name landed in the execution partition.
        assert store.resolve("speed", rec.id) == 7777
        # global.-prefixed value also resolves.
        assert store.resolve("global.retries", rec.id) == 3
    finally:
        await rt.shutdown(confirm=True)


async def test_submit_workflow_without_profile_does_not_touch_store(
    tmp_path,
) -> None:
    profile_store = FileDeploymentProfileStore(tmp_path)
    await profile_store.add(DeploymentProfile(
        name="should_not_apply", variables={"speed": 1234},
    ))
    system, _ = await _build_simple_system()
    rt = SystemRuntime(system, profile_store=profile_store)
    await rt.start()
    try:
        rec = await rt.submit_workflow("simple_workflow", mode=WorkflowRunMode.PURE_SIM)
        store = system.variable_store
        from orca.variables.errors import UndefinedVariableError
        with pytest.raises(UndefinedVariableError):
            store.resolve("speed", rec.id)
    finally:
        await rt.shutdown(confirm=True)


async def test_submit_workflow_with_unknown_profile_raises(tmp_path) -> None:
    profile_store = FileDeploymentProfileStore(tmp_path)
    system, _ = await _build_simple_system()
    rt = SystemRuntime(system, profile_store=profile_store)
    await rt.start()
    try:
        with pytest.raises(KeyError, match="ghost"):
            await rt.submit_workflow(
                "simple_workflow", deployment_profile="ghost",
                mode=WorkflowRunMode.PURE_SIM,
            )
    finally:
        await rt.shutdown(confirm=True)


async def test_editing_profile_after_submit_does_not_affect_running_execution(
    tmp_path,
) -> None:
    profile_store = FileDeploymentProfileStore(tmp_path)
    await profile_store.add(DeploymentProfile(
        name="p", variables={"speed": 100},
    ))
    system, _ = await _build_simple_system()
    rt = SystemRuntime(system, profile_store=profile_store)
    await rt.start()
    try:
        rec = await rt.submit_workflow(
            "simple_workflow", deployment_profile="p",
            mode=WorkflowRunMode.PURE_SIM,
        )
        store = system.variable_store
        assert store.resolve("speed", rec.id) == 100

        # Mutate the profile in the store after submit.
        await profile_store.update(
            DeploymentProfile(name="p", variables={"speed": 999}),
        )
        # Consumed once at submit: the running execution keeps the submit-time
        # value (the variable store, not the profile registry, resolves it).
        assert store.resolve("speed", rec.id) == 100
    finally:
        await rt.shutdown(confirm=True)


async def test_profile_global_seeds_later_executions(tmp_path) -> None:
    """A profile's `global.`-prefixed value is poured into the global layer at
    submit, so it resolves for EVERY later execution -- not only the one that
    named the profile. Bare-name values stay scoped to the loading execution.

    The second submit uses `simple_workflow_b` (pad2) so its in-flight plate
    does not collide with the first execution's plate at pad1 on the
    start-location check.
    """
    profile_store = FileDeploymentProfileStore(tmp_path)
    await profile_store.add(DeploymentProfile(
        name="seed_globals",
        variables={"speed": 50, "global.retries": 3},
    ))
    system, _ = await _build_simple_system()
    rt = SystemRuntime(system, profile_store=profile_store)
    await rt.start()
    try:
        first = await rt.submit_workflow(
            "simple_workflow", deployment_profile="seed_globals",
            mode=WorkflowRunMode.PURE_SIM,
        )
        # A second execution that never names the profile.
        second = await rt.submit_workflow(
            "simple_workflow_b", mode=WorkflowRunMode.PURE_SIM,
        )
        store = system.variable_store

        # The global the first submit seeded resolves for the second execution.
        assert store.resolve("global.retries", second.id) == 3
        # The bare-name value stayed scoped to the first execution.
        assert store.resolve("speed", first.id) == 50
        from orca.variables.errors import UndefinedVariableError
        with pytest.raises(UndefinedVariableError):
            store.resolve("speed", second.id)
    finally:
        await rt.shutdown(confirm=True)

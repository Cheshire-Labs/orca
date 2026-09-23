"""Tests for DeploymentProfileFacade.

Mirrors the AccessConfigFacade / TeachpointFacade pattern: thin Protocol +
@dangerous concrete impl that wraps the IDeploymentProfileStore. The tests
exercise the facade directly against an in-memory store; the fact that
SystemRuntime exposes `runtime.profiles` returning this facade is covered
by an integration test.
"""

import pytest

from orca.runtime.danger import ConfirmationRequired
from orca.runtime.facades.profiles import DeploymentProfileFacade
from orca.runtime.profile_store import (
    FileDeploymentProfileStore,
    NullDeploymentProfileStore,
)
from orca.variables.deployment_profile import DeploymentProfile


def _profile(name: str = "p", **overrides: object) -> DeploymentProfile:
    base = {"name": name, "variables": {"speed": 100}}
    base.update(overrides)
    return DeploymentProfile.model_validate(base)


async def test_get_returns_value(tmp_path) -> None:
    store = FileDeploymentProfileStore(tmp_path)
    await store.add(_profile("p"))
    facade = DeploymentProfileFacade(store)
    loaded = await facade.get("p")
    assert loaded is not None
    assert loaded.name == "p"


async def test_get_returns_none_for_unknown(tmp_path) -> None:
    facade = DeploymentProfileFacade(FileDeploymentProfileStore(tmp_path))
    assert await facade.get("missing") is None


async def test_list_returns_all(tmp_path) -> None:
    store = FileDeploymentProfileStore(tmp_path)
    await store.add(_profile("a"))
    await store.add(_profile("b"))
    facade = DeploymentProfileFacade(store)
    names = sorted(p.name for p in await facade.list())
    assert names == ["a", "b"]


async def test_add_requires_confirm(tmp_path) -> None:
    facade = DeploymentProfileFacade(FileDeploymentProfileStore(tmp_path))
    with pytest.raises(ConfirmationRequired):
        await facade.add(_profile("p"))


async def test_add_with_confirm_persists(tmp_path) -> None:
    store = FileDeploymentProfileStore(tmp_path)
    facade = DeploymentProfileFacade(store)
    await facade.add(_profile("p"), confirm=True)
    assert await store.get("p") is not None


async def test_update_requires_confirm(tmp_path) -> None:
    store = FileDeploymentProfileStore(tmp_path)
    await store.add(_profile("p"))
    facade = DeploymentProfileFacade(store)
    with pytest.raises(ConfirmationRequired):
        await facade.update(_profile("p", description="updated"))


async def test_update_with_confirm_persists(tmp_path) -> None:
    store = FileDeploymentProfileStore(tmp_path)
    await store.add(_profile("p"))
    facade = DeploymentProfileFacade(store)
    await facade.update(_profile("p", description="updated"), confirm=True)
    loaded = await store.get("p")
    assert loaded is not None
    assert loaded.description == "updated"


async def test_delete_requires_confirm(tmp_path) -> None:
    store = FileDeploymentProfileStore(tmp_path)
    await store.add(_profile("p"))
    facade = DeploymentProfileFacade(store)
    with pytest.raises(ConfirmationRequired):
        await facade.delete("p")


async def test_delete_with_confirm_returns_true(tmp_path) -> None:
    store = FileDeploymentProfileStore(tmp_path)
    await store.add(_profile("p"))
    facade = DeploymentProfileFacade(store)
    assert await facade.delete("p", confirm=True) is True
    assert await store.get("p") is None


async def test_delete_unknown_returns_false(tmp_path) -> None:
    facade = DeploymentProfileFacade(FileDeploymentProfileStore(tmp_path))
    assert await facade.delete("missing", confirm=True) is False


async def test_facade_works_with_null_store() -> None:
    facade = DeploymentProfileFacade(NullDeploymentProfileStore())
    assert await facade.list() == []
    # Mutations on the null store are no-ops but still need confirm.
    await facade.add(_profile("p"), confirm=True)
    assert await facade.get("p") is None

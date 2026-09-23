"""DeploymentProfileFacade: external UI surface over IDeploymentProfileStore.

Reads pass through. Writes are `@dangerous` so CLI/REST/MCP all get uniform
confirmation prompts via the danger registry.

Editing a profile here does NOT affect executions that are already running.
The variable store consumed the prior profile body once at submit time, and
the variable store is the only resolver after submit. New bodies take
effect at the NEXT submission that names the profile.
"""

from orca.runtime.danger import DangerLevel, dangerous
from orca.runtime.interfaces import IDeploymentProfileStore
from orca.runtime.runtime_interface import IDeploymentProfileFacade
from orca.variables.deployment_profile import DeploymentProfile


_NEXT_SUBMIT_NOTE = (
    "Editing a deployment profile does not affect any execution that is "
    "already running. The new profile body is applied to executions "
    "submitted AFTER this edit."
)


class DeploymentProfileFacade(IDeploymentProfileFacade):
    """Concrete DeploymentProfileFacade implementation."""

    def __init__(self, store: IDeploymentProfileStore) -> None:
        self._store = store

    async def get(self, name: str) -> DeploymentProfile | None:
        return await self._store.get(name)

    async def list(self) -> list[DeploymentProfile]:
        return await self._store.list()

    @dangerous(
        name="profiles.add",
        level=DangerLevel.OPERATOR,
        message=(
            "Register deployment profile '{profile.name}'. "
            + _NEXT_SUBMIT_NOTE
        ),
    )
    async def add(self, profile: DeploymentProfile) -> None:
        await self._store.add(profile)

    @dangerous(
        name="profiles.update",
        level=DangerLevel.OPERATOR,
        message=(
            "Update deployment profile '{profile.name}'. "
            + _NEXT_SUBMIT_NOTE
        ),
    )
    async def update(self, profile: DeploymentProfile) -> None:
        await self._store.update(profile)

    @dangerous(
        name="profiles.delete",
        level=DangerLevel.OPERATOR,
        message=(
            "Delete deployment profile '{name}'. "
            + _NEXT_SUBMIT_NOTE
        ),
    )
    async def delete(self, name: str) -> bool:
        return await self._store.delete(name)

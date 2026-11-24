"""DeploymentProfileService: orchestration over an ``IDeploymentProfileStore`` under one lock."""

import asyncio
from typing import List, Optional, Protocol

from orca.resource_models.store_interfaces import IDeploymentProfileStore
from orca.variables.deployment_profile import DeploymentProfile


class IDeploymentProfileService(IDeploymentProfileStore, Protocol):
    """Deployment profiles, one at a time under one lock. It is used wherever a store is."""


class DeploymentProfileService:
    def __init__(self, store: IDeploymentProfileStore) -> None:
        self._store = store
        self._lock = asyncio.Lock()

    async def get(self, name: str) -> Optional[DeploymentProfile]:
        async with self._lock:
            return await self._store.get(name)

    async def list(self) -> List[DeploymentProfile]:
        async with self._lock:
            return await self._store.list()

    async def add(self, profile: DeploymentProfile) -> None:
        async with self._lock:
            await self._store.add(profile)

    async def update(self, profile: DeploymentProfile) -> None:
        async with self._lock:
            await self._store.update(profile)

    async def delete(self, name: str) -> bool:
        async with self._lock:
            return await self._store.delete(name)

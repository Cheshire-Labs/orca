"""Deployment profiles as files: `profiles/<name>.json` in the deployment package.

Every call reads the folder, so a hand edit is seen on the next read.
"""

import asyncio
import json
from pathlib import Path
from typing import List, Optional

from pydantic import ValidationError

from orca.runtime.setup_files import SetupFileError, file_in, read_setup_text, replace_file
from orca.variables.deployment_profile import DeploymentProfile


class FileDeploymentProfileStore:
    """One profile per ``<dir>/<name>.json``. A bad file is refused, naming it."""

    def __init__(self, directory: Path) -> None:
        self._dir = Path(directory)

    @property
    def directory(self) -> Path:
        return self._dir

    def _path_for(self, name: str) -> Path:
        return file_in(self._dir, name, "deployment profile")

    @staticmethod
    def _load(path: Path) -> DeploymentProfile:
        try:
            profile = DeploymentProfile.model_validate(json.loads(read_setup_text(path)))
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as exc:
            raise SetupFileError(path, str(exc)) from exc
        if profile.name != path.stem:
            raise SetupFileError(path, f"file name {path.stem!r} has name {profile.name!r}; make them match")
        return profile

    def _list_now(self) -> List[DeploymentProfile]:
        if not self._dir.is_dir():
            return []
        return [
            self._load(path) for path in sorted(self._dir.iterdir())
            if path.is_file() and path.suffix == ".json"
        ]

    def _get_now(self, name: str) -> Optional[DeploymentProfile]:
        path = self._path_for(name)
        return self._load(path) if path.is_file() else None

    async def get(self, name: str) -> Optional[DeploymentProfile]:
        return await asyncio.to_thread(self._get_now, name)

    async def list(self) -> List[DeploymentProfile]:
        return await asyncio.to_thread(self._list_now)

    async def _write(self, profile: DeploymentProfile, *, exists: bool) -> None:
        path = self._path_for(profile.name)
        if path.is_file() != exists:
            if exists:
                raise KeyError(f"DeploymentProfile {profile.name!r} not found")
            raise ValueError(f"DeploymentProfile {profile.name!r} already registered")
        text = json.dumps(profile.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        await asyncio.to_thread(replace_file, path, text)

    async def add(self, profile: DeploymentProfile) -> None:
        await self._write(profile, exists=False)

    async def update(self, profile: DeploymentProfile) -> None:
        await self._write(profile, exists=True)

    async def delete(self, name: str) -> bool:
        path = self._path_for(name)
        if not path.is_file():
            return False
        await asyncio.to_thread(path.unlink)
        return True

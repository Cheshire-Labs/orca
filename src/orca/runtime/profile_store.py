"""DeploymentProfile registry implementations.

Two source-available impls:

    NullDeploymentProfileStore -- no-op default; submissions never name a profile.
    FileDeploymentProfileStore -- directory-of-JSON files; one file per profile.

A hosted deployment ships `DbDeploymentProfileStore` in its own repo. There is exactly one
store per process; operator REST/MCP CRUD and submit-time resolution share
the same instance.

Naming note: file-backed instead of in-memory because deployment profiles
are operator artefacts that should survive process restarts even without a
hosted DB. The in-memory cache is rebuilt from disk at construction.
"""

import json
from pathlib import Path
from typing import List, Optional

from pydantic import ValidationError

from orca.variables.deployment_profile import DeploymentProfile


class NullDeploymentProfileStore:
    """No-op store. Default when no profile registry is configured."""

    async def get(self, name: str) -> Optional[DeploymentProfile]:
        return None

    async def list(self) -> List[DeploymentProfile]:
        return []

    async def add(self, profile: DeploymentProfile) -> None:
        pass

    async def update(self, profile: DeploymentProfile) -> None:
        pass

    async def delete(self, name: str) -> bool:
        return False


class FileDeploymentProfileStore:
    """Directory-of-JSONs registry.

    Each profile lives at ``<dir>/<profile.name>.json``. Construction loads
    every ``*.json`` file in the dir into an in-memory cache; mutations
    write through to disk and update the cache. Files written outside this
    process are NOT picked up at runtime - construct a fresh store to
    pick up out-of-band edits.

    Validation: every file is parsed through ``DeploymentProfile`` (which
    has ``extra="forbid"``), so a malformed file raises at construction
    rather than at first read. This makes operator typos fail loud at boot.
    """

    def __init__(self, directory: Path) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._by_name: dict[str, DeploymentProfile] = {}
        self._load_all()

    @property
    def directory(self) -> Path:
        return self._dir

    def _load_all(self) -> None:
        for path in sorted(self._dir.iterdir()):
            if not path.is_file() or path.suffix != ".json":
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                profile = DeploymentProfile.model_validate(data)
            except (json.JSONDecodeError, ValidationError) as exc:
                raise ValueError(
                    f"Failed to load deployment profile from {path}: {exc}",
                ) from exc
            self._by_name[profile.name] = profile

    def _path_for(self, name: str) -> Path:
        return self._dir / f"{name}.json"

    def _write(self, profile: DeploymentProfile) -> None:
        self._path_for(profile.name).write_text(
            profile.model_dump_json(indent=2),
            encoding="utf-8",
        )

    async def get(self, name: str) -> Optional[DeploymentProfile]:
        return self._by_name.get(name)

    async def list(self) -> List[DeploymentProfile]:
        return list(self._by_name.values())

    async def add(self, profile: DeploymentProfile) -> None:
        if profile.name in self._by_name:
            raise ValueError(
                f"DeploymentProfile {profile.name!r} already registered",
            )
        self._write(profile)
        self._by_name[profile.name] = profile

    async def update(self, profile: DeploymentProfile) -> None:
        if profile.name not in self._by_name:
            raise KeyError(f"DeploymentProfile {profile.name!r} not found")
        self._write(profile)
        self._by_name[profile.name] = profile

    async def delete(self, name: str) -> bool:
        if name not in self._by_name:
            return False
        path = self._path_for(name)
        if path.exists():
            path.unlink()
        del self._by_name[name]
        return True

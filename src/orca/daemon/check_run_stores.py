"""The framework keeps each check run's store as a SQLite file under the package's `.orca/check_runs/`."""

import asyncio
import shutil
import time
from pathlib import Path

from orca.daemon.activity_database import ActivityDatabase
from orca.runtime.check_runs import CheckRunEntry, ICheckRunStore

_DB = "state.db"
_EXECUTIONS = "execution_ids"
_CREATED = "created_ns"


def check_runs_folder(package_root: Path) -> Path:
    return package_root / ".orca" / "check_runs"


def _executions_of(folder: Path) -> tuple[str, ...]:
    recorded = folder / _EXECUTIONS
    if not recorded.is_file():
        return ()
    return tuple(line for line in recorded.read_text(encoding="utf-8").splitlines() if line)


class SqliteCheckRunStores:
    def __init__(self, folder: Path) -> None:
        self._folder = folder

    async def create(self, run_id: str) -> ICheckRunStore:
        folder = self._folder / run_id
        folder.mkdir(parents=True)
        (folder / _CREATED).write_text(str(time.time_ns()), encoding="utf-8")
        return await self._opened(folder)

    async def open(self, run_id: str) -> ICheckRunStore | None:
        folder = self._folder / run_id
        if not (folder / _DB).is_file():
            return None
        return await self._opened(folder)

    async def record_execution(self, run_id: str, execution_id: str) -> None:
        if execution_id in _executions_of(self._folder / run_id):
            return
        with (self._folder / run_id / _EXECUTIONS).open("a", encoding="utf-8") as recorded:
            recorded.write(execution_id + "\n")

    async def entries(self) -> list[CheckRunEntry]:
        if not self._folder.is_dir():
            return []
        found: list[tuple[int, CheckRunEntry]] = []
        for folder in self._folder.iterdir():
            created = folder / _CREATED
            if not created.is_file():
                continue
            found.append((
                int(created.read_text(encoding="utf-8")),
                CheckRunEntry(run_id=folder.name, execution_ids=_executions_of(folder)),
            ))
        return [entry for _, entry in sorted(found, key=lambda pair: pair[0])]

    async def delete(self, run_id: str) -> None:
        await asyncio.to_thread(shutil.rmtree, self._folder / run_id, True)

    @staticmethod
    async def _opened(folder: Path) -> ActivityDatabase:
        database = ActivityDatabase(folder / _DB)
        await database.open()
        return database

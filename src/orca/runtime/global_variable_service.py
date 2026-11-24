"""GlobalVariableService: reads and writes `global_variables.json` through its store, one write at a time."""

import asyncio
from typing import Protocol

from pydantic import ValidationError

from orca.variables.errors import OptionValue, VariableValidationError
from orca.variables.variable_definition import GlobalVariable


class IGlobalVariableStore(Protocol):
    """The declared globals and the values an operator set, keyed by bare name."""

    async def read(self) -> dict[str, GlobalVariable]: ...

    async def write(self, entries: dict[str, GlobalVariable]) -> None: ...


class IGlobalVariableService(Protocol):
    async def read(self) -> dict[str, GlobalVariable]: ...

    async def save_value(self, name: str, value: OptionValue) -> None:
        """Keep `value` beside the global's declaration. Raises VariableValidationError if it does not fit."""

    async def clear_value(self, name: str) -> None:
        """Drop the kept value; a declaration stays."""


class GlobalVariableService:
    def __init__(self, store: IGlobalVariableStore) -> None:
        self._store = store
        self._lock = asyncio.Lock()

    async def read(self) -> dict[str, GlobalVariable]:
        async with self._lock:
            return await self._store.read()

    async def save_value(self, name: str, value: OptionValue) -> None:
        async with self._lock:
            entries = await self._store.read()
            kept = entries[name].model_dump(exclude_unset=True) if name in entries else {}
            try:
                entries[name] = GlobalVariable.model_validate({**kept, "value": value})
            except ValidationError as refused:
                raise VariableValidationError(name, value, str(refused)) from refused
            await self._store.write(entries)

    async def clear_value(self, name: str) -> None:
        async with self._lock:
            entries = await self._store.read()
            entry = entries.get(name)
            if entry is None or entry.value is None:
                return
            kept = entry.model_dump(exclude_unset=True, exclude={"value"})
            if kept:
                entries[name] = GlobalVariable.model_validate(kept)
            else:
                del entries[name]
            await self._store.write(entries)

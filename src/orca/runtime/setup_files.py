"""One setup file in a deployment package: a JSON object of entries keyed by id.

The file is the store. Every read goes to disk and a write replaces the
whole file atomically, so a person editing by hand and Orca writing never leave
a half-written file. The entity's Service holds the lock.
"""

import asyncio
import json
import os
import shutil
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Generic, TypeVar

from pydantic import JsonValue, ValidationError

from orca.config import OrcaConfig

Entry = TypeVar("Entry")
_Result = TypeVar("_Result")


def is_plain_file_name(name: str) -> bool:
    """A name that stays inside its folder when used as `<name>.json`."""
    return bool(name) and Path(name).name == name and name not in {".", ".."}


def file_in(directory: Path, name: str, what: str) -> Path:
    """`<directory>/<name>.json`, refusing a name that would leave the folder."""
    if not is_plain_file_name(name):
        raise ValueError(f"{what} {name!r} cannot be a file name")
    return directory / f"{name}.json"


def _retrying_while_shared(act: Callable[[], _Result]) -> _Result:
    # On Windows a rename or a read fails while another handle has the file open.
    for attempt in range(1, 10):
        try:
            return act()
        except PermissionError:
            time.sleep(0.02 * attempt)
    return act()


def read_setup_text(path: Path) -> str:
    """The file's text. A missing file raises FileNotFoundError, as `read_text` does."""
    return _retrying_while_shared(lambda: path.read_text(encoding="utf-8"))


def replace_file(path: Path, text: str) -> None:
    """Write `text` through a temp file and a rename, so a crash leaves the old file whole."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        # mkstemp makes an owner-only file, and people edit these by hand.
        if path.exists():
            shutil.copymode(path, temp)
        else:
            os.chmod(temp, 0o644)
        _retrying_while_shared(lambda: os.replace(temp, path))
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


class SetupFileError(Exception):
    """A setup file that cannot be read as what it claims to hold."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


def read_system_config(path: Path) -> OrcaConfig:
    """The settings in `path`, or every default when the file does not exist."""
    try:
        text = read_setup_text(path)
    except FileNotFoundError:
        return OrcaConfig()
    try:
        return OrcaConfig.model_validate_json(text)
    except ValidationError as error:
        raise SetupFileError(path, str(error)) from error


class SetupEntryExistsError(ValueError):
    """An add whose key the file already has."""


class SetupFile(Generic[Entry]):
    def __init__(
        self,
        path: Path,
        decode: Callable[[JsonValue], Entry],
        encode: Callable[[Entry], JsonValue],
        name_of: Callable[[Entry], str] | None = None,
    ) -> None:
        self._path = Path(path).resolve()
        self._decode = decode
        self._encode = encode
        self._name_of = name_of

    @property
    def path(self) -> Path:
        return self._path

    async def read(self) -> dict[str, Entry]:
        return await asyncio.to_thread(self._read_now)

    async def write(self, entries: dict[str, Entry]) -> None:
        await asyncio.to_thread(self._write_now, entries)

    async def change(
        self, edit: Callable[[dict[str, Entry]], dict[str, Entry]],
    ) -> dict[str, Entry]:
        """Read, apply `edit`, and write. The entity's Service serializes these."""
        entries = edit(await asyncio.to_thread(self._read_now))
        await asyncio.to_thread(self._write_now, entries)
        return entries

    def _read_now(self) -> dict[str, Entry]:
        try:
            text = read_setup_text(self._path)
        except FileNotFoundError:
            return {}
        except UnicodeDecodeError as exc:
            raise SetupFileError(self._path, f"not UTF-8 text: {exc}") from exc
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SetupFileError(self._path, f"not valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise SetupFileError(self._path, "must be a JSON object keyed by id")
        entries: dict[str, Entry] = {}
        for key, value in raw.items():
            try:
                entries[key] = self._decode(value)
            except (ValidationError, ValueError, TypeError, KeyError) as exc:
                raise SetupFileError(self._path, f"entry {key!r}: {exc}") from exc
            if self._name_of is not None and self._name_of(entries[key]) != key:
                raise SetupFileError(
                    self._path, f"key {key!r} has name {self._name_of(entries[key])!r}; make them match",
                )
        return entries

    def _write_now(self, entries: dict[str, Entry]) -> None:
        text = json.dumps(
            {key: self._encode(entries[key]) for key in sorted(entries)},
            indent=2, sort_keys=True,
        ) + "\n"
        replace_file(self._path, text)

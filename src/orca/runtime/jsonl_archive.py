"""Locked append-only JSONL files under a directory, one per key.

Shared disk mechanics for JSONL-backed stores: ``<root>/<key>.jsonl`` with the
key path-separator-validated. Appends and whole-file reads serialize on one
lock; callers run them off the event loop via ``asyncio.to_thread``. The
archive is model-agnostic -- it moves lines of text, not records.
"""

import threading
from pathlib import Path
from typing import List


class JsonlArchive:
    """One ``<root>/<key>.jsonl`` file per key, append-only, lock-serialized."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def path_for(self, key: str) -> Path:
        # Reject path separators and the "."/".." dir entries; a bare ".."
        # substring (e.g. "plate..2") is a valid key, not a traversal.
        if "/" in key or "\\" in key or key in (".", ".."):
            raise ValueError(f"Invalid key for JSONL archive: {key!r}")
        return self._root / f"{key}.jsonl"

    def append_line(self, key: str, line: str) -> None:
        path = self.path_for(key)
        with self._lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def read_lines(self, key: str) -> List[str]:
        path = self.path_for(key)
        if not path.exists():
            return []
        with self._lock:
            with path.open("r", encoding="utf-8") as handle:
                return [line for line in handle.read().splitlines() if line]

    def keys(self) -> List[str]:
        """Every key with an archive file (the ``*.jsonl`` stems), sorted."""
        return sorted(path.stem for path in self._root.glob("*.jsonl"))

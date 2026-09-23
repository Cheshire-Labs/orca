"""Append-only JSONL IOpsHistoryStore: one file per execution.

The source-available default ops-history backing. Each TrackingRecord is appended as one
JSON line to a per-execution file in a shared JsonlArchive; the files ARE the
source of truth, so list/search scan them and reuse the shared record_matches.
A hosted deployment injects its own indexed store behind the same interface.
"""

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import List, Tuple

from pydantic import ValidationError
from typing_extensions import Self

from orca.state.records import TrackingRecord
from orca.runtime.jsonl_archive import JsonlArchive
from orca.state.ops_store import (
    IOpsHistoryView,
    OpsHistorySearchQuery,
    StoreBackedOpsHistoryView,
    record_matches,
)

logger = logging.getLogger(__name__)


class JsonlOpsHistoryStore:
    """Per-execution JSONL archive of TrackingRecords.

    Appends offload to a worker thread; reads scan the per-execution file in
    append order. search() returns cross-execution matches in ascending
    ``timestamp`` order -- the IOpsHistoryStore ordering contract every impl
    honors.
    """

    def __init__(self, ops_dir: Path) -> None:
        self._archive = JsonlArchive(ops_dir)
        self._tmp: tempfile.TemporaryDirectory | None = None

    @classmethod
    def ephemeral(cls) -> Self:
        """Source-available default: a throwaway temp dir, cleaned when the store is dropped.

        No less durable than the prior in-memory default; a configured data dir
        + clear-on-boot is the deferred shared file-backing step.
        """
        tmp = tempfile.TemporaryDirectory(prefix="orca_ops_history_")
        store = cls(Path(tmp.name))
        store._tmp = tmp
        return store

    async def append(self, execution_id: str, record: TrackingRecord) -> None:
        line = record.model_dump_json() + "\n"
        await asyncio.to_thread(self._archive.append_line, execution_id, line)

    def for_execution(self, execution_id: str) -> IOpsHistoryView:
        return StoreBackedOpsHistoryView(self, execution_id)

    async def list(self, execution_id: str) -> List[TrackingRecord]:
        lines = await asyncio.to_thread(self._archive.read_lines, execution_id)
        return self._parse(execution_id, lines)

    async def search(
        self, query: OpsHistorySearchQuery,
    ) -> List[Tuple[str, TrackingRecord]]:
        if query.execution_id is not None:
            keys = [query.execution_id]
        else:
            keys = self._archive.keys()
        matches: List[Tuple[str, TrackingRecord]] = []
        for key in keys:
            lines = await asyncio.to_thread(self._archive.read_lines, key)
            for record in self._parse(key, lines):
                if record_matches(record, query):
                    matches.append((key, record))
        matches.sort(key=lambda pair: pair[1].timestamp)
        return matches

    def _parse(self, key: str, lines: List[str]) -> List[TrackingRecord]:
        """Parse one file's lines, tolerating a single torn trailing line.

        Only the last line of an append-only file can be torn by an
        interrupted write, so a final unparseable line is skipped (logged); an
        unparseable line anywhere earlier is real corruption and re-raises.
        """
        records: List[TrackingRecord] = []
        for index, line in enumerate(lines):
            try:
                records.append(TrackingRecord.model_validate_json(line))
            except ValidationError:
                if index == len(lines) - 1:
                    logger.warning(
                        "Skipping truncated trailing ops-history line in archive %r",
                        key,
                    )
                    break
                raise
        return records

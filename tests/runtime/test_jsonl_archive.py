"""JsonlArchive: the shared, model-agnostic disk mechanics for JSONL stores.

Direct coverage for the helper that both orca's JsonlOpsHistoryStore and the
hosted DiskAndIndexOpsHistoryStore compose: append/read round-trip, key
listing, and the key validation (path separators + "."/".." dir entries
rejected, on both the write and read paths).
"""
from pathlib import Path

import pytest

from orca.runtime.jsonl_archive import JsonlArchive


def test_append_then_read_round_trips(tmp_path: Path) -> None:
    archive = JsonlArchive(tmp_path)
    archive.append_line("exec-1", "line-a\n")
    archive.append_line("exec-1", "line-b\n")
    assert archive.read_lines("exec-1") == ["line-a", "line-b"]


def test_read_missing_key_returns_empty(tmp_path: Path) -> None:
    assert JsonlArchive(tmp_path).read_lines("never-written") == []


def test_keys_lists_written_stems_sorted(tmp_path: Path) -> None:
    archive = JsonlArchive(tmp_path)
    archive.append_line("exec-2", "x\n")
    archive.append_line("exec-1", "y\n")
    assert archive.keys() == ["exec-1", "exec-2"]


@pytest.mark.parametrize("bad_key", ["a/b", "a\\b", "..", "."])
def test_append_rejects_separator_and_dot_dir_keys(
    tmp_path: Path, bad_key: str,
) -> None:
    archive = JsonlArchive(tmp_path)
    with pytest.raises(ValueError):
        archive.append_line(bad_key, "x\n")


@pytest.mark.parametrize("bad_key", ["a/b", "a\\b", "..", "."])
def test_read_rejects_separator_and_dot_dir_keys(
    tmp_path: Path, bad_key: str,
) -> None:
    archive = JsonlArchive(tmp_path)
    with pytest.raises(ValueError):
        archive.read_lines(bad_key)


def test_benign_double_dot_substring_is_a_valid_key(tmp_path: Path) -> None:
    """A bare '..' substring (no separator, not the '.'/'..' dir entry) is a
    valid key, not a traversal, so it must not be rejected."""
    archive = JsonlArchive(tmp_path)
    archive.append_line("plate..2", "x\n")
    assert archive.read_lines("plate..2") == ["x"]
    assert "plate..2" in archive.keys()

"""What a head carries has one owner, `MountedTipsLedger`, so only it writes that a head went unobserved.

Read as text (`cheshire_source_text`), because the repo bans parsing source into a syntax tree. Positive and
negative controls included: a scan over a clean tree passes whether or not it works.
"""

from pathlib import Path

from cheshire_source_text import code_lines_of

SRC = Path(__file__).resolve().parents[1] / "src" / "orca"
OWNER = SRC / "state" / "mounted.py"
WRITE = ".append_head_observation_gap("


def _writers_in(paths: list[Path]) -> list[str]:
    return [
        f"{path}:{line.lineno}"
        for path in paths
        for line in code_lines_of(path)
        if WRITE in line.text
    ]


def test_only_the_mounted_tips_ledger_writes_a_head_gap() -> None:
    others = [path for path in SRC.rglob("*.py") if path != OWNER]
    assert _writers_in(others) == []
    assert _writers_in([OWNER]) != []


def test_the_scan_finds_a_call_and_ignores_the_name_in_a_string(tmp_path: Path) -> None:
    calls = tmp_path / "calls.py"
    calls.write_text("async def f(history):\n    await history.append_head_observation_gap('lh', cause)\n")
    mentions = tmp_path / "mentions.py"
    mentions.write_text("NOTE = 'history.append_head_observation_gap(lh)'\n# history.append_head_observation_gap(lh)\n")

    assert _writers_in([calls]) == [f"{calls}:2"]
    assert _writers_in([mentions]) == []

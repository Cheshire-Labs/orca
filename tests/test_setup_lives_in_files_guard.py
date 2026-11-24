"""Guard: setup lives in the package's files, activity in the database.

Fails on a database store for a setup entity, on a seed, on setup declared in a
topology.py, and on a topology.py that reads a file itself. Source is read as
text through cheshire_source_text, so strings and comments never match.
"""

import re
from pathlib import Path

from cheshire_source_text import code_lines, python_files

_REPO = Path(__file__).resolve().parents[1]
_SKIPPED = {"tests", "bench"}

_SETUP_ENTITIES = (
    "teachpoint", "decklayout", "accessconfig", "movedefaults",
    "gripprofile", "labwarecatalog", "deploymentprofile",
)
_DB_STORE_CLASS = re.compile(r"^class\s+((?:Sqlite|Postgres|Db)\w*Store)\b")
_SEED = re.compile(r"\bseed\s*=|\bapply_seeds\b")
_SETUP_IN_TOPOLOGY = re.compile(r"\b(?:Teachpoint|DeckLayoutConfig|AccessConfig)\s*\(")
_FILE_IN_TOPOLOGY = re.compile(r"\b__file__\b|\bPath\s*\(|\bopen\s*\(")


def violations(source: str, *, is_topology: bool) -> list[str]:
    found: list[str] = []
    for line in code_lines(source):
        store = _DB_STORE_CLASS.match(line.text)
        if store and any(e in store.group(1).lower() for e in _SETUP_ENTITIES):
            found.append(f"{line.lineno}: a database store for setup: {store.group(1)}")
        if _SEED.search(line.text):
            found.append(f"{line.lineno}: a seed; setup is written to the package's files")
        if is_topology and _SETUP_IN_TOPOLOGY.search(line.text):
            found.append(f"{line.lineno}: setup declared in topology.py; it belongs in a JSON file")
        if is_topology and _FILE_IN_TOPOLOGY.search(line.text):
            found.append(f"{line.lineno}: topology.py reads a file; the build attaches setup files")
    return found


def _is_a_package_topology(path: Path) -> bool:
    """A deployment package's topology.py. The engine's own `orca/cli/topology.py` is a CLI verb."""
    return path.name == "topology.py" and _REPO / "src" / "orca" not in path.parents


def _scanned() -> list[Path]:
    return [
        path for path in python_files(_REPO)
        if not _SKIPPED & set(path.relative_to(_REPO).parts)
    ]


def test_the_repo_keeps_setup_in_files() -> None:
    problems = []
    for path in _scanned():
        for found in violations(path.read_text(encoding="utf-8"), is_topology=_is_a_package_topology(path)):
            problems.append(f"{path.relative_to(_REPO)}:{found}")
    assert not problems, "setup is not only in files, or activity is not only in the database:\n" + "\n".join(problems)


def test_the_scan_covers_the_examples_and_the_engine() -> None:
    scanned = {path.relative_to(_REPO).as_posix() for path in _scanned()}
    assert "examples/smc_assay/topology.py" in scanned
    assert "src/orca/runtime/store_factory.py" in scanned
    assert not any(p.startswith("tests/") for p in scanned)
    assert _is_a_package_topology(_REPO / "examples" / "smc_assay" / "topology.py")
    assert not _is_a_package_topology(_REPO / "src" / "orca" / "cli" / "topology.py")


def test_each_rule_fires_on_the_shape_it_names() -> None:
    assert violations("class SqliteTeachpointStore:\n    pass\n", is_topology=False)
    assert violations("class PostgresGripProfileStore:\n    pass\n", is_topology=False)
    assert violations("class DbDeckLayoutStore:\n    pass\n", is_topology=False)
    assert violations("stores.teachpoints('arm', seed=[tp])\n", is_topology=False)
    assert violations("await stores.apply_seeds()\n", is_topology=False)
    assert violations("tp = Teachpoint('pad_1')\n", is_topology=True)
    assert violations("lh = DeckLayoutConfig(deck_type='STAR')\n", is_topology=True)
    assert violations("a = AccessConfig(name='v', access_type='vertical')\n", is_topology=True)
    assert violations("arm = Arm(str(Path(__file__).parent / 'tp.json'))\n", is_topology=True)


def test_no_rule_fires_on_what_is_allowed() -> None:
    assert not violations("class SqliteIncidentStore:\n    pass\n", is_topology=False)
    assert not violations("class SqlLabwareStore:\n    pass\n", is_topology=False)
    assert not violations("class TeachpointFileStore:\n    pass\n", is_topology=False)
    assert not violations("random.seed(4)\nrng = Random(seed)\n", is_topology=False)
    assert not violations("# seed=[...] used to live here\nx = 'apply_seeds'\n", is_topology=False)
    assert not violations("tp = Teachpoint('pad_1')\n", is_topology=False)
    assert not violations("arm = Transporter('arm')\n", is_topology=True)

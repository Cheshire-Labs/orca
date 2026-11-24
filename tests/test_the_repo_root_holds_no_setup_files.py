"""Guard: the repo root is not a package folder, so it holds no setup files and no activity database.

A daemon or a test builder run with the repo root as its package folder writes them there, and a public
cut would publish them. Tests get a temp package folder from the autouse fixture in `conftest.py`.
"""

from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]

_SETUP_NAMES = (
    "access_configs.json", "move_defaults.json", "grip_profiles.json",
    "teachpoints", "deck_layouts", "devices", "state.db",
)


def setup_files_in(folder: Path) -> list[str]:
    return sorted(name for name in _SETUP_NAMES if (folder / name).exists())


def test_the_repo_root_holds_no_setup_files() -> None:
    found = setup_files_in(_REPO)

    assert found == [], (
        f"{found} at the repo root. They are a package folder's files: something ran with the repo root as "
        f"its package folder. Delete them, and run it in a package folder or with ORCA_PACKAGE_DIR set."
    )


def test_the_guard_finds_each_setup_file_and_folder(tmp_path: Path) -> None:
    for name in _SETUP_NAMES:
        if name.endswith((".json", ".db")):
            (tmp_path / name).write_text("{}", encoding="utf-8")
        else:
            (tmp_path / name).mkdir()

    assert setup_files_in(tmp_path) == sorted(_SETUP_NAMES)


def test_the_guard_passes_a_folder_with_only_a_repos_own_files(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    (tmp_path / "examples" / "teachpoints").mkdir(parents=True)

    assert setup_files_in(tmp_path) == []

"""The `orca` package carries a `py.typed` marker, so type checkers read its annotations.

setuptools ships `py.typed` only from inside a package directory. The marker
sat at `src/py.typed`, beside the package, so no wheel carried it and every
consumer saw orca as untyped.
"""

from pathlib import Path

import orca


def test_py_typed_sits_inside_the_orca_package() -> None:
    assert (Path(orca.__file__).parent / "py.typed").is_file()


def test_the_wheel_is_told_to_ship_py_typed() -> None:
    """An editable install reads the source tree, so only package-data puts the marker in a wheel."""
    pyproject = (Path(orca.__file__).parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    # Read as text: tomllib needs Python 3.11, and the package supports 3.10.
    table = pyproject.split("[tool.setuptools.package-data]", 1)[1].split("\n[", 1)[0]
    assert "py.typed" in table

"""A server imports its package folder afresh on each build."""

import sys
from pathlib import Path


class PackageLoadError(Exception):
    """The package folder did not import, build or start. The message names the cause."""

    @property
    def cause(self) -> BaseException:
        """The error the package raised, or this error when nothing else did."""
        return self.__cause__ or self


def _belongs_to(module_name: str, package_dir: Path) -> bool:
    module = sys.modules[module_name]
    folders = [Path(p).resolve() for p in getattr(module, "__path__", [])]
    file = getattr(module, "__file__", None)
    if file is not None:
        folders.append(Path(file).resolve().parent)
    return any(folder == package_dir or package_dir in folder.parents for folder in folders)


class PackageImport:
    """Makes the package folder importable under its own name, and forgets it again.

    Each build imports every module afresh, so an edited file takes effect on the next build.
    """

    def __init__(self, package_dir: Path) -> None:
        self._dir = package_dir.resolve()
        self.name = self._dir.name
        self._path_entry = str(self._dir.parent)
        self._added_path = False

    def open(self) -> None:
        if self.name in sys.modules and not _belongs_to(self.name, self._dir):
            raise PackageLoadError(
                f"the package folder is named {self.name!r}, which is already an imported "
                "module. Rename the folder.",
            )
        self._forget_modules()
        if self._path_entry not in sys.path:
            sys.path.insert(0, self._path_entry)
            self._added_path = True

    def close(self) -> None:
        self._forget_modules()
        if self._added_path and self._path_entry in sys.path:
            sys.path.remove(self._path_entry)
        self._added_path = False

    def _forget_modules(self) -> None:
        # Decide every name first: a namespace package reads its parent to list its own folders.
        names = [
            m for m in sys.modules
            if (m == self.name or m.startswith(f"{self.name}.")) and _belongs_to(m, self._dir)
        ]
        for module_name in names:
            del sys.modules[module_name]

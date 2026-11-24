"""The cases of a surface module build on each other, so each one checks that the cases before it ran first."""

from collections.abc import Sequence


class InOrder:
    def __init__(self, worlds: Sequence[str]) -> None:
        self._worlds = list(worlds)
        self._ran: set[int] = set()

    def start(self, index: int) -> None:
        world = self._worlds[index]
        missing = [i for i in range(index) if self._worlds[i] == world and i not in self._ran]
        self._ran.add(index)
        assert not missing, (
            f"case {index} needs the cases before it in the {world!r} world to run first in this process, "
            f"and {len(missing)} did not (first: case {missing[0]}). Run the whole module, on one worker."
        )

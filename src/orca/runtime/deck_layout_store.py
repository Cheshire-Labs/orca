"""Null deck-layout store: the deckless-device default.

``NullDeckLayoutStore`` is the no-op null object a LiquidHandler uses when
no deck store is supplied. Real persistence lives in
``SqliteDeckLayoutStore`` (source-available) behind ``DeckLayoutService``; a hosted
deployment injects a DB-backed store from its own repo. There is exactly one store per
LiquidHandler. Edits take effect on the next RuntimeLifecycle.rebuild()
because deck reconfiguration during an active run is unsafe (physical
labware cannot reposition mid-motion).
"""

from typing import List, Optional, Tuple

from cheshire_drivers.liquid_handler_models import DeckLayoutConfig


class NullDeckLayoutStore:
    """No-op store. Default when a liquid handler has no configured deck."""

    async def get(self, name: str) -> Optional[DeckLayoutConfig]:
        return None

    async def list(self) -> List[Tuple[str, DeckLayoutConfig]]:
        return []

    async def add(self, name: str, config: DeckLayoutConfig) -> None:
        pass

    async def update(self, name: str, config: DeckLayoutConfig) -> None:
        pass

    async def delete(self, name: str) -> bool:
        return False

    async def create_schema(self) -> None:
        pass

    async def aclose(self) -> None:
        pass

"""DB-neutral mapping between ``DeckLayoutConfig`` and ``DeckLayoutRow``.

Lives in orca-core; the per-DB deck-layout stores reuse it. The config is
persisted as one JSON blob (``model_dump``) and reconstructed via
``model_validate``, so there is no per-field flattening.
"""

from typing import Tuple

from cheshire_drivers.liquid_handler_models import DeckLayoutConfig

from orca.runtime.db.models import DeckLayoutRow


def deck_layout_to_row(name: str, config: DeckLayoutConfig) -> DeckLayoutRow:
    return DeckLayoutRow(name=name, deck_data=config.model_dump())


def apply_deck_layout_to_row(row: DeckLayoutRow, config: DeckLayoutConfig) -> None:
    """Overwrite a row's config from a layout (update path; PK fixed)."""
    row.deck_data = config.model_dump()


def row_to_deck_layout(row: DeckLayoutRow) -> Tuple[str, DeckLayoutConfig]:
    return row.name, DeckLayoutConfig.model_validate(row.deck_data)

"""Labware an operator introduced by naming a catalog definition.

Everything the engine keys off a labware reaches it through a template: the PLR
object, the deck ``catalog_ref``, the grip profile, the restore path after a
restart. A deployment package declares templates in code, so labware its author
never anticipated has no template and cannot be placed at all.

An ad-hoc template closes that gap. It is *derived from the catalog row*, never
declared and never stored: the template name is a pure function of the labware
type, and every persisted instance row already carries its ``labware_type``, so
a rebuild re-derives a byte-identical template instead of finding a dangling
name. That is what keeps the ledger and the driver deck agreeing across a
restart, which a template injected into the live system alone would not.
"""

from typing import Callable

from orca.resource_models.labware import (
    LabwareTemplate,
    PlateTemplate,
    TipRackTemplate,
    TroughTemplate,
    TubeRackTemplate,
)
from orca.runtime.labware_factory_resolver import resolve_plr_factory
from orca.runtime.labware_catalog_protocol import ILabwareCatalog, LabwareDefinition

ADHOC_TEMPLATE_PREFIX = "adhoc__"
"""Marks a template as derived rather than declared. Plain identifier
characters because the template name becomes the PLR object's name and the
driver-world deck key."""


class UnplaceableLabwareCategory(ValueError):
    """The catalog knows this labware type, but nothing can hold an instance of it.

    A carrier is deck furniture the layout declares, not something an operator
    registers, so there is no template class for it and no instance to mint.
    """


class LabwareHasNoModel(ValueError):
    """The catalog knows this labware type, but nothing can build one.

    A catalog row carries geometry; making an instance needs a PLR factory that
    cheshire-drivers exposes for that type, and most of the seeded rows have
    none. Raised before anything is registered or placed, so the operator gets
    a named refusal rather than a 500 with the reason thrown away.
    """


# A hand-placed tip rack is a fresh full one, the same assumption the contents
# ledger already makes when a driver asks for a layout nobody has written.
# `set-tip-state` is the correction when it is not.
_BUILDERS: dict[str, Callable[[str, str], LabwareTemplate]] = {
    "plate": lambda name, labware_type: PlateTemplate(name, labware_type),
    "tip_rack": lambda name, labware_type: TipRackTemplate(
        name, labware_type, with_tips=True,
    ),
    "trough": lambda name, labware_type: TroughTemplate(name, labware_type),
    "tube": lambda name, labware_type: TubeRackTemplate(name, labware_type),
}


def adhoc_template_name(labware_type: str) -> str:
    """The template name a given catalog labware type always derives to."""
    return f"{ADHOC_TEMPLATE_PREFIX}{labware_type}"


def is_adhoc_template_name(name: str) -> bool:
    """Whether this template name was derived from a catalog type.

    The reverse mapping is deliberately not offered: a persisted instance
    carries its own ``labware_type``, so rebuilding one reads the row rather
    than parsing the name back apart.
    """
    return name.startswith(ADHOC_TEMPLATE_PREFIX)


async def build_adhoc_template(
    catalog: ILabwareCatalog, labware_type: str,
) -> LabwareTemplate:
    """The template for a catalog labware type, built and bound to the catalog.

    Raises ``LabwareNotFound`` for a type the catalog does not carry and
    ``UnplaceableLabwareCategory`` for one no template class can hold.
    """
    definition = await catalog.get(labware_type)
    build = _BUILDERS.get(definition.category)
    if build is None:
        raise UnplaceableLabwareCategory(
            f"labware type {labware_type!r} is category {definition.category!r}, "
            f"which is not something an operator places; placeable categories "
            f"are {sorted(_BUILDERS)}."
        )
    # Before the template exists, so a type nothing can build leaves nothing
    # registered behind it.
    refuse_unbuildable(definition)
    template = build(adhoc_template_name(labware_type), labware_type)
    await template.bind_catalog(catalog)
    return template


def refuse_unbuildable(definition: LabwareDefinition) -> None:
    """Refuse a catalog row no PLR factory can turn into an instance.

    Most seeded rows have no wrapped factory, so this is the common answer, not
    an edge case. It has to be a named refusal: the resolver raises RuntimeError
    and NotImplementedError, which every surface above shapes into a 500 with
    the reason dropped.
    """
    try:
        resolve_plr_factory(definition)
    except (RuntimeError, NotImplementedError) as exc:
        raise LabwareHasNoModel(
            f"labware type {definition.labware_type!r} is in the catalog but "
            f"cheshire-drivers exposes no model for it, so no instance can be "
            f"made: {exc}"
        ) from exc

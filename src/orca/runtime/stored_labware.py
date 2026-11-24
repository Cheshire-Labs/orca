"""Turn a labware the store handed back into one the engine holds, with its facts entered."""

import logging

from orca.resource_models.adhoc_labware import is_adhoc_template_name
from orca.resource_models.labware import LabwareInstance
from orca.system.system_interface import ISystem

logger = logging.getLogger(__name__)


async def adopt_stored_labware(system: ISystem, stored: LabwareInstance) -> LabwareInstance:
    """A database store builds a new object on every read, so its copy has no template and no record binding.

    Call this before anything places the labware.
    """
    await _ensure_derived_template(system, stored)
    instance = await _restore_persisted_instance(system, stored)
    # Writes an opening entry only if the record has none, so a restart cannot refill a consumed rack.
    await instance.enter_record(system.labware_contents)
    # Registered after its facts, so anything that finds it in the system finds its facts, and
    # before it is placed, so clear_all (iterates system.labwares) can reach its slot.
    system.add_labware(instance)
    return instance


async def _ensure_derived_template(system: ISystem, stored: LabwareInstance) -> None:
    # An ad-hoc template is derived from the catalog, so a rebuilt system starts without one. The PLR rebuild
    # and the deck projection's catalog_ref both need it first.
    if not is_adhoc_template_name(stored.template_name):
        return
    try:
        await system.ensure_adhoc_labware_template(stored.labware_type)
    except Exception:
        logger.warning(
            "Labware %s names derived template %r, which could not be rebuilt "
            "from labware type %r; the deck cannot model it",
            stored.name, stored.template_name, stored.labware_type,
            exc_info=True,
        )


async def _restore_persisted_instance(system: ISystem, stored: LabwareInstance) -> LabwareInstance:
    # Falling back to the identity-only labware keeps a deployment that boots: its deck is modelled from the
    # driver's defaults, which is wrong but recoverable, where a raised error takes every route to 503.
    if stored.has_plr_backing:
        return stored
    try:
        template = system.get_labware_template(stored.template_name)
    except KeyError:
        logger.warning(
            "Labware %s names template %r, which this system does not declare; "
            "leaving it identity-only",
            stored.name, stored.template_name,
        )
        return stored
    try:
        return await template.restore_instance(stored)
    except Exception:
        logger.exception(
            "Could not rebuild labware %s from template %r; leaving it identity-only",
            stored.name, stored.template_name,
        )
        return stored

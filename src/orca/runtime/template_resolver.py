"""TemplateResolver for resolving name strings to live Python objects.

Used during workflow deserialization to convert string references
(pool names, labware names, location names) back to objects.
Implements the TemplateResolver protocol from serialization.py.
"""

from orca.resource_models.labware import AnyLabwareTemplate, LabwareTemplate
from orca.resource_models.location import Location
from orca.resource_models.resource_pool import ResourcePool


class LoaderTemplateResolver:
    """Resolves serialized name strings to live objects during workflow loading.

    Populated by SystemLoader during the build process, then passed
    to workflow_from_dict() for deserialization.
    """

    def __init__(
        self,
        pools: dict[str, ResourcePool],
        labware_templates: dict[str, LabwareTemplate],
        locations: dict[str, Location],
    ) -> None:
        self._pools = pools
        self._labware_templates = labware_templates
        self._locations = locations

    def resolve_resource_pool(self, name: str) -> ResourcePool:
        pool = self._pools.get(name)
        if pool is None:
            raise ValueError(
                f"Unknown resource pool '{name}'. "
                f"Available: {', '.join(sorted(self._pools.keys()))}"
            )
        return pool

    def resolve_labware_template(self, name: str) -> LabwareTemplate | AnyLabwareTemplate:
        if name == "$any":
            return AnyLabwareTemplate()
        template = self._labware_templates.get(name)
        if template is None:
            raise ValueError(
                f"Unknown labware template '{name}'. "
                f"Available: {', '.join(sorted(self._labware_templates.keys()))}"
            )
        return template

    def resolve_location(self, name: str) -> Location:
        location = self._locations.get(name)
        if location is None:
            raise ValueError(
                f"Unknown location '{name}'. "
                f"Available: {', '.join(sorted(self._locations.keys()))}"
            )
        return location

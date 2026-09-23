"""Resolve entities by name, id prefix, or full id.

Used by plugins, CLI, and operations to look up threads, methods,
and other entities from their registries. Supports the ``<name>-<id
prefix>`` display-name convention (see ``instance_name_for``).
"""

from typing import Dict, Optional


def resolve_entity(query: str, registry: Dict[str, str]) -> Optional[str]:
    """Find an entity id from a registry of {id: name} mappings.

    Matches against (in order):
        1. Exact id match
        2. Exact name match (returns first match)
        3. Id prefix match (returns first match)
        4. Name prefix match (raises ValueError if multiple names match)

    Returns the entity id or None if no match found.
    Raises ValueError if the name prefix matches multiple entities.
    """
    if query in registry:
        return query
    for entity_id, name in registry.items():
        if name == query or entity_id.startswith(query):
            return entity_id
    matches = [eid for eid, name in registry.items() if name.startswith(query)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = [registry[eid] for eid in matches]
        raise ValueError(f"Ambiguous match for '{query}': {', '.join(names)}")
    return None

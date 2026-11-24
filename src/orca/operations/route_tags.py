"""OpenAPI tag vocabulary for the daemon REST surface.

`(str, Enum)` so each member's ``.value`` is the kebab-case string the
website's OpenAPI docs pipeline groups by, while route declarations
reference a typed member instead of a bare string literal.
"""

from enum import Enum


class RouteTag(str, Enum):
    LIFECYCLE = "lifecycle"
    EXECUTIONS = "executions"
    THREADS = "threads"
    MANUAL_STEPS = "manual-steps"
    RESERVATIONS = "reservations"
    AUDIT = "audit"
    VARIABLES = "variables"
    PROFILES = "profiles"
    DEVICES = "devices"
    TRANSPORTERS = "transporters"
    LIQUID_HANDLERS = "liquid-handlers"
    PLUGINS = "plugins"
    ACCESS_CONFIGS = "access-configs"
    MOVE_DEFAULTS = "move-defaults"
    GRIP_PROFILES = "grip-profiles"
    TEACHPOINTS = "teachpoints"
    DECK_LAYOUTS = "deck-layouts"
    INCIDENTS = "incidents"
    INTERVENTIONS = "interventions"
    SYSTEM = "system"
    CATALOG = "catalog"
    EVENTS = "events"
    SUBMISSIONS = "submissions"
    LABWARE = "labware"
    OPS_HISTORY = "ops-history"
    RUNTIME = "runtime"
    TOPOLOGY = "topology"

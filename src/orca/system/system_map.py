from abc import ABC, abstractmethod
import itertools
import logging
from typing import Dict, List, Mapping, Optional, Set, Tuple, TypedDict

from typing_extensions import Self
from orca.resource_models.labware_placeable_interface import ILabwarePlaceable
from orca.resource_models.resources import IResource
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.deck_site_location import DeckSiteLocation
from orca.resource_models.location import IResourceLocationObserver, Location
import networkx as nx
import matplotlib.pyplot as plt

from orca.resource_models.plate_pad import PlatePad

orca_logger = logging.getLogger("orca")
from orca.resource_models.transporter import Transporter
from orca.resource_models.transporter_base import TransporterBase
from orca.system.resource_registry import IResourceRegistry
from orca.system.resource_registry import IResourceRegistryObserver

class NodeData(TypedDict):
    location: Location

class EdgeData(TypedDict):
    transporter: TransporterBase
    weight: float

class IResourceLocator(ABC):
    def get_resource_location(self, resource_name: str) -> Location:
        raise NotImplementedError

class _NetworkXHandler:
    
    def __init__(self, graph: Optional[nx.DiGraph] = None) -> None:
        if graph is None:
            graph = nx.DiGraph()
        self._graph: nx.DiGraph = graph

    def add_node(self, name: str, location: Location) -> None:
        self._graph.add_node(name, location=location)

    def add_edge(self, start: str, end: str, transporter: TransporterBase, weight: float = 1.0) -> None:
        self._graph.add_edge(start, end, weight=weight, transporter=transporter)

    def has_path(self, source: str, target: str) -> bool:
        return nx.has_path(self._graph, source, target)

    def get_nodes(self) -> Dict[str, NodeData]:
        return {name: NodeData(location=data["location"]) for name, data in self._graph.nodes.items()}

    def get_node_data(self, name: str) -> NodeData:
        raw = self._graph.nodes[name]
        return NodeData(location=raw["location"])

    def has_node(self, name: str) -> bool:
        return self._graph.has_node(name)

    def has_edge(self, source: str, target: str) -> bool:
        return self._graph.has_edge(source, target)

    def get_shortest_path(self, source: str, target: str) -> List[str]:
        path: List[str] = nx.shortest_path(self._graph, source, target, weight='weight')
        return path

    def get_all_shortest_paths(self, source: str, target: str) -> List[List[str]]:
        return list(nx.all_shortest_paths(self._graph, source, target, weight='weight'))

    def get_all_simple_paths(self, source: str, target: str) -> List[List[str]]:
        return list(nx.all_simple_paths(self._graph, source, target))

    def get_subgraph(self, nodes: List[str]) -> Self:
        return type(self)(nx.subgraph(self._graph, nodes))

    def get_path_graph(self, path: List[str]) -> Self:
        return type(self)(nx.path_graph(self._graph, path))

    def get_all_edges(self) -> List[Tuple[str, str, EdgeData]]:
        return [
            (source, target, EdgeData(transporter=data["transporter"], weight=data["weight"]))
            for source, target, data in self._graph.edges(data=True)
        ]

    def get_edge_data(self, source: str, target: str) -> EdgeData:
        raw = self._graph.edges[source, target]
        return EdgeData(transporter=raw["transporter"], weight=raw["weight"])

    def set_edge_weight(self, source: str, target: str, weight: float) -> None:
        self._graph[source][target]['weight'] = weight

    def get_distance(self, source: str, target: str) -> float:
        return nx.shortest_path_length(self._graph, source, target, weight='weight')

    def draw(self) -> None:
        pos = nx.spring_layout(self._graph)
        nx.draw(self._graph, pos=pos, with_labels=True)
        plt.show()

    def __getitem__(self, key: str) -> NodeData:
        raw = self._graph.nodes[key]
        return NodeData(location=raw["location"])


class ILocationRegistry(ABC):
    @property
    @abstractmethod
    def locations(self) -> List[Location]:
        raise NotImplementedError

    @abstractmethod
    def get_location(self, name: str) -> Location:
        raise NotImplementedError

    @abstractmethod
    def add_location(self, location: Location):
        raise NotImplementedError

    def sites_of(self, mutex_key: str) -> List[Location]:
        """The flat site nodes owned by the device whose mutex is ``mutex_key``."""
        return [
            location for location in self.locations
            if location.owner_mutex_id == mutex_key
        ]
    

class RouteGraphMissingTeachpointError(nx.NetworkXNoPath):
    """No route, and both ends are real positions the system knows.

    Subclasses NetworkXNoPath so the callers that already treat "no route" as
    an answer keep working; what it adds is the reason an operator hits this
    right after successfully teaching the position they are told is
    unreachable.
    """


class SystemMap(ILocationRegistry, IResourceLocator, IResourceLocationObserver, IResourceRegistryObserver):
    """ SystemMap is a representation of the system's locations and their connections."""
    def __init__(self, resource_registry: IResourceRegistry) -> None:
        """Initialize the SystemMap with a resource registry
        Args:
            resource_registry (IResourceRegistry): The resource registry that contains the resources and transporters.
        """
        self._graph: _NetworkXHandler = _NetworkXHandler()
        self._equipment_map: Dict[str, Location] = {}
        self._mutex_locations: Dict[str, Location] = {}
        self._teachpoint_aliases: Dict[str, List[str]] = {}
        self._added_transporters: set[str] = set()
        self._transporter_positions: Dict[str, List[str]] = {}
        self._exclusion_groups: Dict[str, frozenset[str]] = {}
        self._exclusion_owner: Dict[str, str] = {}
        self._resource_registry = resource_registry
        self._resource_registry.add_observer(self)

    @property
    def locations(self) -> List[Location]:
        return [nodedata["location"] for _, nodedata in self._graph.get_nodes().items()]

    def get_location(self, name: str) -> Location:
        if self.location_exists(name):
            return self._graph.get_node_data(name)["location"]
        # Device mutex keys resolve for the reservation layer but are
        # never routing nodes.
        mutex = self._mutex_locations.get(name)
        if mutex is not None:
            return mutex
        # A DeckResourceConfig.name or PlateTemplate.name mistaken for a site
        # lands here; flat deck sites ARE graph nodes, so they list below.
        configured = sorted(
            nd["location"].position_id
            for _, nd in self._graph.get_nodes().items()
        )
        raise KeyError(
            f"Topology site {name!r} not found in system map. "
            f"Configured sites: {configured!r}. "
            f"(A site is a Topology.locations key or a flat deck-site node -- the "
            f"thing `@orca.thread(start=..., end=...)` consumes. A specific deck "
            f"site is addressable as '<device>/<carrier>-<site_index>' "
            f"(e.g. 'lh/carrier-25-0'). DeckResourceConfig.name values "
            f"(deck slots), PlateTemplate.name values (template labels), and "
            f"labware instance names ('<template>-<id>', the driver deck key) "
            f"are DIFFERENT NAMESPACES and cannot be used here.)"
        )

    async def add_location(self, location: Location) -> None:
        gripper = self.find_gripper_location(location.position_id)
        if gripper is not None and gripper is not location:
            raise ValueError(
                f"{location.position_id!r} is already the gripper of a mover. Two "
                f"different positions cannot share one id: the ledger would record "
                f"one and every lookup would answer with the other, so an arm would "
                f"reach into a slot the model calls free. Rename the site."
            )
        self._graph.add_node(location.position_id, location=location)
        if isinstance(location.resource, ILabwarePlaceable):
            self.assign_resource_to_location(location.position_id, location.resource)

        location.add_observer(self)
        for transporter in self._resource_registry.transporters:
            await self._maybe_register_transporter_on_location(transporter, location)

    async def add_site_location(self, site: DeckSiteLocation) -> None:
        """Register a device-owned site as a FLAT routing node."""
        await self.add_location(site)

    def register_mutex_location(self, location: Location) -> None:
        """Register a device's reservation mutex key: resolvable via
        `get_location`, never a routing node."""
        self._mutex_locations[location.position_id] = location

    def register_teachpoint_alias(self, name: str, site_ids: List[str]) -> None:
        """Map a device-named external teachpoint onto its arm-reachable
        site nodes; `add_transporter` expands through this table."""
        self._teachpoint_aliases[name] = list(site_ids)

    def resolve_journey_location(self, name: str) -> Location:
        """Resolve a thread `start=`/`end=` name to a routing node.

        A device name resolves to its single site; a multi-site device
        requires the site-qualified form.
        """
        return self._resolve_addressable_location(name, "journeys")

    def resolve_placement_location(self, name: str) -> Location:
        """Resolve an operator placement target (edit/reset/register) the same
        disciplined way journeys do. A bare device name resolves to its single
        site; a device mutex key is REJECTED. Without this, an operator placing
        on a multi-site device name lands the plate on the off-graph mutex,
        where ``can_reserve``'s occupancy branch then rejects every action on
        that device for the life of the process."""
        try:
            return self._resolve_addressable_location(name, "placement targets")
        except KeyError:
            # Only here, not in the shared helper: the reads print this
            # position, but a thread may not start or end in a gripper.
            gripper = self.find_gripper_location(name)
            if gripper is None:
                raise
            return gripper

    def find_gripper_location(self, name: str) -> Optional[Location]:
        """The mover gripper position called ``name``, or None.

        Resolvable but never routable, like a device mutex key.
        Read off the movers so a new one cannot forget to register.
        """
        for mover in self._resource_registry.movers:
            if mover.gripper_location.position_id == name:
                return mover.gripper_location
        return None

    def _resolve_addressable_location(self, name: str, purpose: str) -> Location:
        if self.location_exists(name):
            return self._graph.get_node_data(name)["location"]
        aliased = self._teachpoint_aliases.get(name)
        if aliased is not None:
            if len(aliased) == 1:
                return self.get_location(aliased[0])
            raise KeyError(
                f"{name!r} names a multi-site device; {purpose} must name the "
                f"specific site (e.g. {aliased[0]!r}). Sites: {sorted(aliased)!r}."
            )
        if name in self._mutex_locations:
            sites = sorted(l.position_id for l in self.sites_of(name))
            if len(sites) == 1:
                return self.get_location(sites[0])
            hint = sites[0] if sites else f"{name}/<site>"
            raise KeyError(
                f"{name!r} names a multi-site device; {purpose} must name the "
                f"specific site (fully qualified, e.g. {hint!r}). Sites: {sites!r}."
            )
        return self.get_location(name)

    def location_exists(self, name: str) -> bool:
        return self._graph.has_node(name)

    def get_resource_location(self, resource_name: str) -> Location:
        try:
            return self._equipment_map[resource_name]
        except KeyError:
            resource_name = resource_name.replace("-", "_")
            try:
                return self._equipment_map[resource_name]
            except KeyError:
                raise ValueError(f"Resource {resource_name} does not exist")
        
    def get_distance(self, source: str, target: str) -> float:
        """Min over mutex-expanded node pairs; unreachable pairs count as
        infinity. A resident-only site is a VALID isolated node, and raising
        here killed the reservation tick loop."""
        best = float("inf")
        for s in self._distance_nodes(source):
            for t in self._distance_nodes(target):
                try:
                    best = min(best, self._graph.get_distance(s, t))
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    continue
        return best

    def _distance_nodes(self, name: str) -> List[str]:
        """A mutex key stands for its owned sites in distance terms
        (candidate distance = MIN over owned sites)."""
        if name in self._mutex_locations and not self._graph.has_node(name):
            sites = self.sites_of(name)
            if sites:
                return [site.position_id for site in sites]
        return [name]
    
    def get_transporter_between(self, source: str, target: str) -> TransporterBase:
        return self._graph.get_edge_data(source, target)["transporter"]

    def movers_between(self, source: str, target: str) -> set[str]:
        """Names of every mover on any shortest route from source to target.

        Empty when the two are not connected, which is the answer a caller
        asking "who carries this" wants rather than an exception.
        """
        if not (self.location_exists(source) and self.location_exists(target)):
            return set()
        try:
            paths = self.get_all_shortest_any_paths(source, target)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return set()
        return {
            self.get_transporter_between(leg_start, leg_end).name
            for path in paths
            for leg_start, leg_end in zip(path, path[1:])
        }

    async def add_edge(self, start: str, end: str, transporter: TransporterBase, weight: float = 5.0) -> None:
        if start not in self._graph.get_nodes():
            raise ValueError(f"Node {start} does not exist")
        if end not in self._graph.get_nodes():
            raise ValueError(f"Node {end} does not exist")
        if self._graph.has_edge(start, end):
            incumbent = self._graph.get_edge_data(start, end)["transporter"]
            if incumbent is not transporter:
                # Two movers reach one node pair, and multi-mover edges are
                # not modelled. First writer wins; rejecting breaks hamilton_smc.
                orca_logger.warning(
                    "add_edge: %s -> %s is already served by %s; %s is shadowed and "
                    "will never route over this pair. Multi-mover edges aren't modelled.",
                    start, end, incumbent.name, transporter.name,
                )
            return
        self._graph.add_edge(start, end, transporter=transporter, weight=weight)
        await self._maybe_register_transporter_on_location(transporter, self.get_location(start))
        await self._maybe_register_transporter_on_location(transporter, self.get_location(end))

    async def _maybe_register_transporter_on_location(
        self, transporter: TransporterBase, location: Location
    ) -> None:
        """Register `transporter` as an `ILabwareLocationObserver` of
        `location` if the transporter's teachpoints actually include it.

        This is the wiring that makes a sim pick work: when labware lands
        at a teachpoint via `Location.initialize_labware`, the
        transporter's observer callback seeds its sim's PLR graph so the
        validator does not reject the first pick. Real-hardware drivers
        no-op the seed.
        """
        if not isinstance(transporter, Transporter):
            return
        position_ids = {t.position_id for t in await transporter.get_teachpoints()}
        if (
            location.position_id not in position_ids
            and not transporter.covers_position(location.position_id)
        ):
            return
        location.add_observer(transporter)

    def set_edge_weight(self, start: str, end: str, weight: float) -> None:
        self._graph.set_edge_weight(start, end, weight)

    def has_available_route(self, source: str, target: str) -> bool:
        available_graph = self._get_available_graph([source])
        return available_graph.has_path(source, target)
    
    def has_any_route(self, source: str, target: str) -> bool:
        return self._graph.has_path(source, target)
        
    def get_all_shortest_available_paths(self, source: str, target: str) -> List[List[str]]:
        available_graph = self._get_available_graph([source])
        try:
            return available_graph.get_all_shortest_paths(source, target)
        except (nx.NetworkXNoPath, nx.NodeNotFound) as exc:
            raise self._no_route(source, target, exc) from exc

    def get_all_shortest_any_paths(self, source: str, target: str) -> List[List[str]]:
        try:
            return self._graph.get_all_shortest_paths(source, target)
        except (nx.NetworkXNoPath, nx.NodeNotFound) as exc:
            raise self._no_route(source, target, exc) from exc

    def _no_route(
        self, source: str, target: str, cause: Exception,
    ) -> nx.NetworkXNoPath:
        """Say why a real position can be unreachable.

        The route graph is built once, at startup, from the teachpoints that
        existed then. A position taught since is in the store and not in the
        graph, so the arm that was just taught it cannot plan to it.
        """
        if not (self.location_exists(source) and self.location_exists(target)):
            return nx.NetworkXNoPath(str(cause))
        return RouteGraphMissingTeachpointError(
            f"no route from {source!r} to {target!r}. Both are positions the "
            f"system knows, so either no mover is taught both, or the route "
            f"graph predates the teachpoint: it is built once at startup and "
            f"a position taught since is not in it. Reload the runtime to "
            f"rebuild it."
        )

    def is_path_admissible(self, path: List[str]) -> bool:
        """A device's site may appear as an INTERIOR hop
        only when the route starts or ends on that device -- never as a
        corridor between two foreign endpoints. The service set is the path's
        own source and terminal owners, which admits the inbound chained
        handoff, the departure relay, and the parking exit hop."""
        service = {
            self._graph.get_node_data(path[0])["location"].owner_mutex_id,
            self._graph.get_node_data(path[-1])["location"].owner_mutex_id,
        }
        service.discard(None)
        for node in path[1:-1]:
            owner = self._graph.get_node_data(node)["location"].owner_mutex_id
            if owner is not None and owner not in service:
                return False
        return True

    def is_deadlock_resolution_location(self, position_id: str) -> bool:
        """Park targets: explicit PlatePads only, and never a device-owned
        site even if one was constructed with a PlatePad resource."""
        location = self._graph.get_node_data(position_id)["location"]
        resource = location.resource
        return (
            isinstance(resource, PlatePad)
            and resource.supports_deadlock_resolution
            and location.owner_mutex_id is None
        )

    def get_shortest_paths_to_deadlock_resolution(self, source: str) -> List[List[str]]:
        paths = []
        for name in self._graph.get_nodes().keys():
            if name != source and self.is_deadlock_resolution_location(name):
                paths.extend(
                    path
                    for path in self.get_all_shortest_any_paths(source, name)
                    if self.is_path_admissible(path)
                )

        return paths
    
    def _get_blocking_locations(self, source: str, target: str) -> List[Location]:
        unique_stop = {stop for path in self.get_all_shortest_any_paths(source, target) for stop in path}
        blocking_locs: Set[Location] = set()
        for position_id in unique_stop:
            if position_id == source:
                continue
            location: Location = self._graph.get_node_data(position_id)["location"]
            if location is not None:
                blocking_locs.add(location)
        return list(blocking_locs)

    def _get_blocking_transporter(self, labware: LabwareInstance, source: str, target: str) -> List[TransporterBase]:
        blocking_transporters: Set[TransporterBase] = set()
        for path in self.get_all_shortest_any_paths(source, target):
            for i in range(len(path) - 1):
                edge = self._graph.get_edge_data(path[i], path[i + 1])
                transporter: TransporterBase = edge["transporter"]
                if transporter.labware is not None:
                    blocking_transporters.add(transporter)
        return list(blocking_transporters)
    
    def draw(self) -> None:
        self._graph.draw()
        
    async def initialize_transporters(self) -> None:
        """Build the route graph from every registered transporter's teachpoints.

        Split out of `__init__` because the teachpoint read is async (the
        store is the source of truth). `build_system` and the
        topology loader await this after construction. Idempotent: the
        `_added_transporters` guard inside `add_transporter` skips ones
        already wired, so re-running after a dynamic add only processes
        the newcomers.
        """
        for transporter in self._resource_registry.transporters:
            await self.add_transporter(transporter)

    async def add_transporter(self, transporter: Transporter) -> None:
        if transporter.name in self._added_transporters:
            return
        self._added_transporters.add(transporter.name)
        if self._resource_registry is not None and not self._resource_registry.has_resource(transporter.name):
            self._resource_registry.add_resource(transporter)
        taught_teachpoints = await transporter.get_teachpoints()
        # A name another teachpoint routes THROUGH is a transit pose, not a
        # destination: the arm passes it on the way somewhere and never leaves
        # labware there. It gets no routing node, and it is not required to
        # name a registered location.
        transit_only = {
            tp.gateway for tp in taught_teachpoints if tp.gateway is not None
        } - {
            tp.position_id for tp in taught_teachpoints if self.location_exists(tp.position_id)
        }
        position_ids: List[str] = []
        for teachpoint in taught_teachpoints:
            if teachpoint.position_id in transit_only:
                continue
            expanded = self._expand_teachpoint(transporter.name, teachpoint.position_id)
            for node_id in expanded:
                if node_id != teachpoint.position_id:
                    transporter.register_position_alias(node_id, teachpoint.position_id)
            position_ids.extend(expanded)
        self._transporter_positions[transporter.name] = position_ids
        if transporter.single_carriage:
            self._register_exclusion_group(transporter.name, position_ids)
        for edge in itertools.combinations(position_ids, 2):
            await self.add_edge(edge[0], edge[1], transporter)
            await self.add_edge(edge[1], edge[0], transporter)

    async def add_taught_position(
        self, transporter: Transporter, position_id: str,
    ) -> None:
        """Wire a position taught after startup into the route graph.

        Additive: the new node gets edges to every position this transporter
        already covers, and nothing is torn down. Without it the position is in
        the teachpoint store and not in the graph, so the arm that was just
        taught it cannot plan a move there and says only that the target cannot
        be reached.
        """
        if transporter.name not in self._added_transporters:
            return
        taught = await transporter.get_teachpoints()
        transit_only = {
            tp.gateway for tp in taught if tp.gateway is not None
        } - {
            tp.position_id for tp in taught if self.location_exists(tp.position_id)
        }
        if position_id in transit_only:
            return
        if not (
            self.location_exists(position_id)
            or position_id in self._teachpoint_aliases
        ):
            # Not a routing destination the map knows. Startup refuses this
            # loudly when it wires the whole transporter; refusing an operator's
            # teach the same way would stop them teaching a point before its
            # location is declared, which is a legitimate order to work in.
            return
        covered = self._transporter_positions.setdefault(transporter.name, [])
        fresh = [
            node_id
            for node_id in self._expand_teachpoint(transporter.name, position_id)
            if node_id not in covered
        ]
        if not fresh:
            return
        for node_id in fresh:
            if node_id != position_id:
                transporter.register_position_alias(node_id, position_id)
        for node_id in fresh:
            for other in covered:
                await self.add_edge(node_id, other, transporter)
                await self.add_edge(other, node_id, transporter)
        for edge in itertools.combinations(fresh, 2):
            await self.add_edge(edge[0], edge[1], transporter)
            await self.add_edge(edge[1], edge[0], transporter)
        covered.extend(fresh)
        if transporter.single_carriage:
            self._register_exclusion_group(transporter.name, covered)

    def _register_exclusion_group(
        self, transporter_name: str, position_ids: List[str]
    ) -> None:
        group = frozenset(position_ids)
        for position_id in position_ids:
            existing = self._exclusion_groups.get(position_id)
            owner = self._exclusion_owner.get(position_id)
            if existing is not None and existing != group and owner != transporter_name:
                raise ValueError(
                    f"single_carriage transporter {transporter_name!r} shares "
                    f"position {position_id!r} with another single_carriage "
                    f"group {sorted(existing)!r}; overlapping carriage groups "
                    f"are not supported."
                )
            self._exclusion_groups[position_id] = group
            self._exclusion_owner[position_id] = transporter_name

    def exclusion_siblings_of(self, position_id: str) -> List[Location]:
        """Sibling positions of a single-carriage transporter: the other
        taught stations of the one physical pad this position belongs to.
        Empty for positions outside any single_carriage group."""
        group = self._exclusion_groups.get(position_id)
        if group is None:
            return []
        return [
            self.get_location(sibling)
            for sibling in sorted(group)
            if sibling != position_id
        ]

    def is_carriage_position(self, position_id: str) -> bool:
        return position_id in self._exclusion_groups

    def boarding_onward_positions(self, path: List[str]) -> List[str]:
        """Positions that must be reserved TOGETHER with boarding ``path[1]``.

        A single-carriage position is a corridor, not a destination: labware
        may only board with its whole run to the first resting position
        reserved, or it strands on the carriage and severs the bridge for
        every other thread. Empty when ``path[1]`` is not a carriage position
        or is itself the requested final target.
        """
        onward: List[str] = []
        index = 1
        while index < len(path) - 1 and path[index] in self._exclusion_groups:
            onward.append(path[index + 1])
            index += 1
        return onward

    def _expand_teachpoint(self, transporter_name: str, position_id: str) -> List[str]:
        """Translate a taught name to routing nodes: an existing node stands
        for itself; a device name stands for its arm-reachable sites.
        Unknown names fail LOUD - never an auto-created node."""
        if self.location_exists(position_id):
            return [position_id]
        aliased = self._teachpoint_aliases.get(position_id)
        if aliased is not None:
            return list(aliased)
        if position_id in self._mutex_locations:
            sites = sorted(l.position_id for l in self.sites_of(position_id))
            raise ValueError(
                f"Transporter {transporter_name!r} teachpoint {position_id!r} names a "
                f"multi-site device; teach the SITE-QUALIFIED point the arm actually "
                f"reaches (one per physical coordinate). Sites: {sites!r}."
            )
        raise ValueError(
            f"Transporter {transporter_name!r} teachpoint {position_id!r} names "
            f"neither a registered location nor a device. Registered locations: "
            f"{sorted(loc.position_id for loc in self.locations)!r}; devices with "
            f"arm-reachable sites: {sorted(self._teachpoint_aliases)!r}. Register "
            f"the location (or fix the teachpoint name) before wiring edges."
        )

    def register_device_location(self, device_name: str, location: Location) -> None:
        """Register a device name -> location mapping for routing lookups."""
        self._equipment_map[device_name] = location

    def assign_resource_to_location(self, position_id: str, resource: ILabwarePlaceable) -> None:
        try:
            location = self.get_location(position_id)
        except KeyError:
            raise ValueError(f"Location {position_id} does not exist")
        location.resource = resource
        self._equipment_map[resource.name] = location

    def assign_resources(self, resources: Mapping[str, ILabwarePlaceable]) -> None:
        for name, resource in resources.items():
            self.assign_resource_to_location(name, resource)
        
    def resource_registry_notify(self, event: str, resource: IResource) -> None:
        # Transporter wiring reads teachpoints from the async store, so it
        # runs in async `initialize_transporters`, not this sync observer hook.
        return

    def location_notify(self, event: str, location: Location, resource: ILabwarePlaceable) -> None:
        if event == "resource_set":
            if isinstance(resource, ILabwarePlaceable):
                self._equipment_map[resource.name] = location

    def _get_available_graph(self, include_nodes: Optional[List[str]] = None) -> _NetworkXHandler:
        subgraph = self._graph.get_subgraph([name for name, _ in self._get_available_locations(include_nodes).items()])
        available_edges: List[Tuple[str, str, EdgeData]] = []
        for edge in subgraph.get_all_edges():
            source, target, data = edge
            if data["transporter"].labware is None:
                available_edges.append(edge)
        available_graph = _NetworkXHandler()
        for name, nodedata in subgraph.get_nodes().items():
            available_graph.add_node(name, nodedata["location"])
        for source, target, data in available_edges:
            available_graph.add_edge(source, target, data["transporter"], data["weight"])
        return available_graph

    def _get_available_locations(self, include_nodes: Optional[List[str]] = None) -> Dict[str, NodeData]:
        nodes = {}
        include_nodes = include_nodes if include_nodes is not None else []
        for node, nodedata in self._graph.get_nodes().items():
            location: Location = nodedata["location"]
            if node in include_nodes or location.labware is None:
                nodes[node] = nodedata
        return nodes
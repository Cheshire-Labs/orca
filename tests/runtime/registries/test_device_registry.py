"""DeviceRegistryImpl contract.

Verifies the two-card composition rules:

- Either card may be present alone; absence of one is a valid state.
- Topology-only entries support CHECK_RUN only (R1).
- SIM / LIVE require both cards AND a live connection.
- Heartbeat staleness flips `is_connected` to False (owned by the source impl;
  asserted via a fake source that exposes the threshold knob).
- `is_device_connected` / `is_initialized` come from the device bridge when one
  holds the device, and from the in-process LIVE driver when none does.
- Kind-mismatch (C2 superset rule) raises `TopologyCollisionError`.
- `unreachable` names every device a sim or live run cannot reach, not just the first.

Tests use small fake doubles (no PLR / cheshire-drivers / system_runtime
infrastructure pulled in) so failures localize to the registry itself.
"""

from datetime import datetime, timedelta, timezone

from unittest.mock import AsyncMock, MagicMock

import pytest
from cheshire_drivers.gateway_protocol import DeviceConnectInfo, DriverMode

from orca.modes.run_modes import OUT_OF_RUN_BASE
from orca.gateway.connection_source import (
    _HEARTBEAT_TOLERANCE_SECONDS,
    DeviceConnectionSource,
)
from orca.gateway.registry.connection_tracker import DeviceConnectionTracker
from orca.resource_models.resources import IResource
from orca.resource_models.device_run_mode import DeviceRunMode
from orca.runtime.registries.device_registry import DeviceRegistryImpl
from orca.runtime.registry_protocols import (
    IDeviceConnectionSource,
    ITopologyRegistry,
)
from orca.runtime.errors import (
    TopologyCollisionError,
)
from orca.runtime.system_runtime import declared_device_snapshots
from orca.runtime.status_models import (
    ConnectionCard,
    ReportedDeviceLink,
    TopologyDeviceEntry,
    WorkflowRunMode,
)


# --- Fakes ------------------------------------------------------------------


class FakeTopologyRegistry(ITopologyRegistry):
    """In-memory topology source keyed by name."""

    def __init__(self, entries: list[TopologyDeviceEntry]) -> None:
        self._by_name: dict[str, TopologyDeviceEntry] = {
            entry.name: entry for entry in entries
        }

    def list_devices(self) -> list[TopologyDeviceEntry]:
        return list(self._by_name.values())

    def get_device(self, name: str) -> TopologyDeviceEntry | None:
        return self._by_name.get(name)


class FakeConnectionSource(IDeviceConnectionSource):
    """In-memory connection source.

    `is_connected` mirrors the production `DeviceConnectionSource` rule:
    `last_heartbeat` against the imported `_HEARTBEAT_TOLERANCE_SECONDS`,
    with a missing heartbeat treated as disconnected. The registry-composition
    tests use this fake to localize failures to the registry; the production
    staleness rule itself is covered by `TestRealConnectionSourceStaleness`.
    `peek_reported_link` reads the same card, so a device with no card has no
    device bridge behind it and the registry falls back to the in-process
    driver.
    """

    def __init__(
        self,
        cards: list[ConnectionCard],
        now: datetime | None = None,
    ) -> None:
        self._by_name: dict[str, ConnectionCard] = {
            card.name: card for card in cards
        }
        self._now: datetime = now or datetime.now(timezone.utc)

    async def get_connection_card(self, name: str) -> ConnectionCard | None:
        return self._by_name.get(name)

    async def list_connection_cards(self) -> list[ConnectionCard]:
        return list(self._by_name.values())

    async def is_connected(self, name: str) -> bool:
        card = self._by_name.get(name)
        if card is None or card.last_heartbeat is None:
            return False
        delta = self._now - card.last_heartbeat
        return delta.total_seconds() <= _HEARTBEAT_TOLERANCE_SECONDS

    def peek_reported_link(self, name: str) -> ReportedDeviceLink | None:
        card = self._by_name.get(name)
        if card is None:
            return None
        return ReportedDeviceLink(
            mode=card.device_link_mode,
            is_connected=card.device_is_connected,
            is_initialized=card.device_is_initialized,
        )


class FakeSystem:
    """Stand-in for ISystem providing only the resource lookups the registry needs.

    The real ISystem aggregates many sub-interfaces; the registry only calls
    `has_resource(name)` and `get_resource(name)` (via the `DeviceLinkReader`
    fallback for when no device bridge holds the device). A bare object
    satisfies the runtime structural check.
    """

    def __init__(self, resources: dict[str, IResource] | None = None) -> None:
        self._resources: dict[str, IResource] = resources or {}

    def has_resource(self, name: str) -> bool:
        return name in self._resources

    def get_resource(self, name: str) -> IResource:
        return self._resources[name]


class _StubDriver:
    """Driver stub reporting its own link, as a real driver does."""

    def __init__(self, *, is_connected: bool, is_initialized: bool) -> None:
        self.is_connected: bool = is_connected
        self.is_initialized: bool = is_initialized


class _StubAgentHeldDriver(_StubDriver):
    """Driver stub carrying the agent-held stamp `RemoteDeviceFactory` applies.

    Declares the attribute rather than being stamped, so these tests need no
    factory. It is the marker a read surface tests for before believing a link
    flag it finds in process.
    """

    @property
    def instrument_is_held_remotely(self) -> bool:
        return True


class _StubResource(IResource):
    """Stub shaped like Device/Transporter: one driver and a real run mode."""

    def __init__(self, driver: _StubDriver) -> None:
        self._driver = driver
        self._run_mode = DeviceRunMode("stub")

    @property
    def name(self) -> str:
        return "stub"

    @property
    def driver(self) -> _StubDriver:
        return self._driver

    async def mode_under(self, base: WorkflowRunMode) -> WorkflowRunMode:
        return await self._run_mode.mode_under(base)

    @property
    def is_initialized(self) -> bool:
        return self._driver.is_initialized


# --- Helpers ----------------------------------------------------------------


def _topology_entry(
    name: str = "shaker_1",
    *,
    interfaces: tuple[str, ...] = ("IShaker",),
    kind: str = "shaker",
    position_ids: tuple[str, ...] = ("loc_a",),
    interfaces_are_class_defaults: bool = False,
) -> TopologyDeviceEntry:
    return TopologyDeviceEntry(
        name=name,
        kind=kind,
        interfaces=interfaces,
        position_ids=position_ids,
        interfaces_are_class_defaults=interfaces_are_class_defaults,
    )


def _connection_card(
    name: str = "shaker_1",
    *,
    advertised_interfaces: frozenset[str] = frozenset({"IShaker"}),
    advertised_kind: str = "Shaker",
    last_heartbeat: datetime | None = None,
    client_id: str = "client_1",
    connection_id: str = "conn_1",
    device_is_connected: bool = False,
    device_is_initialized: bool = False,
    device_link_mode: DriverMode | None = None,
) -> ConnectionCard:
    return ConnectionCard(
        name=name,
        client_id=client_id,
        connection_id=connection_id,
        last_heartbeat=last_heartbeat,
        advertised_kind=advertised_kind,
        advertised_interfaces=advertised_interfaces,
        device_is_connected=device_is_connected,
        device_is_initialized=device_is_initialized,
        device_link_mode=device_link_mode,
    )


def _brought_up(is_initialized: bool) -> _StubResource:
    """A resource whose in-process drivers both agree, so only the source varies."""
    driver = _StubDriver(is_connected=is_initialized, is_initialized=is_initialized)
    return _StubResource(driver)


def _registry(
    *,
    topology: list[TopologyDeviceEntry] | None = None,
    connections: list[ConnectionCard] | None = None,
    now: datetime | None = None,
    resources: dict[str, IResource] | None = None,
) -> DeviceRegistryImpl:
    return DeviceRegistryImpl(
        topology_source=FakeTopologyRegistry(topology or []),
        connection_source=FakeConnectionSource(connections or [], now=now),
        system=FakeSystem(resources),
        base_mode=lambda: OUT_OF_RUN_BASE,
    )


# --- Two-card matrix --------------------------------------------------------


class TestTwoCardMatrix:
    """Each combination of (topology_card, connection_card) presence."""

    async def test_topology_only(self) -> None:
        now = datetime.now(timezone.utc)
        registry = _registry(
            topology=[_topology_entry("shaker_1")],
            now=now,
        )
        entry = await registry.get("shaker_1")
        assert entry is not None
        assert entry.topology_card is not None
        assert entry.connection_card is None
        assert await entry.is_client_connected() is False
        assert await entry.supports_mode(WorkflowRunMode.CHECK_RUN) is True
        assert await entry.supports_mode(WorkflowRunMode.SIM) is False
        assert await entry.supports_mode(WorkflowRunMode.LIVE) is False

    async def test_connection_only(self) -> None:
        now = datetime.now(timezone.utc)
        fresh_hb = now - timedelta(seconds=1)
        registry = _registry(
            connections=[_connection_card("shaker_1", last_heartbeat=fresh_hb)],
            now=now,
        )
        entry = await registry.get("shaker_1")
        assert entry is not None
        assert entry.topology_card is None
        assert entry.connection_card is not None
        # The device list shows when its client last spoke and what kind it advertised.
        assert entry.connection_card.last_heartbeat == fresh_hb
        assert entry.connection_card.advertised_kind == "Shaker"
        assert await entry.is_client_connected() is True
        # CHECK_RUN needs a topology declaration; connection-only fails.
        assert await entry.supports_mode(WorkflowRunMode.CHECK_RUN) is False
        # SIM/LIVE also fail because BOTH cards are required (R1).
        assert await entry.supports_mode(WorkflowRunMode.SIM) is False
        assert await entry.supports_mode(WorkflowRunMode.LIVE) is False

    async def test_both_cards_present(self) -> None:
        now = datetime.now(timezone.utc)
        fresh_hb = now - timedelta(seconds=1)
        registry = _registry(
            topology=[_topology_entry("shaker_1")],
            connections=[_connection_card("shaker_1", last_heartbeat=fresh_hb)],
            now=now,
        )
        entry = await registry.get("shaker_1")
        assert entry is not None
        assert entry.topology_card is not None
        assert entry.connection_card is not None
        assert await entry.is_client_connected() is True
        assert await entry.supports_mode(WorkflowRunMode.CHECK_RUN) is True
        assert await entry.supports_mode(WorkflowRunMode.SIM) is True
        assert await entry.supports_mode(WorkflowRunMode.LIVE) is True

    async def test_neither_card(self) -> None:
        registry = _registry()
        assert await registry.get("nonexistent") is None

    async def test_list_all_orders_topology_first_then_connection_only(self) -> None:
        now = datetime.now(timezone.utc)
        fresh_hb = now - timedelta(seconds=1)
        registry = _registry(
            topology=[
                _topology_entry("shaker_1"),
                _topology_entry("shaker_2"),
            ],
            connections=[
                _connection_card("shaker_1", last_heartbeat=fresh_hb),
                _connection_card("rogue", last_heartbeat=fresh_hb),
            ],
            now=now,
        )
        entries = await registry.list_all()
        names = [entry.name for entry in entries]
        assert names == ["shaker_1", "shaker_2", "rogue"]


# --- Which devices a sim or live run cannot reach ---------------------------


class TestUnreachable:
    """A sim or live command needs the device declared and its orca-client connected."""

    async def test_a_declared_device_with_no_connection_is_unreachable(self) -> None:
        registry = _registry(topology=[_topology_entry("shaker_1")])
        assert list(await registry.unreachable(["shaker_1"])) == ["shaker_1"]

    async def test_both_cards_and_a_fresh_heartbeat_are_reachable(self) -> None:
        now = datetime.now(timezone.utc)
        registry = _registry(
            topology=[_topology_entry("shaker_1")],
            connections=[_connection_card("shaker_1", last_heartbeat=now - timedelta(seconds=1))],
            now=now,
        )
        assert await registry.unreachable(["shaker_1"]) == {}

    async def test_a_stale_heartbeat_is_unreachable(self) -> None:
        now = datetime.now(timezone.utc)
        stale_hb = now - timedelta(seconds=_HEARTBEAT_TOLERANCE_SECONDS + 60)
        registry = _registry(
            topology=[_topology_entry("shaker_1")],
            connections=[_connection_card("shaker_1", last_heartbeat=stale_hb)],
            now=now,
        )
        assert list(await registry.unreachable(["shaker_1"])) == ["shaker_1"]

    async def test_every_unreachable_device_is_named(self) -> None:
        now = datetime.now(timezone.utc)
        registry = _registry(
            topology=[_topology_entry("ok_1"), _topology_entry("broken_2"), _topology_entry("broken_3")],
            connections=[_connection_card("ok_1", last_heartbeat=now - timedelta(seconds=1))],
            now=now,
        )
        assert sorted(await registry.unreachable(["ok_1", "broken_2", "broken_3"])) == ["broken_2", "broken_3"]

    async def test_the_reason_names_a_connected_device_the_topology_does_not_declare(self) -> None:
        # A typo'd 'shaker1' connected while 'shaker_1' is declared: the reason names it so the mismatch shows.
        now = datetime.now(timezone.utc)
        registry = _registry(
            topology=[_topology_entry("shaker_1")],
            connections=[_connection_card("shaker1", last_heartbeat=now - timedelta(seconds=1))],
            now=now,
        )
        assert "shaker1" in (await registry.unreachable(["shaker_1"]))["shaker_1"]

    async def test_the_reason_has_no_hint_when_every_connection_is_declared(self) -> None:
        registry = _registry(topology=[_topology_entry("shaker_1")])
        assert (await registry.unreachable(["shaker_1"]))["shaker_1"] == "its orca-client is not connected"

    async def test_a_stale_undeclared_connection_is_not_offered_as_the_mismatch(self) -> None:
        now = datetime.now(timezone.utc)
        stale_hb = now - timedelta(seconds=_HEARTBEAT_TOLERANCE_SECONDS + 60)
        registry = _registry(
            topology=[_topology_entry("shaker_1")],
            connections=[_connection_card("shaker1", last_heartbeat=stale_hb)],
            now=now,
        )
        assert "shaker1" not in (await registry.unreachable(["shaker_1"]))["shaker_1"]


# --- Heartbeat staleness ----------------------------------------------------


class TestHeartbeatStaleness:
    """The registry never time-checks; it delegates to the connection source.

    These cases verify the registry routes `entry.is_client_connected()` to the
    source's verdict. The threshold is the production
    `_HEARTBEAT_TOLERANCE_SECONDS` (imported), so the fresh/stale boundary
    here can't drift from production. The production staleness computation
    itself is exercised in `TestRealConnectionSourceStaleness`.
    """

    async def test_fresh_heartbeat_is_connected(self) -> None:
        now = datetime.now(timezone.utc)
        fresh_hb = now - timedelta(seconds=1)
        registry = _registry(
            topology=[_topology_entry("shaker_1")],
            connections=[_connection_card("shaker_1", last_heartbeat=fresh_hb)],
            now=now,
        )
        entry = await registry.get("shaker_1")
        assert entry is not None
        assert await entry.is_client_connected() is True

    async def test_stale_heartbeat_disconnected(self) -> None:
        now = datetime.now(timezone.utc)
        stale_hb = now - timedelta(seconds=_HEARTBEAT_TOLERANCE_SECONDS + 60)
        registry = _registry(
            topology=[_topology_entry("shaker_1")],
            connections=[_connection_card("shaker_1", last_heartbeat=stale_hb)],
            now=now,
        )
        entry = await registry.get("shaker_1")
        assert entry is not None
        assert await entry.is_client_connected() is False

    async def test_no_heartbeat_disconnected(self) -> None:
        now = datetime.now(timezone.utc)
        registry = _registry(
            topology=[_topology_entry("shaker_1")],
            connections=[_connection_card("shaker_1", last_heartbeat=None)],
            now=now,
        )
        entry = await registry.get("shaker_1")
        assert entry is not None
        assert await entry.is_client_connected() is False


# --- Real connection source: production staleness rule ----------------------


async def _register_device(
    tracker: DeviceConnectionTracker, name: str = "shaker_1",
) -> None:
    await tracker.register_client(
        client_id="client_1",
        site="site_1",
        lab="lab_1",
        workcell=None,
        devices=[
            DeviceConnectInfo(
                name=name,
                type="Shaker",
                interfaces=frozenset({"IShaker"}),
            ),
        ],
    )


class TestRealConnectionSourceStaleness:
    """Drive the production `DeviceConnectionSource.is_connected` directly.

    The registry tests above use a fake source; this class exercises the real
    staleness computation. Offsets are fixed wall-clock seconds (not derived
    from the threshold constant) so a comparison-operator flip or an absurd
    threshold change fails here. `test_threshold_is_90_seconds` pins the
    value itself so a silent retune is caught loudly.
    """

    async def test_threshold_is_90_seconds(self) -> None:
        assert _HEARTBEAT_TOLERANCE_SECONDS == 90.0

    async def test_fresh_registration_is_connected(self) -> None:
        tracker = DeviceConnectionTracker()
        await _register_device(tracker)
        source = DeviceConnectionSource(tracker)
        assert await source.is_connected("shaker_1") is True

    async def test_unknown_device_is_disconnected(self) -> None:
        source = DeviceConnectionSource(DeviceConnectionTracker())
        assert await source.is_connected("never_seen") is False

    async def test_stale_last_seen_is_disconnected(self) -> None:
        tracker = DeviceConnectionTracker()
        await _register_device(tracker)
        snapshot = await tracker.get_device("shaker_1")
        assert snapshot is not None
        # 5 minutes old: stale under the 90 s threshold.
        snapshot.last_seen = datetime.now(timezone.utc) - timedelta(seconds=300)
        source = DeviceConnectionSource(tracker)
        assert await source.is_connected("shaker_1") is False

    async def test_within_tolerance_is_connected(self) -> None:
        tracker = DeviceConnectionTracker()
        await _register_device(tracker)
        snapshot = await tracker.get_device("shaker_1")
        assert snapshot is not None
        # 5 s old: comfortably inside the 90 s threshold.
        snapshot.last_seen = datetime.now(timezone.utc) - timedelta(seconds=5)
        source = DeviceConnectionSource(tracker)
        assert await source.is_connected("shaker_1") is True


# --- Init transitions -------------------------------------------------------


class TestIsInitialized:
    """`is_initialized` comes from whoever holds the driver."""

    async def test_the_agents_report_wins_over_the_in_process_proxy(self) -> None:
        """The proxy's copy only moves when a command passes through it.

        Typed device routes reach the instrument over the wire and skip the
        proxy entirely, so its flag can sit False through a full bring-up.
        """
        now = datetime.now(timezone.utc)
        fresh_hb = now - timedelta(seconds=1)
        registry = _registry(
            topology=[_topology_entry("shaker_1")],
            connections=[
                _connection_card(
                    "shaker_1", last_heartbeat=fresh_hb, device_is_initialized=True,
                )
            ],
            now=now,
            resources={"shaker_1": _brought_up(False)},
        )
        entry = await registry.get("shaker_1")
        assert entry is not None
        assert await entry.is_initialized() is True

    async def test_the_agent_can_also_report_not_initialized(self) -> None:
        now = datetime.now(timezone.utc)
        fresh_hb = now - timedelta(seconds=1)
        registry = _registry(
            topology=[_topology_entry("shaker_1")],
            connections=[
                _connection_card(
                    "shaker_1", last_heartbeat=fresh_hb, device_is_initialized=False,
                )
            ],
            now=now,
            resources={"shaker_1": _brought_up(True)},
        )
        entry = await registry.get("shaker_1")
        assert entry is not None
        assert await entry.is_initialized() is False

    async def test_with_no_agent_the_answer_is_the_driver_the_verbs_bring_up(
        self,
    ) -> None:
        """No device bridge means orca holds the driver, so the read is the driver's."""
        resource = _StubResource(_StubDriver(is_connected=True, is_initialized=True))
        registry = _registry(
            topology=[_topology_entry("shaker_1")],
            resources={"shaker_1": resource},
        )
        entry = await registry.get("shaker_1")
        assert entry is not None
        assert entry.connection_card is None, "no device bridge holds this device"
        assert await entry.is_initialized() is True

    async def test_no_resource_in_system_yields_false(self) -> None:
        registry = _registry(topology=[_topology_entry("shaker_1")])
        entry = await registry.get("shaker_1")
        assert entry is not None
        assert await entry.is_initialized() is False


# --- C2 kind mismatch -------------------------------------------------------


class TestKindMismatch:
    """Per C2: connection-advertised interfaces must be a superset of topology's."""

    async def test_superset_passes(self) -> None:
        # Topology: IShaker. Connection: IShaker + ITempSettable. Superset.
        now = datetime.now(timezone.utc)
        fresh_hb = now - timedelta(seconds=1)
        registry = _registry(
            topology=[
                _topology_entry("shaker_1", interfaces=("IShaker",)),
            ],
            connections=[
                _connection_card(
                    "shaker_1",
                    advertised_interfaces=frozenset(
                        {"IShaker", "ITempSettable"},
                    ),
                    last_heartbeat=fresh_hb,
                ),
            ],
            now=now,
        )
        entry = await registry.get("shaker_1")
        assert entry is not None
        assert entry.interface_mismatch is None
        # Accepted, but a command is checked against what the topology declared: the extra is not dispatchable.
        assert entry.effective_interfaces() == frozenset({"IShaker"})

    async def test_non_superset_raises_on_get(self) -> None:
        # Topology: IShaker. Connection: IDelidder. Not a superset.
        now = datetime.now(timezone.utc)
        registry = _registry(
            topology=[_topology_entry("shaker_1", interfaces=("IShaker",))],
            connections=[
                _connection_card(
                    "shaker_1",
                    advertised_interfaces=frozenset({"IDelidder"}),
                    last_heartbeat=now - timedelta(seconds=1),
                ),
            ],
            now=now,
        )
        with pytest.raises(TopologyCollisionError):
            await registry.get("shaker_1")

    async def test_partial_overlap_not_superset_raises(self) -> None:
        # Topology declares IShaker + ITempSettable; connection only IShaker.
        # The connection's set is NOT a superset of the topology's.
        now = datetime.now(timezone.utc)
        registry = _registry(
            topology=[
                _topology_entry(
                    "shaker_1", interfaces=("IShaker", "ITempSettable"),
                ),
            ],
            connections=[
                _connection_card(
                    "shaker_1",
                    advertised_interfaces=frozenset({"IShaker"}),
                    last_heartbeat=now - timedelta(seconds=1),
                ),
            ],
            now=now,
        )
        with pytest.raises(TopologyCollisionError) as exc:
            await registry.get("shaker_1")
        # Error message must call out the missing interface so the operator
        # can act on the actual gap, not just that "something is wrong".
        assert "ITempSettable" in str(exc.value)


class TestOneMismatchedDeviceInTheList:
    """The list answers a mismatched device's row with the mismatch and keeps every other row."""

    async def test_the_list_keeps_every_device_and_names_the_mismatch(self) -> None:
        now = datetime.now(timezone.utc)
        registry = _registry(
            topology=[
                _topology_entry("shaker_1", interfaces=("IShaker", "ITempSettable")),
                _topology_entry("shaker_2", interfaces=("IShaker",)),
            ],
            connections=[
                _connection_card(
                    "shaker_1", advertised_interfaces=frozenset({"IShaker"}),
                    last_heartbeat=now - timedelta(seconds=1),
                ),
            ],
            now=now,
        )

        entries = {entry.name: entry for entry in await registry.list_all()}

        assert set(entries) == {"shaker_1", "shaker_2"}
        mismatch = entries["shaker_1"].interface_mismatch
        assert mismatch is not None and "ITempSettable" in mismatch
        assert entries["shaker_2"].interface_mismatch is None

    async def test_the_blocker_read_still_reads_every_declared_device(self) -> None:
        """The read an operator needs most when something is wrong must not fail for one device."""
        now = datetime.now(timezone.utc)
        registry = _registry(
            topology=[
                _topology_entry("shaker_1", interfaces=("IShaker", "ITempSettable")),
                _topology_entry("shaker_2", interfaces=("IShaker",)),
            ],
            connections=[
                _connection_card(
                    "shaker_1", advertised_interfaces=frozenset({"IShaker"}),
                    last_heartbeat=now - timedelta(seconds=1),
                ),
            ],
            now=now,
        )
        devices = MagicMock()
        devices.get_device_status = AsyncMock(side_effect=lambda name: name)

        await declared_device_snapshots(registry, devices)

        read = [call.args[0] for call in devices.get_device_status.await_args_list]
        assert read == ["shaker_1", "shaker_2"]


# --- effective_interfaces shape per card combo ------------------------------


class TestEffectiveInterfaces:

    async def test_topology_only_returns_declared(self) -> None:
        registry = _registry(
            topology=[
                _topology_entry("shaker_1", interfaces=("IShaker",)),
            ],
        )
        entry = await registry.get("shaker_1")
        assert entry is not None
        assert entry.effective_interfaces() == frozenset({"IShaker"})

    async def test_connection_only_returns_advertised(self) -> None:
        now = datetime.now(timezone.utc)
        registry = _registry(
            connections=[
                _connection_card(
                    "shaker_1",
                    advertised_interfaces=frozenset({"IShaker"}),
                    last_heartbeat=now - timedelta(seconds=1),
                ),
            ],
            now=now,
        )
        entry = await registry.get("shaker_1")
        assert entry is not None
        assert entry.effective_interfaces() == frozenset({"IShaker"})

    async def test_neither_returns_none_so_the_controller_checks_against_what_is_advertised(self) -> None:
        # The registry returns None for neither-card; this exercises
        # DeviceRegistryEntry.effective_interfaces directly with both cards
        # absent (a state operators should never see in production).
        from orca.runtime.status_models import DeviceRegistryEntry

        class _NullProvider:
            async def _is_client_connected(self, name: str) -> bool:
                del name
                return False

        entry = DeviceRegistryEntry(
            name="phantom",
            topology_card=None,
            connection_card=None,
            registry=_NullProvider(),
            device_link=ReportedDeviceLink(
                mode=None, is_connected=False, is_initialized=False,
            ),
        )
        assert entry.effective_interfaces() is None


class TestTheTwoConnectionsAreSeparate:
    """A released device under a live client must be distinguishable.

    This is the whole point of splitting the flag. The heartbeat is a per-CLIENT
    signal stamped on every device that client owns, so on its own it cannot say
    a device was released. If both flags moved together, a UI gating its buttons
    on connectivity would offer commands the instrument cannot take.
    """

    async def test_a_released_device_still_has_a_live_client(self) -> None:
        now = datetime.now(timezone.utc)
        registry = _registry(
            topology=[_topology_entry("arm_1")],
            connections=[_connection_card("arm_1", last_heartbeat=now - timedelta(seconds=1))],
            now=now,
            resources={"arm_1": _brought_up(False)},
        )
        entry = await registry.get("arm_1")
        assert entry is not None

        assert await entry.is_client_connected() is True, (
            "the client never went away; only the device was released"
        )
        assert await entry.is_device_connected() is False

    async def test_a_dead_client_leaves_its_last_device_report_standing(self) -> None:
        """The failure runs the other way too, and the flags must not be conflated.

        A stale heartbeat means the device bridge is unreachable, so nothing can
        correct what it last said about the device. Callers need both flags to
        tell an unreachable device bridge apart from a released instrument.
        """
        now = datetime.now(timezone.utc)
        registry = _registry(
            topology=[_topology_entry("arm_1")],
            connections=[
                _connection_card(
                    "arm_1",
                    last_heartbeat=now - timedelta(seconds=120),
                    device_is_connected=True,
                )
            ],
            now=now,
        )
        entry = await registry.get("arm_1")
        assert entry is not None

        assert await entry.is_client_connected() is False
        assert await entry.is_device_connected() is True

    async def test_a_live_arm_reads_connected_even_when_dispatch_would_pick_a_simulator(
        self,
    ) -> None:
        """The bug this replaced, seen on a bench with the socket open.

        The flag used to be read off `resource.driver`. Outside a seeded run
        mode that property hands back the in-process simulator, so an operator
        watching a live arm was shown a simulator's closed link. What the device
        bridge reports does not depend on the reader's run mode.
        """
        now = datetime.now(timezone.utc)
        registry = _registry(
            topology=[_topology_entry("arm_1")],
            connections=[
                _connection_card(
                    "arm_1",
                    last_heartbeat=now - timedelta(seconds=1),
                    device_is_connected=True,
                    device_link_mode="LIVE",
                )
            ],
            now=now,
            resources={"arm_1": _brought_up(False)},
        )
        entry = await registry.get("arm_1")
        assert entry is not None
        assert await entry.is_device_connected() is True

    async def test_with_no_agent_the_link_read_follows_the_connect_verb(
        self,
    ) -> None:
        """`connect` dispatches under the LIVE write base, so the read does too.

        This is the branch that decides what `orca device list` prints
        on a source-available bench, and there the operator's own verbs are the
        only thing that moves these flags.
        """
        resource = _StubResource(_StubDriver(is_connected=True, is_initialized=True))
        registry = _registry(
            topology=[_topology_entry("arm_1")],
            resources={"arm_1": resource},
        )
        entry = await registry.get("arm_1")
        assert entry is not None
        assert entry.connection_card is None, "no device bridge holds this device"
        assert await entry.is_device_connected() is True

    async def test_an_agent_that_went_quiet_does_not_hand_back_a_stale_cache(
        self,
    ) -> None:
        """Losing the device bridge must not promote its stand-in's cache to an
        answer.

        The device row is deleted outright when the socket drops, so "no report
        now" and "no device bridge ever" arrive looking identical. On a gateway
        deployment the in-process driver is a stand-in whose flags are whatever
        last passed through it, so believing them turns "the arm is gone" into
        "the arm is connected", which is the dangerous direction.
        """
        registry = _registry(
            topology=[_topology_entry("arm_1")],
            resources={"arm_1": _StubResource(_StubAgentHeldDriver(is_connected=True, is_initialized=True))},
        )
        entry = await registry.get("arm_1")
        assert entry is not None
        assert await entry.is_device_connected() is False
        assert await entry.is_initialized() is False

    async def test_no_resource_in_system_reports_no_device_link(self) -> None:
        registry = _registry(topology=[_topology_entry("arm_1")])
        entry = await registry.get("arm_1")
        assert entry is not None
        assert await entry.is_device_connected() is False

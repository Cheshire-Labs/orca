"""Tests for ActionContext: the ctx object passed to @orca.action functions.

ActionContext provides device(), labware(), param(), and emit() inside
action bodies. device() returns a DeviceHandle for the action's declared
device (no name argument needed since actions are single-device).
"""

import asyncio

import pytest

from orca.events.event_channel import EventChannelRegistry
from orca.resource_models.labware import LabwareInstance
from orca.variables.variable_store import NullVariableResolver
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.device_handle import ActionRequest, DeviceHandle
from tests.test_helpers import create_test_labware_instance


class TestActionContext:

    def _make_ctx(
        self,
        device_name: str = "shaker_1",
        labware: dict[str, LabwareInstance] | None = None,
        event_registry: EventChannelRegistry | None = None,
    ) -> tuple[ActionContext, asyncio.Queue[ActionRequest | None]]:
        queue: asyncio.Queue[ActionRequest | None] = asyncio.Queue()
        ctx = ActionContext(
            device_name=device_name,
            action_queue=queue,
            assigned_labware=labware or {},
            variable_store=NullVariableResolver(),
            execution_id="test-exec-1",
            event_channel_registry=event_registry,
        )
        return ctx, queue

    def test_device_no_args_returns_handle(self) -> None:
        """ctx.device() returns DeviceHandle for the action's device (no name needed)."""
        ctx, queue = self._make_ctx(device_name="shaker_1")
        handle = ctx.device()
        assert isinstance(handle, DeviceHandle)

    @pytest.mark.asyncio
    async def test_device_handle_uses_correct_device_name(self) -> None:
        """DeviceHandle from ctx.device() targets the action's declared device."""
        ctx, queue = self._make_ctx(device_name="bravo_96")

        async def complete() -> None:
            action = await queue.get()
            assert action is not None
            assert action.device_name == "bravo_96"
            action.completion.set()

        task = asyncio.create_task(complete())
        handle = ctx.device()
        await handle.run_protocol("proto.pro")
        await task

    async def test_labware_returns_assigned_instance(self) -> None:
        """ctx.labware(name) returns the assigned LabwareInstance."""
        plate = await create_test_labware_instance("plate_1")
        ctx, _ = self._make_ctx(labware={"plate_1": plate})
        assert ctx.labware("plate_1") is plate

    def test_labware_unknown_name_raises(self) -> None:
        """ctx.labware(name) raises ValueError for unknown labware."""
        ctx, _ = self._make_ctx()
        with pytest.raises(ValueError, match="not assigned"):
            ctx.labware("nonexistent")

    async def test_param_resolves_variable(self) -> None:
        """ctx.param(name) resolves from the variable store."""
        from orca.variables.variable_store import VariableService, VariableStore
        from orca.variables.variable_definition import VariableDefinition
        store = VariableService(VariableStore())
        store.register_global_definitions({"speed": VariableDefinition(type="int", default=500)})
        store.create_execution("test-exec-1", "test_workflow")
        store.set_global("speed", 500)
        queue: asyncio.Queue[ActionRequest | None] = asyncio.Queue()
        ctx = ActionContext(
            device_name="shaker_1",
            action_queue=queue,
            assigned_labware={},
            variable_store=store,
            execution_id="test-exec-1",
        )
        assert await ctx.param("global.speed") == 500

    @pytest.mark.asyncio
    async def test_emit_publishes_event(self) -> None:
        """ctx.emit() publishes to the EventChannelRegistry."""
        registry = EventChannelRegistry()
        ctx, _ = self._make_ctx(event_registry=registry)

        await ctx.emit("action_done", value="ok")

        channel = registry.get_or_create("action_done")
        counter, value, data = await channel.wait(seen_counter=0, timeout=1.0)
        assert value == "ok"

    def test_device_with_type_hint(self) -> None:
        """ctx.device(SomeInterface) returns a typed handle for IDE autocomplete."""
        from orca.devices.device_interfaces import IShaker
        ctx, _ = self._make_ctx(device_name="shaker_1")
        handle = ctx.device(IShaker)
        # The handle is a DeviceHandle cast to IShaker for typing purposes
        assert handle is not None


class _FakeTrough:
    """Minimal ITrough stand-in for tests. Properties beyond name/model
    are unused at runtime but must exist to satisfy the Protocol."""

    def __init__(self, name: str = "reagent_trough") -> None:
        self.name = name
        self.model: str | None = None
        self.size_x = 0.0
        self.size_y = 0.0
        self.size_z = 0.0
        self.max_volume = 0.0
        self.volume = 0.0

    def set_volume(self, volume: float) -> None:
        self.volume = volume


class TestActionContextTypedAccessors:
    """Coverage for the typed accessors and kind-narrowing overloads:
    ctx.plate / ctx.tip_rack / ctx.trough, ctx.labware(name, kind),
    await ctx.param(name, kind).
    """

    def _make_ctx(
        self,
        labware: dict[str, LabwareInstance] | None = None,
    ) -> ActionContext:
        queue: asyncio.Queue[ActionRequest | None] = asyncio.Queue()
        return ActionContext(
            device_name="device_1",
            action_queue=queue,
            assigned_labware=labware or {},
            variable_store=NullVariableResolver(),
            execution_id="test-exec-1",
        )

    async def _make_plate_instance(self, name: str = "sample_plate"):
        from orca.runtime.sim_labware import SimPlateTemplate
        return await SimPlateTemplate(name).create_instance()

    async def _make_tip_rack_instance(self, name: str = "tips"):
        from orca.runtime.sim_labware import SimTipRackTemplate
        return await SimTipRackTemplate(name).create_instance()

    def _make_trough_instance(self, name: str = "reagent_trough"):
        from orca.resource_models.labware import TroughInstance
        return TroughInstance(_FakeTrough(name), template_name=name, labware_type=name)

    async def test_plate_returns_underlying_iplate(self) -> None:
        """ctx.plate(name) returns the IPlate from the PlateInstance."""
        instance = await self._make_plate_instance("sample_plate")
        ctx = self._make_ctx(labware={"sample_plate": instance})
        assert ctx.plate("sample_plate") is instance.plate

    async def test_tip_rack_returns_underlying_itip_rack(self) -> None:
        """ctx.tip_rack(name) returns the ITipRack from the TipRackInstance."""
        instance = await self._make_tip_rack_instance("tips")
        ctx = self._make_ctx(labware={"tips": instance})
        assert ctx.tip_rack("tips") is instance.tip_rack

    def test_trough_returns_underlying_itrough(self) -> None:
        """ctx.trough(name) returns the ITrough from the TroughInstance."""
        instance = self._make_trough_instance("reagent_trough")
        ctx = self._make_ctx(labware={"reagent_trough": instance})
        assert ctx.trough("reagent_trough") is instance.trough

    async def test_plate_raises_when_labware_is_not_a_plate(self) -> None:
        """ctx.plate(name) refuses to narrow a tip rack to a plate."""
        instance = await self._make_tip_rack_instance("not_a_plate")
        ctx = self._make_ctx(labware={"not_a_plate": instance})
        with pytest.raises(TypeError, match="not PlateInstance"):
            ctx.plate("not_a_plate")

    async def test_labware_with_kind_narrows(self) -> None:
        """ctx.labware(name, PlateInstance) returns the PlateInstance."""
        from orca.resource_models.labware import PlateInstance
        instance = await self._make_plate_instance("sample_plate")
        ctx = self._make_ctx(labware={"sample_plate": instance})
        narrowed = ctx.labware("sample_plate", PlateInstance)
        assert narrowed is instance

    async def test_labware_with_wrong_kind_raises(self) -> None:
        """ctx.labware(name, kind) raises TypeError when instance is not kind."""
        from orca.resource_models.labware import PlateInstance
        instance = await self._make_tip_rack_instance("tips")
        ctx = self._make_ctx(labware={"tips": instance})
        with pytest.raises(TypeError, match="not PlateInstance"):
            ctx.labware("tips", PlateInstance)

    async def test_param_with_kind_returns_narrowed_value(self) -> None:
        """ctx.param(name, float) returns the value as float (passthrough)."""
        from orca.variables.variable_store import VariableService, VariableStore
        from orca.variables.variable_definition import VariableDefinition
        store = VariableService(VariableStore())
        store.register_global_definitions(
            {"factor": VariableDefinition(type="float", default=10.0)}
        )
        store.create_execution("test-exec-1", "test_workflow")
        store.set_global("factor", 10.0)
        queue: asyncio.Queue[ActionRequest | None] = asyncio.Queue()
        ctx = ActionContext(
            device_name="device_1",
            action_queue=queue,
            assigned_labware={},
            variable_store=store,
            execution_id="test-exec-1",
        )
        value = await ctx.param("global.factor", float)
        assert value == 10.0
        # arithmetic on the narrowed return value should be allowed
        assert value - 1 == 9.0

    async def test_param_with_wrong_kind_raises(self) -> None:
        """ctx.param(name, kind) raises TypeError when value is not kind."""
        from orca.variables.variable_store import VariableService, VariableStore
        from orca.variables.variable_definition import VariableDefinition
        store = VariableService(VariableStore())
        store.register_global_definitions(
            {"label": VariableDefinition(type="str", default="x")}
        )
        store.create_execution("test-exec-1", "test_workflow")
        store.set_global("label", "x")
        queue: asyncio.Queue[ActionRequest | None] = asyncio.Queue()
        ctx = ActionContext(
            device_name="device_1",
            action_queue=queue,
            assigned_labware={},
            variable_store=store,
            execution_id="test-exec-1",
        )
        with pytest.raises(TypeError, match="not float"):
            await ctx.param("global.label", float)


class TestActionContextContributionIndex:
    """ctx.pool_index(receiver_name): 0-based index of this action's
    contribution among all contributions to that receiver's current instance.
    Lets a pooling action route each contributor into a distinct region (e.g.
    four 96-well plates into the four quadrants of one 384 read plate).
    """

    def _make_ctx(self, indices: dict[str, int] | None) -> ActionContext:
        queue: asyncio.Queue[ActionRequest | None] = asyncio.Queue()
        return ActionContext(
            device_name="mlstar_2",
            action_queue=queue,
            assigned_labware={},
            variable_store=NullVariableResolver(),
            execution_id="test-exec-1",
            pool_indices=indices,
        )

    def test_returns_zero_based_index_for_receiver(self) -> None:
        ctx = self._make_ctx({"final_plate": 2})
        assert ctx.pool_index("final_plate") == 2

    def test_first_contribution_is_zero(self) -> None:
        ctx = self._make_ctx({"final_plate": 0})
        assert ctx.pool_index("final_plate") == 0

    def test_unknown_receiver_raises(self) -> None:
        ctx = self._make_ctx({"final_plate": 0})
        with pytest.raises(ValueError, match="does not contribute"):
            ctx.pool_index("neut_plate")

    def test_no_indices_raises(self) -> None:
        ctx = self._make_ctx(None)
        with pytest.raises(ValueError, match="does not contribute"):
            ctx.pool_index("final_plate")

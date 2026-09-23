"""Does a BARE multi-allow join spawn at N>2 methods?

A naive operator reported that a co-input thread whose join allows
EXACTLY ONE method spawns, a 2-method bare join spawns (proven by
``test_single_yield_receiver_two_methods``), but a 3-method bare join
``orca.join(allows=[m1, m2, m3])`` never spawns (stuck CREATED, silent
stall, no incident). This test sweeps N = 2, 3, 4 over the SAME shape as
the known-good 2-method regression to find the breaking point
empirically: if every N completes, the operator's stall was not the
method count; if N>=3 hangs, it confirms a spawn-derivation bug.
"""

import asyncio
from collections.abc import AsyncGenerator
from uuid import uuid4

import pytest

import orca.orca as orca
from orca.plugins import LabwareJourneyTracker, MethodTracker
from orca.resource_models.labware import PlateTemplate
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.labware_group import LabwareGroup, LabwareGroupMember
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, ThreadTemplate, WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system_interface import ISystem
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.runtime.run_modes import WorkflowRunMode
from tests.test_helpers import (
    execution_outcome,
    create_test_device,
    create_test_transporter,
    wire_system_map,
)


async def _build_n_method_bare_join_system(n: int) -> tuple[
    ISystem, WorkflowTemplate, EventBus,
]:
    """Owner runs N methods; receiver bare-single-yields one join over all N."""
    station = create_test_device("station", site_names=["site-1", "site-2"])
    transporter = create_test_transporter(
        "robot1", ["start_pad", "station", "waste"],
    )

    owner_plate = PlateTemplate("owner", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    receiver_plate = PlateTemplate(
        "receiver", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black",
    )

    registry = ResourceRegistry()
    registry.add_resource(station)
    registry.add_resource(transporter)
    station_pool = ResourcePool("station", [station])
    registry.add_resource_pool(station_pool)

    system_map = SystemMap(registry)
    await wire_system_map(
        system_map, devices={"station": station}, pads=["start_pad", "waste"],
    )

    methods: list[MethodTemplate] = []
    for i in range(n):
        @orca.action(device=station_pool, inputs=[owner_plate, receiver_plate])
        async def _step(ctx: ActionContext, _speed: int = 500 + i) -> None:
            await ctx.device().shake(duration=1, speed=_speed)

        async def _method_func(
            ctx: MethodContext, _action: ActionTemplate = _step,
        ) -> AsyncGenerator[ActionTemplate, None]:
            yield _action
        methods.append(MethodTemplate(f"method_{i}", func=_method_func))

    start_pad = system_map.get_location("start_pad")
    waste = system_map.get_location("waste")

    async def _owner_thread_func(
        ctx: ThreadContext,
    ) -> AsyncGenerator[MethodTemplate, None]:
        for m in methods:
            yield m
    owner_thread = ThreadTemplate(
        labware_template=owner_plate,
        start=start_pad,
        end=waste,
        func=_owner_thread_func,
    )

    async def _receiver_thread_func(
        ctx: ThreadContext,
    ) -> AsyncGenerator[IMethodTemplate, None]:
        yield orca.join(allows=list(methods))
    receiver_thread = ThreadTemplate(
        labware_template=receiver_plate,
        start=start_pad,
        end=waste,
        func=_receiver_thread_func,
    )

    workflow = WorkflowTemplate(f"bare_join_{n}_methods")
    workflow.add_thread(owner_thread, is_start=True)
    workflow.add_thread(receiver_thread)
    workflow.register_auto_spawn(receiver_thread)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name=f"bare_join_{n}_methods_system",
        description="",
        labwares=[owner_plate, receiver_plate],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=event_bus,
    )
    await builder.bind_labwares()
    return builder.get_system(), workflow, event_bus


async def _build_noncontiguous_system(*, loop: bool) -> tuple[
    ISystem, WorkflowTemplate, EventBus,
]:
    """Receiver is co-input to method_0 and method_2 but NOT method_1.

    Mirrors the reporter's NGS shape: a labware consumed by methods that
    are INTERLEAVED with methods that do not use it. ``loop=False`` uses a
    bare single-yield multi-allow join; ``loop=True`` uses the
    has_more_work loop the docs steer toward for this case.
    """
    station = create_test_device("station", site_names=["site-1", "site-2"])
    other = create_test_device("other")
    transporter = create_test_transporter(
        "robot1", ["start_pad", "station", "other", "waste"],
    )

    owner_plate = PlateTemplate("owner", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    receiver_plate = PlateTemplate(
        "receiver", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black",
    )

    registry = ResourceRegistry()
    registry.add_resource(station)
    registry.add_resource(other)
    registry.add_resource(transporter)
    station_pool = ResourcePool("station", [station])
    other_pool = ResourcePool("other", [other])
    registry.add_resource_pool(station_pool)
    registry.add_resource_pool(other_pool)

    system_map = SystemMap(registry)
    await wire_system_map(
        system_map,
        devices={"station": station, "other": other},
        pads=["start_pad", "waste"],
    )

    @orca.action(device=station_pool, inputs=[owner_plate, receiver_plate])
    async def step_0(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.action(device=other_pool, inputs=[owner_plate])
    async def step_1_no_receiver(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=550)

    @orca.action(device=station_pool, inputs=[owner_plate, receiver_plate])
    async def step_2(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=600)

    async def _m0(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield step_0
    method_0 = MethodTemplate("method_0", func=_m0)

    async def _m1(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield step_1_no_receiver
    method_1 = MethodTemplate("method_1", func=_m1)

    async def _m2(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield step_2
    method_2 = MethodTemplate("method_2", func=_m2)

    start_pad = system_map.get_location("start_pad")
    waste = system_map.get_location("waste")

    async def _owner_func(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        yield method_0
        yield method_1
        yield method_2
    owner_thread = ThreadTemplate(
        labware_template=owner_plate, start=start_pad, end=waste, func=_owner_func,
        contributes_to=["receiver"] if loop else None,
    )

    if loop:
        async def _receiver_func(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            while ctx.has_more_work():
                yield orca.join(allows=[method_0, method_2])
    else:
        async def _receiver_func(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            yield orca.join(allows=[method_0, method_2])
    receiver_thread = ThreadTemplate(
        labware_template=receiver_plate, start=start_pad, end=waste, func=_receiver_func,
    )

    workflow = WorkflowTemplate(f"noncontig_{'loop' if loop else 'bare'}")
    workflow.add_thread(owner_thread, is_start=True)
    workflow.add_thread(receiver_thread)
    workflow.register_auto_spawn(receiver_thread)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name=f"noncontig_{'loop' if loop else 'bare'}_system",
        description="",
        labwares=[owner_plate, receiver_plate],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=event_bus,
    )
    await builder.bind_labwares()
    return builder.get_system(), workflow, event_bus


async def _build_v2_shape_system(*, loop: bool, leading_method: bool = False) -> tuple[
    ISystem, WorkflowTemplate, EventBus,
]:
    """Faithful reconstruction of the reporter's failing v2 NGS shape.

    ONE tips template is co-input to THREE non-contiguous methods
    (method_0, method_2, method_4) interleaved with two methods that do
    NOT use it (method_1, method_3 on a different device). The single
    tips thread uses a bare multi-allow ``join(allows=[m0, m2, m4])``
    (loop=False) -- exactly the v2 shape the reporter said "never
    spawned". loop=True is the has_more_work form for contrast.
    """
    station = create_test_device("station", site_names=["site-1", "site-2"])
    other = create_test_device("other")
    transporter = create_test_transporter(
        "robot1", ["start_pad", "station", "other", "waste"],
    )

    owner_plate = PlateTemplate("owner", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    tips = PlateTemplate("tips", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")

    registry = ResourceRegistry()
    registry.add_resource(station)
    registry.add_resource(other)
    registry.add_resource(transporter)
    station_pool = ResourcePool("station", [station])
    other_pool = ResourcePool("other", [other])
    registry.add_resource_pool(station_pool)
    registry.add_resource_pool(other_pool)

    system_map = SystemMap(registry)
    await wire_system_map(
        system_map,
        devices={"station": station, "other": other},
        pads=["start_pad", "waste"],
    )

    # methods 0/2/4 consume tips (on station); 1/3 do not (on other).
    methods: list[MethodTemplate] = []
    tips_methods: list[MethodTemplate] = []
    for i in range(5):
        uses_tips = i % 2 == 0
        if uses_tips:
            @orca.action(device=station_pool, inputs=[owner_plate, tips])
            async def _act(ctx: ActionContext, _s: int = 500 + i) -> None:
                await ctx.device().shake(duration=1, speed=_s)
        else:
            @orca.action(device=other_pool, inputs=[owner_plate])
            async def _act(ctx: ActionContext, _s: int = 500 + i) -> None:
                await ctx.device().shake(duration=1, speed=_s)

        async def _mfunc(
            ctx: MethodContext, _a: ActionTemplate = _act,
        ) -> AsyncGenerator[ActionTemplate, None]:
            yield _a
        m = MethodTemplate(f"method_{i}", func=_mfunc)
        methods.append(m)
        if uses_tips:
            tips_methods.append(m)

    start_pad = system_map.get_location("start_pad")
    waste = system_map.get_location("waste")

    async def _owner_func(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        for m in methods:
            yield m
    owner_thread = ThreadTemplate(
        labware_template=owner_plate, start=start_pad, end=waste, func=_owner_func,
        contributes_to=["tips"] if loop else None,
    )

    # Optional leading method (mirrors v2's `yield m_delid` before the join):
    # the tips thread runs a method on `other` consuming its own labware first.
    @orca.action(device=other_pool, inputs=[tips])
    async def predelid(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=999)

    async def _pre(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield predelid
    m_predelid = MethodTemplate("m_predelid", func=_pre)

    if loop:
        async def _tips_func(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            if leading_method:
                yield m_predelid
            while ctx.has_more_work():
                yield orca.join(allows=list(tips_methods))
    else:
        async def _tips_func(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            if leading_method:
                yield m_predelid
            yield orca.join(allows=list(tips_methods))
    tips_thread = ThreadTemplate(
        labware_template=tips, start=start_pad, end=waste, func=_tips_func,
    )

    workflow = WorkflowTemplate(f"v2_shape_{'loop' if loop else 'bare'}")
    workflow.add_thread(owner_thread, is_start=True)
    workflow.add_thread(tips_thread)
    workflow.register_auto_spawn(tips_thread)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name=f"v2_shape_{'loop' if loop else 'bare'}_system",
        description="",
        labwares=[owner_plate, tips],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=event_bus,
    )
    await builder.bind_labwares()
    return builder.get_system(), workflow, event_bus


async def _build_nway_convergence_system(n_cothreads: int) -> tuple[
    ISystem, WorkflowTemplate, EventBus,
]:
    """ONE action with (1 entry + n_cothreads) co-input labwares.

    Reproduces the reporter's original trigger: a single action
    whose inputs are the entry labware PLUS several distinct co-input
    labwares (their tagment_setup = library + sample + reagent + tips).
    They reported that with 3 co-threads only the entry + one co-thread
    ever dispatched. Each co-thread single-allows the one converging
    method.
    """
    station = create_test_device(
        "station", site_names=[f"site-{i + 1}" for i in range(n_cothreads + 1)],
    )
    transporter = create_test_transporter(
        "robot1", ["start_pad", "station", "waste"],
    )

    entry_plate = PlateTemplate("owner", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    co_templates = [
        PlateTemplate(f"co_{i}", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
        for i in range(n_cothreads)
    ]

    registry = ResourceRegistry()
    registry.add_resource(station)
    registry.add_resource(transporter)
    station_pool = ResourcePool("station", [station])
    registry.add_resource_pool(station_pool)

    system_map = SystemMap(registry)
    await wire_system_map(
        system_map, devices={"station": station}, pads=["start_pad", "waste"],
    )

    @orca.action(device=station_pool, inputs=[entry_plate, *co_templates])
    async def converge(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    async def _m(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield converge
    method_converge = MethodTemplate("method_converge", func=_m)

    start_pad = system_map.get_location("start_pad")
    waste = system_map.get_location("waste")

    async def _entry(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        yield method_converge
    entry_thread = ThreadTemplate(
        labware_template=entry_plate, start=start_pad, end=waste, func=_entry,
    )

    workflow = WorkflowTemplate(f"nway_{n_cothreads}")
    workflow.add_thread(entry_thread, is_start=True)

    co_threads = []
    for i, tpl in enumerate(co_templates):
        async def _co(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            yield orca.join(allows=[method_converge])
        ct = ThreadTemplate(
            labware_template=tpl, start=start_pad, end=waste, func=_co,
        )
        co_threads.append(ct)
        workflow.add_thread(ct)
        workflow.register_auto_spawn(ct)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name=f"nway_{n_cothreads}_system",
        description="",
        labwares=[entry_plate, *co_templates],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=event_bus,
    )
    await builder.bind_labwares()
    return builder.get_system(), workflow, event_bus


def _owner_group() -> LabwareGroup:
    return LabwareGroup(
        id=str(uuid4()),
        members=(LabwareGroupMember(thread_template_name="owner"),),
    )


class TestBareMultiAllowJoinByMethodCount:

    @pytest.mark.parametrize("n", [2, 3, 4])
    @pytest.mark.asyncio
    async def test_bare_join_spawns_receiver_for_n_methods(self, n: int) -> None:
        system, workflow, event_bus = await _build_n_method_bare_join_system(n)
        runtime = SystemRuntime(system, event_bus=event_bus)
        method_tracker = MethodTracker()
        runtime.register_plugin(method_tracker)
        await runtime.start()
        try:
            submission = await runtime.submit(
                workflow, groups=[_owner_group()],
                mode=WorkflowRunMode.PURE_SIM,
            )
            status = await execution_outcome(runtime, submission, timeout=45.0)
        finally:
            await runtime.shutdown()

        assert status.status == "completed", (
            f"N={n}: bare {n}-method join did not complete (status="
            f"{status.status}). A silent stall here confirms the multi-allow "
            f"spawn-derivation bug at this method count."
        )
        receiver_names = [
            name for name in method_tracker.thread_names.values()
            if name.startswith("receiver")
        ]
        assert len(receiver_names) >= 1, (
            f"N={n}: no receiver thread spawned (the reported stall shape)."
        )

    @pytest.mark.parametrize("loop", [True, False])
    @pytest.mark.asyncio
    async def test_noncontiguous_consuming_methods(self, loop: bool) -> None:
        """Receiver needed by method_0 and method_2 but NOT method_1.

        Hypothesis: the bare single-yield join cannot re-join after the
        gap (method_1), so method_2 stalls or orphans; the has_more_work
        loop parks and re-joins, completing cleanly. This is the actual
        mechanic behind the reporter's NGS stall (consuming methods were
        interleaved), not the method count of the join.
        """
        system, workflow, event_bus = await _build_noncontiguous_system(loop=loop)
        runtime = SystemRuntime(system, event_bus=event_bus)
        method_tracker = MethodTracker()
        runtime.register_plugin(method_tracker)
        await runtime.start()
        timed_out = False
        status = None
        try:
            submission = await runtime.submit(
                workflow, groups=[_owner_group()],
                mode=WorkflowRunMode.PURE_SIM,
            )
            # Diagnostic bed: a timeout is an OUTCOME here, not a failure.
            try:
                status = await asyncio.wait_for(
                    runtime.wait_for_execution(submission), timeout=25.0,
                )
            except asyncio.TimeoutError:
                timed_out = True
        finally:
            await runtime.shutdown()

        outcome = "TIMEOUT/STALL" if timed_out else (status.status if status else "?")
        print(f"\n[noncontiguous loop={loop}] outcome={outcome}")
        # A multi-allow join spawns and completes even when the consuming
        # methods are non-contiguous, so method-count / non-contiguity is
        # NOT the trigger for the reporter's silent stall. Both the bare
        # single-yield join and the has_more_work loop (with the owner wired
        # contributes_to=["receiver"] so the slot can close) complete.
        assert not timed_out and status is not None, (
            f"multi-allow join must not stall across a gap; got {outcome}"
        )
        assert status.status == "completed", (
            f"multi-allow join across a gap completed in repro; got {outcome}"
        )

    @pytest.mark.parametrize("loop", [False, True])
    @pytest.mark.asyncio
    async def test_v2_shape_single_template_three_noncontiguous(self, loop: bool) -> None:
        """Reproduce the reporter's v2: one tips template co-input to 3
        non-contiguous methods. The bare multi-allow join (loop=False) spawns
        one tips instance per consuming method (3); the has_more_work loop
        (loop=True, owner wired contributes_to=["tips"]) re-joins with a
        single looping instance (1). Both complete -- the reported "never
        spawned" shape spawns and completes here.
        """
        system, workflow, event_bus = await _build_v2_shape_system(loop=loop)
        runtime = SystemRuntime(system, event_bus=event_bus)
        method_tracker = MethodTracker()
        runtime.register_plugin(method_tracker)
        await runtime.start()
        timed_out = False
        status = None
        try:
            submission = await runtime.submit(
                workflow, groups=[_owner_group()],
                mode=WorkflowRunMode.PURE_SIM,
            )
            # Diagnostic bed: a timeout is an OUTCOME here, not a failure.
            try:
                status = await asyncio.wait_for(
                    runtime.wait_for_execution(submission), timeout=25.0,
                )
            except asyncio.TimeoutError:
                timed_out = True
        finally:
            await runtime.shutdown()

        tips_threads = [
            name for name in method_tracker.thread_names.values()
            if name.startswith("tips")
        ]
        outcome = "TIMEOUT/STALL" if timed_out else (status.status if status else "?")
        print(
            f"\n[v2_shape loop={loop}] outcome={outcome} "
            f"tips_instances_spawned={len(tips_threads)}"
        )
        assert not timed_out and status is not None, (
            f"v2 single-template multi-allow join must not stall; got {outcome}"
        )
        assert status.status == "completed", (
            f"v2 single-template multi-allow join completes; got {outcome}"
        )
        expected_tips = 1 if loop else 3
        assert len(tips_threads) == expected_tips, (
            f"loop={loop}: expected {expected_tips} tips instance(s), "
            f"got {len(tips_threads)} -- the spawn model regressed"
        )

    @pytest.mark.asyncio
    async def test_v2_shape_bare_join_with_leading_method(self) -> None:
        """The one structural feature the v2 reconstruction had that the
        plain v2_shape test lacked: a LEADING method (yield m_delid) before
        the bare multi-allow join on the single tips template. Records
        whether the leading method changes the spawn outcome.
        """
        system, workflow, event_bus = await _build_v2_shape_system(
            loop=False, leading_method=True,
        )
        runtime = SystemRuntime(system, event_bus=event_bus)
        method_tracker = MethodTracker()
        runtime.register_plugin(method_tracker)
        await runtime.start()
        timed_out = False
        status = None
        try:
            submission = await runtime.submit(
                workflow, groups=[_owner_group()],
                mode=WorkflowRunMode.PURE_SIM,
            )
            # Diagnostic bed: a timeout is an OUTCOME here, not a failure.
            try:
                status = await asyncio.wait_for(
                    runtime.wait_for_execution(submission), timeout=25.0,
                )
            except asyncio.TimeoutError:
                timed_out = True
        finally:
            await runtime.shutdown()

        tips_threads = [
            name for name in method_tracker.thread_names.values()
            if name.startswith("tips")
        ]
        outcome = "TIMEOUT/STALL" if timed_out else (status.status if status else "?")
        print(
            f"\n[v2_shape bare+leading] outcome={outcome} "
            f"tips_instances_spawned={len(tips_threads)}"
        )
        assert not timed_out and status is not None, (
            f"bare join with a leading method must not stall; got {outcome}"
        )
        assert status.status == "completed", (
            f"bare join with a leading method completes; got {outcome}"
        )
        assert len(tips_threads) == 3, (
            f"a leading method before the bare multi-allow join still spawns "
            f"one tips instance per consuming method (3); got {len(tips_threads)}"
        )

    @pytest.mark.parametrize("n_cothreads", [2, 3, 4])
    @pytest.mark.asyncio
    async def test_nway_single_action_convergence(self, n_cothreads: int) -> None:
        """N distinct co-threads converging on ONE action (the reporter's
        original trigger: tagment_setup = library + sample +
        reagent + tips). Records whether all co-threads dispatch and the
        run completes, or whether convergence past 2-3 co-threads stalls.
        """
        system, workflow, event_bus = await _build_nway_convergence_system(n_cothreads)
        runtime = SystemRuntime(system, event_bus=event_bus)
        method_tracker = MethodTracker()
        runtime.register_plugin(method_tracker)
        await runtime.start()
        timed_out = False
        status = None
        try:
            submission = await runtime.submit(
                workflow, groups=[_owner_group()],
                mode=WorkflowRunMode.PURE_SIM,
            )
            # Diagnostic bed: a timeout is an OUTCOME here, not a failure.
            try:
                status = await asyncio.wait_for(
                    runtime.wait_for_execution(submission), timeout=25.0,
                )
            except asyncio.TimeoutError:
                timed_out = True
        finally:
            await runtime.shutdown()

        co_threads = [
            name for name in method_tracker.thread_names.values()
            if name.startswith("co_")
        ]
        outcome = "TIMEOUT/STALL" if timed_out else (status.status if status else "?")
        print(
            f"\n[nway n_cothreads={n_cothreads}] outcome={outcome} "
            f"co_threads_spawned={len(co_threads)} (expected {n_cothreads})"
        )
        assert not timed_out and status is not None, (
            f"n-way convergence (n={n_cothreads}) must not stall; got {outcome}"
        )
        assert status.status == "completed", (
            f"n-way convergence (n={n_cothreads}) completes; got {outcome}"
        )
        assert len(co_threads) == n_cothreads, (
            f"all {n_cothreads} co-threads must dispatch; got {len(co_threads)} "
            f"-- convergence past 2-3 co-threads did NOT stall in repro"
        )

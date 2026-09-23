"""Tests for orca.runtime.code_injection.

Validation, execution, and template extraction. Covers the path that
a hosted deployment's `thread_insert_method` / `thread_insert_action` will use to
turn wire-supplied Python source into runtime templates.
"""

import textwrap

import pytest

from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.code_injection import (
    CodeInjectionError,
    CodeValidationError,
    LiveTopology,
    compile_action_code,
    compile_method_code,
)
from orca.workflow_models.action_template import Action, ActionTemplate
from orca.workflow_models.method_template import (
    MethodTemplate,
    _PENDING_METHOD_TEMPLATES,
)
from tests.mock import UniversalMockDevice


class _FakeSystem:
    """Minimal ISystem stand-in for LiveTopology: just the device/pool
    lookups LiveTopology touches, backed by real Device/ResourcePool
    objects so identity assertions are meaningful."""

    def __init__(
        self,
        devices: list[UniversalMockDevice],
        pools: list[ResourcePool] | None = None,
    ) -> None:
        self._devices = {d.name: d for d in devices}
        self._pools = {p.name: p for p in (pools or [])}

    @property
    def devices(self) -> list[UniversalMockDevice]:
        return list(self._devices.values())

    @property
    def resource_pools(self) -> list[ResourcePool]:
        return list(self._pools.values())

    def get_device(self, name: str) -> UniversalMockDevice:
        return self._devices[name]  # KeyError if absent (LiveTopology catches)

    def get_resource_pool(self, name: str) -> ResourcePool:
        return self._pools[name]  # KeyError if absent (LiveTopology catches)


def _system_with(*device_names: str) -> _FakeSystem:
    devs = [UniversalMockDevice(n) for n in device_names]
    pool = ResourcePool("the_pool", list(devs))
    return _FakeSystem(devs, [pool])


def _live_topology(host: _FakeSystem) -> LiveTopology:
    # _FakeSystem stands in for the 4 ISystem members LiveTopology reads; the
    # ABC's other 76 abstract methods are out of scope for these tests.
    return LiveTopology(host)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# LiveTopology resolver
# ---------------------------------------------------------------------------


class TestLiveTopologyResolver:
    def test_device_returns_the_live_object(self) -> None:
        sys = _system_with("mlstar_1")
        topo = _live_topology(sys)
        from orca.resource_models.devices import Device
        assert topo.device("mlstar_1", Device) is sys.get_device("mlstar_1")

    def test_device_narrows_to_interface(self) -> None:
        from orca.devices.device_interfaces import IShaker
        sys = _system_with("shaker_1")
        topo = _live_topology(sys)
        assert topo.device("shaker_1", IShaker) is sys.get_device("shaker_1")

    def test_device_wrong_type_raises_typeerror(self) -> None:
        # A type the universal mock does NOT implement.
        from orca.resource_models.transporter import Transporter
        sys = _system_with("shaker_1")
        topo = _live_topology(sys)
        with pytest.raises(TypeError, match="expected Transporter"):
            topo.device("shaker_1", Transporter)

    def test_unknown_device_raises_keyerror_with_available(self) -> None:
        from orca.resource_models.devices import Device
        sys = _system_with("mlstar_1", "biotek_1")
        topo = _live_topology(sys)
        with pytest.raises(KeyError, match="biotek_1"):
            topo.device("nope", Device)

    def test_pool_returns_the_live_pool(self) -> None:
        sys = _system_with("d1", "d2")
        topo = _live_topology(sys)
        assert topo.pool("the_pool") is sys.get_resource_pool("the_pool")

    def test_unknown_pool_raises_keyerror_with_available(self) -> None:
        sys = _system_with("d1", "d2")
        topo = _live_topology(sys)
        with pytest.raises(KeyError, match="the_pool"):
            topo.pool("missing_pool")


# ---------------------------------------------------------------------------
# Namespace injection: orca + topology available to injected code
# ---------------------------------------------------------------------------


_DEVICE_ACTION_SRC = textwrap.dedent("""
    from orca.devices.device_interfaces import IShaker
    from orca.sdk.labware import AnyLabwareTemplate

    @orca.action(device=topology.device("shaker_1", IShaker), inputs=[AnyLabwareTemplate()])
    async def ad_hoc(ctx):
        await ctx.device(IShaker).shake(duration=1, speed=222)
""")


class TestNamespaceInjection:
    def test_orca_preinjected_no_import_needed(self) -> None:
        # No `import orca` in the source; the @orca.action decorator must
        # still resolve because `orca` is pre-injected.
        sys = _system_with("shaker_1")
        result = compile_action_code(_DEVICE_ACTION_SRC, _live_topology(sys))
        assert isinstance(result, Action)

    def test_injected_action_binds_the_live_device(self) -> None:
        sys = _system_with("shaker_1")
        result = compile_action_code(_DEVICE_ACTION_SRC, _live_topology(sys))
        # The whole correctness claim: the action targets the SAME object
        # the reservation system resolves against.
        assert result.resource_pool.resources[0] is sys.get_device("shaker_1")

    def test_topology_absent_when_not_provided(self) -> None:
        # Without a topology, referencing `topology` is a NameError ->
        # CodeInjectionError. Confirms topology is provided ONLY on the
        # device-targeting path, never leaks as an ambient global.
        with pytest.raises(CodeInjectionError, match="topology"):
            compile_action_code(_DEVICE_ACTION_SRC, None)

    def test_method_code_can_target_device_via_topology(self) -> None:
        src = textwrap.dedent("""
            from orca.devices.device_interfaces import IShaker
            from orca.sdk.labware import AnyLabwareTemplate

            @orca.action(device=topology.device("shaker_1", IShaker), inputs=[AnyLabwareTemplate()])
            async def step(ctx):
                await ctx.device(IShaker).shake(duration=1, speed=300)

            @orca.method
            async def ad_hoc_method(ctx):
                yield step
        """)
        sys = _system_with("shaker_1")
        template = compile_method_code(src, _live_topology(sys))
        assert isinstance(template, MethodTemplate)
        assert template.name == "ad_hoc_method"


class TestInjectedSourceCarriesForward:
    """The audit trail for `insert_action`/`replace_action`/`insert_method`/
    `replace_method` captures whatever compile_*_code returns via
    `@dangerous`'s JSON-safe projection (`_to_json_safe`, which prefers a
    `to_dict()`). Before this, the compiled object had no `to_dict()`, so
    the audit fell back to a bare repr and the actual injected source --
    the thing that ran against real hardware -- was unrecoverable
    afterwards. These pin that `injected_source` survives compilation and
    that `to_dict()` is what an audit consumer would actually read."""

    def test_compile_action_code_attaches_the_source_it_compiled(self) -> None:
        sys = _system_with("shaker_1")
        result = compile_action_code(_DEVICE_ACTION_SRC, _live_topology(sys))

        assert result.injected_source == _DEVICE_ACTION_SRC

    def test_action_to_dict_carries_the_injected_source(self) -> None:
        sys = _system_with("shaker_1")
        result = compile_action_code(_DEVICE_ACTION_SRC, _live_topology(sys))

        assert result.to_dict() == {
            "name": result.name, "injected_source": _DEVICE_ACTION_SRC,
        }

    def test_a_workflow_authored_action_has_no_injected_source(self) -> None:
        """An Action built by the normal @orca.action decorator path (not
        through compile_action_code) must not read as injected -- to_dict()
        distinguishes "this ran from a workflow file" from "this was spliced
        in at runtime", which is exactly the fact an operator reading the
        audit trail needs."""
        from orca.orca import action as orca_action
        from orca.sdk.labware import AnyLabwareTemplate
        from orca.workflow_models.action_context import ActionContext

        device = UniversalMockDevice("shaker_1")

        @orca_action(device=device, inputs=[AnyLabwareTemplate()])
        async def authored(ctx: ActionContext) -> None:
            pass

        assert isinstance(authored, ActionTemplate)
        assert authored.injected_source is None
        assert authored.to_dict() == {"name": "authored", "injected_source": None}

    def test_compile_method_code_attaches_the_source_it_compiled(self) -> None:
        src = textwrap.dedent("""
            @orca.method
            async def ad_hoc_method(ctx):
                yield
        """)
        template = compile_method_code(src, None)

        assert template.injected_source == src
        assert template.to_dict() == {
            "name": "ad_hoc_method", "injected_source": src,
        }


@pytest.mark.parametrize(
    "iface,call",
    [
        ("ISealer", "await ctx.device(ISealer).seal(temperature=170, duration=3.0)"),
        ("IShaker", "await ctx.device(IShaker).shake(duration=1, speed=500)"),
        ("ICentrifuge", "await ctx.device(ICentrifuge).centrifuge(g=500, duration=60)"),
        ("IReader", "await ctx.device(IReader).read('p.pro', 'o.csv')"),
        ("IDelidder", "await ctx.device(IDelidder).delid()"),
    ],
)
class TestManyDeviceTypesCompile:
    def test_each_interface_compiles_and_binds(self, iface: str, call: str) -> None:
        src = textwrap.dedent(f"""
            from orca.devices.device_interfaces import {iface}
            from orca.sdk.labware import AnyLabwareTemplate

            @orca.action(device=topology.device("dev", {iface}), inputs=[AnyLabwareTemplate()])
            async def ad_hoc(ctx):
                {call}
        """)
        sys = _system_with("dev")
        result = compile_action_code(src, _live_topology(sys))
        assert isinstance(result, Action)
        assert result.resource_pool.resources[0] is sys.get_device("dev")


class TestInputVarieties:
    def test_pool_device_and_multiple_typed_inputs(self) -> None:
        src = textwrap.dedent("""
            from orca.devices.device_interfaces import IShaker
            from orca.sdk.labware import PlateTemplate, TipRackTemplate

            plate = PlateTemplate("p", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
            tips = TipRackTemplate("t", labware_type="hamilton_96_tiprack_10uL_filter", with_tips=True)

            @orca.action(device=topology.pool("the_pool"), inputs=[plate, tips])
            async def ad_hoc(ctx):
                await ctx.device(IShaker).shake(duration=1, speed=100)
        """)
        sys = _system_with("d1", "d2")
        result = compile_action_code(src, _live_topology(sys))
        assert isinstance(result, Action)
        assert result.resource_pool is sys.get_resource_pool("the_pool")


class TestInjectedDeviceFailureModes:
    def test_unknown_device_name_surfaces_clean(self) -> None:
        src = textwrap.dedent("""
            from orca.devices.device_interfaces import IShaker
            from orca.sdk.labware import AnyLabwareTemplate

            @orca.action(device=topology.device("ghost", IShaker), inputs=[AnyLabwareTemplate()])
            async def ad_hoc(ctx):
                await ctx.device(IShaker).shake(duration=1, speed=1)
        """)
        sys = _system_with("shaker_1")
        with pytest.raises(CodeInjectionError, match="ghost|shaker_1"):
            compile_action_code(src, _live_topology(sys))

    def test_wrong_device_type_surfaces_clean(self) -> None:
        src = textwrap.dedent("""
            from orca.resource_models.transporter import Transporter
            from orca.sdk.labware import AnyLabwareTemplate

            @orca.action(device=topology.device("shaker_1", Transporter), inputs=[AnyLabwareTemplate()])
            async def ad_hoc(ctx):
                pass
        """)
        sys = _system_with("shaker_1")
        with pytest.raises(CodeInjectionError, match="expected Transporter"):
            compile_action_code(src, _live_topology(sys))


@pytest.fixture(autouse=True)
def reset_pending_methods():
    """Pending methods leak across tests if a previous test set up a system
    via the SDK. Snapshot and restore so each code_injection test sees a
    clean slate and doesn't pollute downstream tests."""
    saved = list(_PENDING_METHOD_TEMPLATES)
    _PENDING_METHOD_TEMPLATES.clear()
    yield
    _PENDING_METHOD_TEMPLATES[:] = saved


# -- Source rejection --------------------------------------------------------


class TestSyntaxValidation:
    def test_syntax_error_raises_with_location(self) -> None:
        with pytest.raises(CodeValidationError) as exc:
            compile_method_code("def broken(:\n  pass")
        assert exc.value.code == "syntax_error"
        assert exc.value.line >= 1


class TestImportAllowlist:
    def test_blocks_disallowed_top_level_import(self) -> None:
        with pytest.raises(CodeValidationError, match="forbidden_import"):
            compile_method_code("import os\n")

    def test_blocks_disallowed_from_import(self) -> None:
        with pytest.raises(CodeValidationError, match="forbidden_import"):
            compile_method_code("from os.path import join\n")

    def test_allows_orca_import(self) -> None:
        # An import alone won't produce a method, so the post-exec "no method"
        # error is what proves the import itself was accepted.
        with pytest.raises(CodeInjectionError, match="did not define"):
            compile_method_code("import orca.orca\n")

    def test_allows_cheshire_drivers_import(self) -> None:
        with pytest.raises(CodeInjectionError, match="did not define"):
            compile_method_code("import cheshire_drivers\n")

    def test_allows_typing_import(self) -> None:
        with pytest.raises(CodeInjectionError, match="did not define"):
            compile_method_code("from typing import Any\n")


class TestBlockedBuiltins:
    def test_blocks_eval_call(self) -> None:
        src = textwrap.dedent("""
            import orca.orca as orca
            x = eval("1 + 1")
            @orca.method
            async def m(ctx):
                yield None
        """)
        with pytest.raises(CodeValidationError, match="forbidden_builtin"):
            compile_method_code(src)

    def test_blocks_open_call(self) -> None:
        src = textwrap.dedent("""
            import orca.orca as orca
            f = open("/etc/passwd")
            @orca.method
            async def m(ctx):
                yield None
        """)
        with pytest.raises(CodeValidationError, match="forbidden_builtin"):
            compile_method_code(src)

    def test_blocks_a_builtin_reached_through_an_alias(self) -> None:
        """The stub raises at the call, so renaming the builtin first does not
        get past it."""
        src = textwrap.dedent("""
            import orca.orca as orca
            reader = open
            f = reader("/etc/passwd")
            @orca.method
            async def m(ctx):
                yield None
        """)
        with pytest.raises(CodeValidationError, match="forbidden_builtin"):
            compile_method_code(src)

    def test_blocks_a_dynamically_named_import(self) -> None:
        """A module named at runtime meets the same allow-list an import
        statement does."""
        src = textwrap.dedent("""
            import orca.orca as orca
            os = __import__("os")
            @orca.method
            async def m(ctx):
                yield None
        """)
        with pytest.raises(CodeValidationError, match="forbidden_import"):
            compile_method_code(src)

    def test_reflection_through_attributes_is_not_rejected(self) -> None:
        """Injected source is not sandboxed and this pins that we do not claim
        it is: an attribute chain off an already-imported object is reachable,
        so the source runs and fails only on the wire contract. The guards
        catch the accident an AI client makes, not a deliberate escape."""
        src = textwrap.dedent("""
            import orca.orca as orca
            x = ().__class__
        """)
        with pytest.raises(CodeInjectionError, match="did not define"):
            compile_method_code(src)


# -- Method compilation ------------------------------------------------------


class TestCompileMethodCode:
    def test_compiles_minimal_method(self) -> None:
        src = textwrap.dedent("""
            import orca.orca as orca

            @orca.method
            async def my_inserted_method(ctx):
                if False:
                    yield None
        """)
        template = compile_method_code(src)
        assert isinstance(template, MethodTemplate)
        assert template.name == "my_inserted_method"

    def test_no_method_raises_clean(self) -> None:
        src = textwrap.dedent("""
            import orca.orca as orca

            async def helper(ctx):
                pass
        """)
        with pytest.raises(CodeInjectionError, match="did not define"):
            compile_method_code(src)

    def test_multiple_methods_raises_clean(self) -> None:
        src = textwrap.dedent("""
            import orca.orca as orca

            @orca.method
            async def first(ctx):
                if False:
                    yield None

            @orca.method
            async def second(ctx):
                if False:
                    yield None
        """)
        with pytest.raises(CodeInjectionError, match="multiple @orca.method"):
            compile_method_code(src)

    def test_does_not_leak_into_pending_queue(self) -> None:
        """Runtime injection must not leak into _PENDING_METHOD_TEMPLATES
        (drained by SdkToSystemBuilder at build time)."""
        src = textwrap.dedent("""
            import orca.orca as orca

            @orca.method
            async def m(ctx):
                if False:
                    yield None
        """)
        compile_method_code(src)
        assert _PENDING_METHOD_TEMPLATES == []

    def test_exec_failure_rewinds_pending_queue(self) -> None:
        """If exec raises after registering a method, the queue must be
        rewound to its pre-exec state so subsequent build-time drains
        are not corrupted."""
        src = textwrap.dedent("""
            import orca.orca as orca

            @orca.method
            async def m(ctx):
                if False:
                    yield None

            raise ValueError("bang")
        """)
        with pytest.raises(CodeInjectionError, match="raised during execution"):
            compile_method_code(src)
        assert _PENDING_METHOD_TEMPLATES == []


# -- Action compilation ------------------------------------------------------


class TestCompileActionCode:
    def test_no_action_raises_clean(self) -> None:
        src = textwrap.dedent("""
            import orca.orca as orca

            async def not_an_action(ctx):
                pass
        """)
        with pytest.raises(CodeInjectionError, match="did not define"):
            compile_action_code(src)

    def test_collects_single_action_from_namespace(self) -> None:
        """compile_action_code walks the post-exec namespace for `Action`
        instances. Constructing a real @orca.action requires the full
        Device/ResourcePool/LabwareTemplate stack; we sidestep that by
        defining an Action subclass in the injected source that bypasses
        the parent __init__. The exec path + namespace walk are what
        we're verifying here; the real `@orca.action` decorator compiled
        and run end to end lives in test_code_injection_execution.py.
        """
        src = textwrap.dedent("""
            from orca.workflow_models.action_template import Action

            class _BareAction(Action):
                def __init__(self):
                    pass

            my_action = _BareAction()
        """)
        result = compile_action_code(src)
        from orca.workflow_models.action_template import Action
        assert isinstance(result, Action)

    def test_multiple_actions_rejected(self) -> None:
        """Source with more than one Action instance must raise; the
        wire surface accepts exactly one action per insert call.
        """
        src = textwrap.dedent("""
            from orca.workflow_models.action_template import Action

            class _BareAction(Action):
                def __init__(self, label):
                    self._operation_name = label

            first = _BareAction("first")
            second = _BareAction("second")
        """)
        with pytest.raises(CodeInjectionError, match="multiple"):
            compile_action_code(src)

    def test_underscore_prefixed_actions_ignored(self) -> None:
        """Names starting with underscore are skipped during the walk
        so test scaffolding / temp Action instances don't accidentally
        count toward the one-action requirement.
        """
        src = textwrap.dedent("""
            from orca.workflow_models.action_template import Action

            class _BareAction(Action):
                def __init__(self):
                    pass

            _scratch = _BareAction()
            keep = _BareAction()
        """)
        result = compile_action_code(src)
        from orca.workflow_models.action_template import Action
        assert isinstance(result, Action)

    def test_exec_failure_raises_code_injection_error(self) -> None:
        """A runtime error during exec (post-AST-validation) surfaces as
        CodeInjectionError, not a bare Exception."""
        src = textwrap.dedent("""
            raise RuntimeError("simulated exec failure")
        """)
        with pytest.raises(CodeInjectionError, match="raised during execution"):
            compile_action_code(src)

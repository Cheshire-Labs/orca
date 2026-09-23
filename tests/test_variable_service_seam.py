"""Service-over-Store split for the variable system.

Covers three things the genuine-layering refactor introduced:
- the dumb ``VariableStore`` primitive accessors in isolation (no resolution),
  which are exactly the ``IVariableStore`` persistence-swap seam,
- ``VariableService`` orchestration: delegation + the full consumer API. It is
  the ``IVariableResolver`` authored code depends on (as is ``NullVariableResolver``)
  and runs over any ``IVariableStore`` implementation,
- the injection seam: ``build_system`` builds the System around the store the
  ``IRuntimeStoreFactory`` hands out.
"""

import threading

import pytest

from orca.runtime.store_factory import InMemoryRuntimeStoreFactory, IRuntimeStoreFactory
from orca.sdk.build import Topology, build_system
from orca.variables import (
    DeploymentProfile,
    IVariableResolver,
    IVariableStore,
    NullVariableResolver,
    VariableDefinition,
    VariableService,
    VariableStore,
)
from tests.daemon.daemon_test_fixture_topology import build_topology


# The primitive accessors the dumb store exposes = the IVariableStore seam.
_IVARIABLE_STORE_PRIMITIVES = [
    "register_global_definitions", "get_global_definition", "iter_global_definitions",
    "register_workflow_definitions", "get_workflow_definition", "iter_workflow_definitions",
    "register_computed", "get_computed", "has_computed", "iter_computed_names",
    "create_execution", "remove_execution", "has_execution", "workflow_for",
    "get_global_value", "set_global_value", "pop_global_value", "has_global_value",
    "iter_global_values", "get_execution_value", "set_execution_value",
    "pop_execution_value", "has_execution_value", "iter_execution_values",
    "get_submission_value", "set_submission_value", "pop_submission_value",
    "has_submission_value", "iter_submission_ids", "iter_submission_partitions",
]

# The full consumer API only VariableService carries.
_VARIABLE_SERVICE_API = [
    "resolve", "explain", "resolve_global", "has",
    "has_global_value", "has_execution_value", "has_submission_value",
    "set", "set_submission", "unset", "unset_submission", "set_global",
    "unset_global", "load_profile",
    "register_global_definitions", "register_workflow_definitions", "register_computed",
    "create_execution", "remove_execution", "get_all", "get_all_with_source",
]


# ===========================================================================
# DUMB STORE PRIMITIVES (no resolution, no validation, no lock)
# ===========================================================================

class TestVariableStorePrimitives:

    def test_is_the_primitive_store_seam_not_a_resolver(self) -> None:
        """The dumb store IS the IVariableStore primitive seam (every accessor),
        but NOT a resolver: it carries no resolution surface (resolve /
        get_all_with_source) and holds no lock."""
        store: IVariableStore = VariableStore()
        for method in _IVARIABLE_STORE_PRIMITIVES:
            assert callable(getattr(store, method))
        assert not hasattr(store, "resolve")
        assert not hasattr(store, "get_all_with_source")
        assert not hasattr(store, "_lock")

    def test_execution_lifecycle_primitives(self) -> None:
        store = VariableStore()
        assert store.has_execution("e1") is False
        store.create_execution("e1", "wf1")
        assert store.has_execution("e1") is True
        assert store.workflow_for("e1") == "wf1"
        store.remove_execution("e1")
        assert store.has_execution("e1") is False
        assert store.workflow_for("e1") is None

    def test_create_execution_is_idempotent(self) -> None:
        store = VariableStore()
        store.create_execution("e1", "wf1")
        store.set_execution_value("e1", "x", 7)
        store.create_execution("e1", "wf1")  # no-op, must not clear the partition
        assert store.get_execution_value("e1", "x") == 7

    def test_global_value_layer(self) -> None:
        store = VariableStore()
        assert store.has_global_value("g") is False
        store.set_global_value("g", 3)
        assert store.has_global_value("g") is True
        assert store.get_global_value("g") == 3
        assert dict(store.iter_global_values()) == {"g": 3}
        store.pop_global_value("g")
        assert store.has_global_value("g") is False

    def test_execution_value_layer(self) -> None:
        store = VariableStore()
        store.create_execution("e1", "wf1")
        store.set_execution_value("e1", "x", 1)
        assert store.has_execution_value("e1", "x") is True
        assert store.get_execution_value("e1", "x") == 1
        assert dict(store.iter_execution_values("e1")) == {"x": 1}
        store.pop_execution_value("e1", "x")
        assert store.has_execution_value("e1", "x") is False

    def test_submission_value_layer(self) -> None:
        store = VariableStore()
        store.create_execution("e1", "wf1")
        store.set_submission_value("e1", "sub-a", "x", 9)
        assert store.has_submission_value("e1", "sub-a", "x") is True
        assert store.get_submission_value("e1", "sub-a", "x") == 9
        partitions = list(store.iter_submission_partitions("e1"))
        assert partitions == [{"x": 9}]
        # remove_execution clears submission partitions too
        store.remove_execution("e1")
        assert store.has_submission_value("e1", "sub-a", "x") is False

    def test_value_getters_raise_keyerror_on_absence(self) -> None:
        """None is never a valid OptionValue, so absence is KeyError, not None."""
        store = VariableStore()
        store.create_execution("e1", "wf1")
        with pytest.raises(KeyError):
            store.get_global_value("missing")
        with pytest.raises(KeyError):
            store.get_execution_value("e1", "missing")

    def test_definition_and_computed_lookup(self) -> None:
        store = VariableStore()
        store.register_global_definitions({"g": VariableDefinition(type="int", default=1)})
        store.register_workflow_definitions("wf1", {"w": VariableDefinition(type="int", default=2)})
        store.register_computed({"c": "g + w"})
        assert store.get_global_definition("g") is not None
        assert store.get_workflow_definition("wf1", "w") is not None
        assert store.get_workflow_definition("wf1", "absent") is None
        assert store.has_computed("c") is True
        assert store.get_computed("c") == "g + w"
        assert list(store.iter_computed_names()) == ["c"]

    def test_workflow_definition_reserved_name_rejected(self) -> None:
        store = VariableStore()
        with pytest.raises(ValueError, match="reserved"):
            store.register_workflow_definitions("wf1", {"global": VariableDefinition(type="str")})


# ===========================================================================
# SERVICE ORCHESTRATION + CONTRACT
# ===========================================================================

class TestVariableServiceContract:

    def test_service_is_a_resolver_and_carries_the_full_api(self) -> None:
        service: IVariableResolver = VariableService(VariableStore())
        for method in _VARIABLE_SERVICE_API:
            assert callable(getattr(service, method))

    def test_service_runs_over_the_ivariable_store_interface(self) -> None:
        """The persistence seam: VariableService takes any IVariableStore and
        resolves through it without reimplementing resolution."""
        store: IVariableStore = VariableStore()
        service = VariableService(store)
        service.register_workflow_definitions("wf1", {"x": VariableDefinition(type="int", default=7)})
        service.create_execution("e1", "wf1")
        assert service.resolve("x", "e1") == 7

    def test_null_store_is_a_resolver(self) -> None:
        null: IVariableResolver = NullVariableResolver()
        assert callable(getattr(null, "resolve"))

    def test_null_store_resolver_semantics(self) -> None:
        from orca.variables import UndefinedVariableError
        null = NullVariableResolver()
        with pytest.raises(UndefinedVariableError):
            null.resolve("x", "e1")
        with pytest.raises(UndefinedVariableError):
            null.resolve_global("x")
        assert null.has("x", "e1") is False

    def test_service_holds_a_lock_and_delegates_to_its_store(self) -> None:
        store = VariableStore()
        service = VariableService(store)
        assert isinstance(service._lock, type(threading.Lock()))
        service.create_execution("e1", "wf1")
        service.set("x", 5, "e1")
        # The mutation landed in the wrapped store's primitive layer.
        assert store.get_execution_value("e1", "x") == 5

    def test_service_validates_on_set(self) -> None:
        from orca.variables import VariableValidationError
        service = VariableService(VariableStore())
        service.register_workflow_definitions("wf1", {"speed": VariableDefinition(type="int", min=0, max=10)})
        service.create_execution("e1", "wf1")
        service.set("speed", 5, "e1")
        with pytest.raises(VariableValidationError):
            service.set("speed", 99, "e1")

    def test_service_resolution_priority_matches_layers(self) -> None:
        service = VariableService(VariableStore())
        service.register_workflow_definitions("wf1", {"x": VariableDefinition(type="int", default=1)})
        service.create_execution("e1", "wf1")
        assert service.resolve("x", "e1") == 1            # workflow default
        service.set("x", 2, "e1")
        assert service.resolve("x", "e1") == 2            # execution overrides default
        service.set_submission("x", 3, "e1", "sub-a")
        assert service.resolve("x", "e1", "sub-a") == 3   # submission overrides execution
        assert service.resolve("x", "e1") == 2            # no submission id -> execution


# ===========================================================================
# INJECTION SEAM
# ===========================================================================

class _SpyStoreFactory(InMemoryRuntimeStoreFactory):
    """Hands out one known variable store so the seam can be asserted by identity."""

    def __init__(self) -> None:
        super().__init__()
        self.injected = VariableService(VariableStore())

    def variable_store(self) -> VariableService:
        return self.injected


class TestVariableSeam:

    def test_factory_returns_a_fresh_store_per_call(self) -> None:
        factory = InMemoryRuntimeStoreFactory()
        first = factory.variable_store()
        second = factory.variable_store()
        assert isinstance(first, VariableService)
        assert first is not second

    async def test_build_system_uses_the_injected_variable_store(self) -> None:
        factory: IRuntimeStoreFactory = _SpyStoreFactory()
        topology: Topology = build_topology(factory)
        build = await build_system("sys", topology, factory)
        assert build.system.variable_store is factory.injected

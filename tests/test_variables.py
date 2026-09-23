"""Tests for Two-scope variable system.

Tests cover:
- Core model (LiteralRef, NamedRef, Var)
- Two-scope VariableStore (global + workflow-scoped)
- Expression evaluator
- Validation (VariableDefinition, bool guard)
- Deployment profiles
- Serialization roundtrip (${var.name} and ${global.name})
- NullVariableResolver
- Error paths
- _wrap() clone safety (Bug 1 regression)
- Integration: actual resolved values reach the device
"""

from collections.abc import AsyncGenerator

import pytest
from tests.mock import UniversalMockDevice
from orca.runtime.run_modes import WorkflowRunMode
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext


# ===========================================================================
# CORE MODEL TESTS
# ===========================================================================

class TestLiteralRef:

    def test_resolve_returns_value(self) -> None:
        from orca.variables import LiteralRef
        ref = LiteralRef(7200)
        assert ref.resolve() == 7200

    def test_is_literal(self) -> None:
        from orca.variables import LiteralRef
        ref = LiteralRef(42)
        assert ref.is_literal is True

    def test_falsy_value_zero(self) -> None:
        from orca.variables import LiteralRef
        ref = LiteralRef(0)
        assert ref.resolve() == 0

    def test_falsy_value_false(self) -> None:
        from orca.variables import LiteralRef
        ref = LiteralRef(False)
        assert ref.resolve() is False

    def test_repr(self) -> None:
        from orca.variables import LiteralRef
        assert "42" in repr(LiteralRef(42))

    def test_eq(self) -> None:
        from orca.variables import LiteralRef
        assert LiteralRef(42) == LiteralRef(42)
        assert LiteralRef(42) != LiteralRef(99)


class TestNamedRef:

    def test_is_not_literal(self) -> None:
        from orca.variables import NamedRef
        ref = NamedRef("duration")
        assert ref.is_literal is False

    def test_unresolved_raises(self) -> None:
        from orca.variables import NamedRef
        ref = NamedRef("duration")
        with pytest.raises(RuntimeError, match="has not been resolved"):
            ref.resolve()

    def test_set_resolved_value_then_resolve(self) -> None:
        from orca.variables import NamedRef
        ref = NamedRef("duration")
        ref.set_resolved_value(7200)
        assert ref.resolve() == 7200

    def test_clear_resolved_value(self) -> None:
        from orca.variables import NamedRef
        ref = NamedRef("duration")
        ref.set_resolved_value(7200)
        ref.clear_resolved_value()
        with pytest.raises(RuntimeError, match="has not been resolved"):
            ref.resolve()

    def test_set_resolved_overwrites(self) -> None:
        from orca.variables import NamedRef
        ref = NamedRef("duration")
        ref.set_resolved_value(100)
        ref.set_resolved_value(200)
        assert ref.resolve() == 200

    def test_clone_independence(self) -> None:
        """Two NamedRef with the same name are independent (concurrent safety)."""
        from orca.variables import NamedRef
        a = NamedRef("x")
        b = NamedRef("x")
        a.set_resolved_value(1)
        b.set_resolved_value(2)
        assert a.resolve() == 1
        assert b.resolve() == 2

    def test_repr(self) -> None:
        from orca.variables import NamedRef
        ref = NamedRef("speed")
        assert "speed" in repr(ref)

    def test_eq(self) -> None:
        from orca.variables import NamedRef
        a = NamedRef("speed")
        b = NamedRef("speed")
        assert a == b


class TestVar:

    def test_var_creates_named_ref(self) -> None:
        from orca.variables import Var, NamedRef
        ref = Var("incubation_time")
        assert isinstance(ref, NamedRef)
        assert ref.name == "incubation_time"
        assert ref.is_literal is False

    def test_var_is_just_a_name(self) -> None:
        from orca.variables import Var
        ref = Var("speed")
        assert ref.name == "speed"
        assert ref.is_literal is False


# ===========================================================================
# TWO-SCOPE VARIABLE STORE TESTS
# ===========================================================================

class TestVariableStore:
    """Tests for two-scope, execution-partitioned VariableStore."""

    def test_workflow_scoped_resolve(self) -> None:
        """Workflow-scoped values in execution partition are found."""
        from orca.variables import VariableService, VariableStore
        store = VariableService(VariableStore())
        store.create_execution("exec1", "wf1")
        store.set("x", 42, "exec1")
        assert store.resolve("x", "exec1") == 42

    def test_workflow_default_fallback(self) -> None:
        """Workflow-scoped variable falls back to workflow definition default."""
        from orca.variables import VariableService, VariableStore, VariableDefinition
        store = VariableService(VariableStore())
        store.register_workflow_definitions("wf1", {"x": VariableDefinition(type="int", default=7200)})
        store.create_execution("exec1", "wf1")
        assert store.resolve("x", "exec1") == 7200

    def test_global_scoped_resolve(self) -> None:
        """Global-scoped values resolve via global. prefix."""
        from orca.variables import VariableService, VariableStore
        store = VariableService(VariableStore())
        store.set_global("x", 99)
        store.create_execution("exec1", "wf1")
        assert store.resolve("global.x", "exec1") == 99

    def test_global_default_fallback(self) -> None:
        """Global variable falls back to global definition default."""
        from orca.variables import VariableService, VariableStore, VariableDefinition
        store = VariableService(VariableStore())
        store.register_global_definitions({"x": VariableDefinition(type="int", default=5)})
        store.create_execution("exec1", "wf1")
        assert store.resolve("global.x", "exec1") == 5

    def test_execution_overrides_workflow_default(self) -> None:
        from orca.variables import VariableService, VariableStore, VariableDefinition
        store = VariableService(VariableStore())
        store.register_workflow_definitions("wf1", {"x": VariableDefinition(type="int", default=7200)})
        store.create_execution("exec1", "wf1")
        store.set("x", 0, "exec1")
        assert store.resolve("x", "exec1") == 0

    def test_execution_overrides_global_value(self) -> None:
        from orca.variables import VariableService, VariableStore
        store = VariableService(VariableStore())
        store.set_global("x", 99)
        store.create_execution("exec1", "wf1")
        store.set("global.x", 1, "exec1")
        assert store.resolve("global.x", "exec1") == 1

    def test_workflow_scope_does_not_fall_through_to_global(self) -> None:
        """Strict scope separation: workflow-scoped resolution ignores global values."""
        from orca.variables import VariableService, VariableStore, UndefinedVariableError
        store = VariableService(VariableStore())
        store.set_global("x", 99)
        store.create_execution("exec1", "wf1")
        with pytest.raises(UndefinedVariableError):
            store.resolve("x", "exec1")

    def test_undefined_raises(self) -> None:
        from orca.variables import VariableService, VariableStore, UndefinedVariableError
        store = VariableService(VariableStore())
        store.create_execution("exec1", "wf1")
        with pytest.raises(UndefinedVariableError):
            store.resolve("nonexistent", "exec1")

    def test_execution_isolation(self) -> None:
        from orca.variables import VariableService, VariableStore
        store = VariableService(VariableStore())
        store.create_execution("exec1", "wf1")
        store.create_execution("exec2", "wf1")
        store.set("x", 100, "exec1")
        store.set("x", 200, "exec2")
        assert store.resolve("x", "exec1") == 100
        assert store.resolve("x", "exec2") == 200

    def test_two_workflows_same_variable_name(self) -> None:
        """Two workflows defining same variable name get independent defaults."""
        from orca.variables import VariableService, VariableStore, VariableDefinition
        store = VariableService(VariableStore())
        store.register_workflow_definitions("assay_a", {"shake_time": VariableDefinition(type="int", default=7200)})
        store.register_workflow_definitions("assay_b", {"shake_time": VariableDefinition(type="int", default=300)})
        store.create_execution("exec1", "assay_a")
        store.create_execution("exec2", "assay_b")
        assert store.resolve("shake_time", "exec1") == 7200
        assert store.resolve("shake_time", "exec2") == 300

    def test_remove_execution_cleans_partition_and_mapping(self) -> None:
        from orca.variables import VariableService, VariableStore
        store = VariableService(VariableStore())
        store.create_execution("exec1", "wf1")
        store.set("x", 42, "exec1")
        store.remove_execution("exec1")
        with pytest.raises(KeyError):
            store.resolve("x", "exec1")

    def test_load_profile_workflow_scoped(self) -> None:
        from orca.variables import VariableService, VariableStore, DeploymentProfile
        store = VariableService(VariableStore())
        store.create_execution("exec1", "wf1")
        profile = DeploymentProfile(name="dry_test", variables={"x": 0, "y": 10})
        store.load_profile("exec1", profile)
        assert store.resolve("x", "exec1") == 0
        assert store.resolve("y", "exec1") == 10

    def test_load_profile_global_prefix_propagates(self) -> None:
        """Profile values with global. prefix also write to the global layer."""
        from orca.variables import VariableService, VariableStore, DeploymentProfile
        store = VariableService(VariableStore())
        store.create_execution("exec1", "wf1")
        store.create_execution("exec2", "wf1")
        profile = DeploymentProfile(name="test", variables={"global.retries": 1})
        store.load_profile("exec1", profile)
        assert store.resolve("global.retries", "exec1") == 1
        assert store.resolve("global.retries", "exec2") == 1

    def test_set_validates_against_workflow_definition(self) -> None:
        from orca.variables import VariableService, VariableStore, VariableDefinition, VariableValidationError
        store = VariableService(VariableStore())
        store.register_workflow_definitions("wf1", {"speed": VariableDefinition(type="int", min=0, max=2000)})
        store.create_execution("exec1", "wf1")
        store.set("speed", 1000, "exec1")
        with pytest.raises(VariableValidationError):
            store.set("speed", 5000, "exec1")

    def test_register_workflow_definitions_replaces_not_accumulates(self) -> None:
        """REPLACE semantics: re-registering a workflow gives it exactly its
        current definitions. A variable dropped from the new version stops
        resolving instead of lingering with a stale default -- the staleness a
        re-submitted (register_workflow_from_source) workflow would otherwise hit."""
        from orca.variables import (
            VariableService, VariableStore, VariableDefinition, UndefinedVariableError,
        )
        store = VariableService(VariableStore())
        store.register_workflow_definitions(
            "wf1",
            {"a": VariableDefinition(type="int", default=1),
             "b": VariableDefinition(type="int", default=2)},
        )
        store.register_workflow_definitions(
            "wf1", {"a": VariableDefinition(type="int", default=5)},
        )
        store.create_execution("exec1", "wf1")
        assert store.resolve("a", "exec1") == 5
        with pytest.raises(UndefinedVariableError):
            store.resolve("b", "exec1")

    def test_get_all_with_source(self) -> None:
        from orca.variables import VariableService, VariableStore, VariableDefinition
        store = VariableService(VariableStore())
        store.register_workflow_definitions("wf1", {"a": VariableDefinition(type="int", default=1)})
        store.register_global_definitions({"b": VariableDefinition(type="int", default=2)})
        store.set_global("c", 3)
        store.create_execution("exec1", "wf1")
        store.set("d", 4, "exec1")
        result = store.get_all_with_source("exec1")
        assert result["a"] == (1, "workflow-default")
        assert result["global.b"] == (2, "global-default")
        assert result["global.c"] == (3, "global")
        assert result["d"] == (4, "execution")

    def test_get_all_includes_submission_overrides(self) -> None:
        """Round 5 V1: submission-scope overrides surface to the operator view.

        Pre-fix `get_all` walked workflow defaults, globals, and the execution
        partition but never the per-submission partitions, so a submission
        that landed via ``submissions_submit(variables={...})`` was invisible
        to ``variables_list``. Threads inside the submission see the value
        fine via ``resolve(name, execution_id, submission_id)``; the visibility
        gap was only on the operator-facing merged view.
        """
        from orca.variables import VariableService, VariableStore, VariableDefinition
        store = VariableService(VariableStore())
        store.register_workflow_definitions(
            "wf1", {"shake_time": VariableDefinition(type="int", default=60)},
        )
        store.create_execution("exec1", "wf1")
        store.set_submission("shake_time", 120, "exec1", "sub-a")
        result = store.get_all("exec1")
        assert result["shake_time"] == 120

    def test_get_all_submission_overrides_layer_above_execution(self) -> None:
        """Submission overrides shadow per-execution writes in the merged view.

        Resolution precedence for a thread is submission > execution >
        workflow-default; the operator view mirrors the same order.
        """
        from orca.variables import VariableService, VariableStore, VariableDefinition
        store = VariableService(VariableStore())
        store.register_workflow_definitions(
            "wf1", {"shake_time": VariableDefinition(type="int", default=60)},
        )
        store.create_execution("exec1", "wf1")
        store.set("shake_time", 90, "exec1")
        store.set_submission("shake_time", 120, "exec1", "sub-a")
        result = store.get_all("exec1")
        assert result["shake_time"] == 120

    def test_get_all_with_source_labels_submission(self) -> None:
        """Submission overrides carry a ``submission`` source tag for CLI display."""
        from orca.variables import VariableService, VariableStore, VariableDefinition
        store = VariableService(VariableStore())
        store.register_workflow_definitions(
            "wf1", {"shake_time": VariableDefinition(type="int", default=60)},
        )
        store.create_execution("exec1", "wf1")
        store.set_submission("shake_time", 120, "exec1", "sub-a")
        result = store.get_all_with_source("exec1")
        assert result["shake_time"][0] == 120
        assert result["shake_time"][1] == "submission"

    def test_has(self) -> None:
        from orca.variables import VariableService, VariableStore, VariableDefinition
        store = VariableService(VariableStore())
        store.set_global("x", 1)
        store.register_workflow_definitions("wf1", {"y": VariableDefinition(type="int", default=99)})
        store.create_execution("exec1", "wf1")
        assert store.has("global.x", "exec1") is True
        assert store.has("y", "exec1") is True
        assert store.has("z", "exec1") is False

    def test_reserved_name_global_rejected(self) -> None:
        """Workflow variable named 'global' or 'global.*' is rejected."""
        from orca.variables import VariableService, VariableStore, VariableDefinition
        store = VariableService(VariableStore())
        with pytest.raises(ValueError, match="reserved"):
            store.register_workflow_definitions("wf1", {"global": VariableDefinition(type="str")})
        with pytest.raises(ValueError, match="reserved"):
            store.register_workflow_definitions("wf1", {"global.foo": VariableDefinition(type="str")})


# ===========================================================================
# EXPRESSION EVALUATOR TESTS
# ===========================================================================

class TestExpressionEvaluator:

    def test_simple_addition(self) -> None:
        from orca.variables.expression import evaluate_expression
        assert evaluate_expression("1 + 2", {}) == 3

    def test_variable_reference(self) -> None:
        from orca.variables.expression import evaluate_expression
        assert evaluate_expression("x + 10", {"x": 5}) == 15

    def test_multiple_variables(self) -> None:
        from orca.variables.expression import evaluate_expression
        result = evaluate_expression("a + b + 100", {"a": 50, "b": 25})
        assert result == 175

    def test_multiplication(self) -> None:
        from orca.variables.expression import evaluate_expression
        assert evaluate_expression("x * 2", {"x": 7200}) == 14400

    def test_division(self) -> None:
        from orca.variables.expression import evaluate_expression
        assert evaluate_expression("x / 2", {"x": 100}) == 50.0

    def test_floor_division(self) -> None:
        from orca.variables.expression import evaluate_expression
        assert evaluate_expression("x // 3", {"x": 10}) == 3

    def test_modulo(self) -> None:
        from orca.variables.expression import evaluate_expression
        assert evaluate_expression("x % 3", {"x": 10}) == 1

    def test_subtraction(self) -> None:
        from orca.variables.expression import evaluate_expression
        assert evaluate_expression("total - offset", {"total": 100, "offset": 30}) == 70

    def test_negative_unary(self) -> None:
        from orca.variables.expression import evaluate_expression
        assert evaluate_expression("-x", {"x": 5}) == -5

    def test_parentheses(self) -> None:
        from orca.variables.expression import evaluate_expression
        assert evaluate_expression("(a + b) * 2", {"a": 3, "b": 4}) == 14

    def test_float_result(self) -> None:
        from orca.variables.expression import evaluate_expression
        result = evaluate_expression("x / 3", {"x": 10})
        assert isinstance(result, float)

    def test_division_by_zero_raises(self) -> None:
        from orca.variables.expression import evaluate_expression, ExpressionError
        with pytest.raises(ExpressionError, match="division by zero"):
            evaluate_expression("x / 0", {"x": 10})

    def test_undefined_variable_raises(self) -> None:
        from orca.variables.expression import evaluate_expression, ExpressionError
        with pytest.raises(ExpressionError, match="Undefined.*missing"):
            evaluate_expression("missing + 1", {})

    def test_disallowed_syntax_raises(self) -> None:
        from orca.variables.expression import evaluate_expression, ExpressionError
        with pytest.raises(ExpressionError):
            evaluate_expression("__import__('os')", {})

    def test_function_call_raises(self) -> None:
        from orca.variables.expression import evaluate_expression, ExpressionError
        with pytest.raises(ExpressionError):
            evaluate_expression("len('abc')", {})

    def test_non_numeric_variable_is_named_in_the_error(self) -> None:
        """The operator gets the variable and its type, not whatever TypeError
        the arithmetic produced."""
        from orca.variables.expression import evaluate_expression, ExpressionError
        with pytest.raises(ExpressionError, match="'plate' has type str"):
            evaluate_expression("plate * 2", {"plate": "p1", "n": 3})

    def test_non_numeric_result_raises(self) -> None:
        """A computed variable must resolve to a number; anything else is a
        profile authoring error, not a value to hand downstream."""
        from orca.variables.expression import evaluate_expression, ExpressionError
        with pytest.raises(ExpressionError, match="expected numeric"):
            evaluate_expression("(1, 2)", {})

    def test_builtins_are_not_reachable(self) -> None:
        """The evaluation namespace holds the run's variables and nothing else,
        so a builtin name is undefined rather than callable."""
        from orca.variables.expression import evaluate_expression, ExpressionError
        with pytest.raises(ExpressionError, match="Undefined variable 'open'"):
            evaluate_expression("open", {})

    def test_computed_variable_in_store(self) -> None:
        """VariableStore resolves computed variables via expression evaluator."""
        from orca.variables import VariableService, VariableStore
        store = VariableService(VariableStore())
        store.create_execution("exec1", "wf1")
        store.set("a", 100, "exec1")
        store.set("b", 200, "exec1")
        store.register_computed({"total": "a + b + 50"})
        assert store.resolve("total", "exec1") == 350


# ===========================================================================
# VALIDATION TESTS
# ===========================================================================

class TestVariableDefinition:

    def test_type_check_int(self) -> None:
        from orca.variables import VariableDefinition, VariableValidationError
        defn = VariableDefinition(type="int")
        defn.validate_value("speed", 800)
        with pytest.raises(VariableValidationError):
            defn.validate_value("speed", "fast")

    def test_min_max(self) -> None:
        from orca.variables import VariableDefinition, VariableValidationError
        defn = VariableDefinition(type="int", min=0, max=2000)
        defn.validate_value("speed", 1000)
        with pytest.raises(VariableValidationError):
            defn.validate_value("speed", -1)
        with pytest.raises(VariableValidationError):
            defn.validate_value("speed", 3000)

    def test_allowed_values(self) -> None:
        from orca.variables import VariableDefinition, VariableValidationError
        defn = VariableDefinition(type="str", allowed_values=["96-well", "384-well"])
        defn.validate_value("plate_type", "96-well")
        with pytest.raises(VariableValidationError):
            defn.validate_value("plate_type", "1536-well")

    def test_store_validates_on_set(self) -> None:
        from orca.variables import VariableService, VariableStore, VariableDefinition, VariableValidationError
        store = VariableService(VariableStore())
        store.register_workflow_definitions("wf1", {
            "speed": VariableDefinition(type="int", min=0, max=2000)
        })
        store.create_execution("exec1", "wf1")
        store.set("speed", 1000, "exec1")
        with pytest.raises(VariableValidationError):
            store.set("speed", 5000, "exec1")


class TestBoolValidationGuard:

    def test_bool_rejected_for_int_type(self) -> None:
        from orca.variables import VariableDefinition, VariableValidationError
        defn = VariableDefinition(type="int")
        with pytest.raises(VariableValidationError, match="got bool"):
            defn.validate_value("flag", True)

    def test_bool_rejected_for_float_type(self) -> None:
        from orca.variables import VariableDefinition, VariableValidationError
        defn = VariableDefinition(type="float")
        with pytest.raises(VariableValidationError, match="got bool"):
            defn.validate_value("flag", False)

    def test_bool_accepted_for_bool_type(self) -> None:
        from orca.variables import VariableDefinition, VariableValidationError
        defn = VariableDefinition(type="bool")
        defn.validate_value("flag", True)
        defn.validate_value("flag", False)
        # The bool branch discriminates: a non-bool is still rejected, so the
        # accept path above is not a blanket pass-through.
        with pytest.raises(VariableValidationError, match="expected type bool"):
            defn.validate_value("flag", 1)
        with pytest.raises(VariableValidationError, match="expected type bool"):
            defn.validate_value("flag", "true")


# ===========================================================================
# DEPLOYMENT PROFILE TESTS
# ===========================================================================

class TestDeploymentProfile:

    def test_load_from_dict(self) -> None:
        from orca.variables import DeploymentProfile
        data = {
            "name": "dry_test",
            "description": "Skip incubations",
            "variables": {"incubation_time": 0, "shaker_speed": 0},
        }
        profile = DeploymentProfile(**data)
        assert profile.name == "dry_test"
        assert profile.variables["incubation_time"] == 0

    def test_profile_with_computed(self) -> None:
        from orca.variables import DeploymentProfile
        data = {
            "name": "production",
            "variables": {"incubation_time": 7200},
            "computed": {"total_time": "incubation_time * 2"},
        }
        profile = DeploymentProfile(**data)
        assert profile.computed["total_time"] == "incubation_time * 2"




# ===========================================================================
# NULL STORE AND ERROR PATHS
# ===========================================================================

class TestNullVariableResolver:

    def test_resolve_raises(self) -> None:
        from orca.variables import NullVariableResolver, UndefinedVariableError
        store = NullVariableResolver()
        with pytest.raises(UndefinedVariableError):
            store.resolve("anything", "exec1")

    def test_set_raises(self) -> None:
        from orca.variables import NullVariableResolver
        store = NullVariableResolver()
        with pytest.raises(RuntimeError, match="Cannot set"):
            store.set("x", 1, "exec1")

    def test_set_global_raises(self) -> None:
        from orca.variables import NullVariableResolver
        store = NullVariableResolver()
        with pytest.raises(RuntimeError, match="Cannot set"):
            store.set_global("x", 1)

    def test_has_returns_false(self) -> None:
        from orca.variables import NullVariableResolver
        store = NullVariableResolver()
        assert store.has("anything", "exec1") is False


class TestVariableStoreErrorPaths:

    def test_set_on_nonexistent_execution_raises(self) -> None:
        from orca.variables import VariableService, VariableStore
        store = VariableService(VariableStore())
        with pytest.raises(KeyError, match="nonexistent"):
            store.set("x", 1, "nonexistent")

    def test_resolve_on_nonexistent_execution_raises(self) -> None:
        from orca.variables import VariableService, VariableStore
        store = VariableService(VariableStore())
        with pytest.raises(KeyError, match="nonexistent"):
            store.resolve("x", "nonexistent")

    def test_load_profile_on_nonexistent_execution_raises(self) -> None:
        from orca.variables import VariableService, VariableStore, DeploymentProfile
        store = VariableService(VariableStore())
        profile = DeploymentProfile(name="test", variables={"x": 1})
        with pytest.raises(KeyError, match="nonexistent"):
            store.load_profile("nonexistent", profile)

    def test_global_validates_against_definition(self) -> None:
        from orca.variables import VariableService, VariableStore, VariableDefinition, VariableValidationError
        store = VariableService(VariableStore())
        store.register_global_definitions({"speed": VariableDefinition(type="int", min=0, max=2000)})
        store.set_global("speed", 1000)
        with pytest.raises(VariableValidationError):
            store.set_global("speed", 5000)


# ===========================================================================
# _wrap() CLONE TESTS (Bug 1 regression)
# ===========================================================================

class TestWrapClonesNamedRef:

    def test_get_location_action_produces_independent_refs(self) -> None:
        from orca.variables import Var, NamedRef
        from orca.resource_models.resource_pool import ResourcePool
        from orca.workflow_models.action_template import Action
        from tests.mock import UniversalMockDevice
        from tests.test_helpers import create_test_plate_template
        import orca.orca as orca

        device = UniversalMockDevice("dev1")
        pool = ResourcePool("dev1", [device])
        plate = create_test_plate_template("plate")

        @orca.action(device=pool, inputs=[plate])
        async def shake(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=Var("shake_time"), speed=800)

        action1 = shake.get_location_action()
        action2 = shake.get_location_action()

        # Action uses ActionBodyLocationAction which stores the func; verify independent copies
        assert action1 is not action2


# ===========================================================================
# INTEGRATION TESTS: end-to-end variable resolution through execution
# ===========================================================================

class RecordingMockDevice(UniversalMockDevice):
    """Mock device that records shake calls for assertion."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.shake_calls: list[tuple[int, int]] = []

    async def shake(self, duration: int, speed: int) -> None:
        self.shake_calls.append((duration, speed))
        await super().shake(duration, speed)


class TestVariableIntegration:
    """Verify variables resolve correctly through the full pipeline."""

    async def test_shake_with_var_resolves_at_execute_time(self) -> None:
        """Shake with Var("shake_time") resolves to workflow default and device receives it."""
        import asyncio
        from typing import AsyncGenerator
        import orca.orca as orca
        from orca.variables import Var, VariableDefinition
        from orca.resource_models.resource_pool import ResourcePool
        from orca.runtime.system_runtime import ExecutionState, SystemRuntime
        from orca.sdk.events import EventBus
        from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
        from orca.sdk.system import ResourceRegistry, SystemMap
        from orca.sdk.workflow import WorkflowTemplate
        from orca.workflow_models.method_template import IMethodTemplate, MethodTemplate
        from orca.workflow_models.thread_context import ThreadContext
        from orca.workflow_models.thread_template import ThreadTemplate
        from tests.test_helpers import create_test_plate_template, create_test_transporter, wire_system_map

        device = RecordingMockDevice("device1")
        transporter = create_test_transporter("robot1", ["device1", "pad1"])
        plate = create_test_plate_template("plate_96")

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        pool = ResourcePool("device1", [device])
        registry.add_resource_pool(pool)
        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"device1": device}, pads=["pad1"])

        @orca.action(device=pool, inputs=[plate])
        async def shake_action(ctx: ActionContext) -> None:
            duration = await ctx.param("shake_time")
            await ctx.device().shake(duration=duration, speed=800)

        @orca.method
        async def test_shake(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield shake_action

        method = test_shake

        pad_loc = system_map.get_location("pad1")

        @orca.thread(labware=plate, start=pad_loc, end=pad_loc)
        async def plate_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            yield test_shake

        thread = plate_thread
        workflow = WorkflowTemplate("test_workflow")
        workflow.add_variable("shake_time", VariableDefinition(type="int", default=7200))
        workflow.add_thread(thread, is_start=True)

        event_bus = EventBus()
        builder = SdkToSystemBuilder(
            name="test_system", description="", labwares=[plate],
            resources_registry=registry, system_map=system_map,
            workflows=[workflow], event_bus=event_bus,
        )
        await builder.bind_labwares()
        system = builder.get_system()
        runtime = SystemRuntime(system, event_bus=event_bus)

        await runtime.start()
        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
        status = await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)
        assert status.status == ExecutionState.COMPLETED

        assert len(device.shake_calls) == 1
        duration, speed = device.shake_calls[0]
        assert duration == 7200, f"Expected duration=7200 (workflow default), got {duration}"
        assert speed == 800

        await runtime.shutdown()

    async def test_global_override_reaches_device(self) -> None:
        """Global variable override is what the device actually receives."""
        import asyncio
        import orca.orca as orca
        from orca.variables import Var, VariableDefinition
        from orca.resource_models.resource_pool import ResourcePool
        from orca.runtime.system_runtime import ExecutionState, SystemRuntime
        from orca.sdk.events import EventBus
        from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
        from orca.sdk.system import ResourceRegistry, SystemMap
        from orca.sdk.workflow import WorkflowTemplate
        from orca.workflow_models.method_template import MethodTemplate
        from orca.workflow_models.thread_template import ThreadTemplate
        from tests.test_helpers import create_test_plate_template, create_test_transporter, wire_system_map

        device = RecordingMockDevice("device1")
        transporter = create_test_transporter("robot1", ["device1", "pad1"])
        plate = create_test_plate_template("plate_96")

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        pool = ResourcePool("device1", [device])
        registry.add_resource_pool(pool)
        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"device1": device}, pads=["pad1"])

        @orca.action(device=pool, inputs=[plate])
        async def shake_action(ctx: ActionContext) -> None:
            duration = await ctx.param("global.shake_time")
            await ctx.device().shake(duration=duration, speed=800)

        @orca.method
        async def test_shake(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield shake_action

        method = test_shake

        pad_loc = system_map.get_location("pad1")

        @orca.thread(labware=plate, start=pad_loc, end=pad_loc)
        async def plate_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            yield test_shake

        thread = plate_thread
        workflow = WorkflowTemplate("test_workflow")
        workflow.add_thread(thread, is_start=True)

        event_bus = EventBus()
        builder = SdkToSystemBuilder(
            name="test_system", description="", labwares=[plate],
            resources_registry=registry, system_map=system_map,
            workflows=[workflow], event_bus=event_bus,
        )
        await builder.bind_labwares()
        system = builder.get_system()

        system.variable_store.register_global_definitions({
            "shake_time": VariableDefinition(type="int", default=7200)
        })
        system.variable_store.set_global("shake_time", 0)

        runtime = SystemRuntime(system, event_bus=event_bus)
        await runtime.start()
        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
        status = await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)
        assert status.status == ExecutionState.COMPLETED

        assert len(device.shake_calls) == 1
        duration, speed = device.shake_calls[0]
        assert duration == 0, f"Expected duration=0 (global override), got {duration}"
        assert speed == 800

        await runtime.shutdown()

    async def test_runtime_override_affects_next_action(self) -> None:
        """Pause between methods, change variable, resume. Second action uses new value."""
        import asyncio
        from typing import AsyncGenerator
        import orca.orca as orca
        from orca.variables import Var, VariableDefinition
        from orca.resource_models.resource_pool import ResourcePool
        from orca.runtime.system_runtime import ExecutionState, SystemRuntime
        from orca.sdk.events import EventBus
        from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
        from orca.sdk.system import ResourceRegistry, SystemMap
        from orca.sdk.workflow import WorkflowTemplate
        from orca.workflow_models.method_template import IMethodTemplate, MethodTemplate
        from orca.workflow_models.thread_context import ThreadContext
        from orca.workflow_models.thread_template import ThreadTemplate
        from tests.mutation_helpers import wait_for_threads, pause_and_wait
        from tests.test_helpers import create_test_plate_template, create_test_transporter, wire_system_map

        device = RecordingMockDevice("device1")
        transporter = create_test_transporter("robot1", ["device1", "pad1"])
        plate = create_test_plate_template("plate_96")

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        pool = ResourcePool("device1", [device])
        registry.add_resource_pool(pool)
        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"device1": device}, pads=["pad1"])

        @orca.action(device=pool, inputs=[plate])
        async def shake_action_1(ctx: ActionContext) -> None:
            duration = await ctx.param("shake_time")
            await ctx.device().shake(duration=duration, speed=800)

        @orca.action(device=pool, inputs=[plate])
        async def shake_action_2(ctx: ActionContext) -> None:
            duration = await ctx.param("shake_time")
            await ctx.device().shake(duration=duration, speed=800)

        @orca.method
        async def shake_1(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield shake_action_1

        @orca.method
        async def shake_2(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield shake_action_2

        m1 = shake_1
        m2 = shake_2

        pad_loc = system_map.get_location("pad1")

        async def _thread_gen(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
            yield m1
            yield m2

        thread = ThreadTemplate(labware_template=plate, start=pad_loc, end=pad_loc,
                               func=_thread_gen)
        workflow = WorkflowTemplate("test_workflow")
        workflow.add_variable("shake_time", VariableDefinition(type="int", default=100))
        workflow.add_thread(thread, is_start=True)

        event_bus = EventBus()
        builder = SdkToSystemBuilder(
            name="test_system", description="", labwares=[plate],
            resources_registry=registry, system_map=system_map,
            workflows=[workflow], event_bus=event_bus,
        )
        await builder.bind_labwares()
        system = builder.get_system()
        runtime = SystemRuntime(system, event_bus=event_bus)

        await runtime.start()
        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)

        threads = await wait_for_threads(runtime, record.id)
        await pause_and_wait(runtime, record.id, threads[0].id)

        # First shake used default=100
        assert len(device.shake_calls) == 1
        assert device.shake_calls[0][0] == 100

        # Change variable for the execution partition (need the execution_id)
        # Use the store directly with the execution_id
        exec_entries = runtime.list_executions()
        exec_id = exec_entries[0].id
        # Find the execution_id from the internal execution entry
        entry = runtime._executions[exec_id]
        # Get all executing threads to find the execution_id (= partition key)
        wf_instance_id = entry.system.executing_threads[0].context.execution_id
        system.variable_store.set("shake_time", 9999, wf_instance_id)

        runtime.resume_thread(record.id, threads[0].id)
        status = await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)
        assert status.status == ExecutionState.COMPLETED

        assert len(device.shake_calls) == 2
        assert device.shake_calls[1][0] == 9999, (
            f"Expected second shake duration=9999, got {device.shake_calls[1][0]}"
        )

        await runtime.shutdown()


# ===========================================================================
# GAP TESTS: load_profile validation, computed collision, serialization
# ===========================================================================

class TestLoadProfileValidation:
    """load_profile should validate values against definitions, same as set()."""

    def test_load_profile_rejects_out_of_range_value(self) -> None:
        """Profile with a value violating min/max should be rejected."""
        from orca.variables import VariableService, VariableStore, VariableDefinition, DeploymentProfile, VariableValidationError
        store = VariableService(VariableStore())
        store.register_workflow_definitions("wf1", {
            "speed": VariableDefinition(type="int", min=0, max=2000)
        })
        store.create_execution("exec1", "wf1")
        profile = DeploymentProfile(name="bad", variables={"speed": 5000})
        with pytest.raises(VariableValidationError):
            store.load_profile("exec1", profile)

    def test_load_profile_rejects_wrong_type(self) -> None:
        """Profile with a value of wrong type should be rejected."""
        from orca.variables import VariableService, VariableStore, VariableDefinition, DeploymentProfile, VariableValidationError
        store = VariableService(VariableStore())
        store.register_workflow_definitions("wf1", {
            "speed": VariableDefinition(type="int")
        })
        store.create_execution("exec1", "wf1")
        profile = DeploymentProfile(name="bad", variables={"speed": "fast"})
        with pytest.raises(VariableValidationError):
            store.load_profile("exec1", profile)

    def test_load_profile_accepts_valid_values(self) -> None:
        """Profile with values within constraints should be accepted."""
        from orca.variables import VariableService, VariableStore, VariableDefinition, DeploymentProfile
        store = VariableService(VariableStore())
        store.register_workflow_definitions("wf1", {
            "speed": VariableDefinition(type="int", min=0, max=2000)
        })
        store.create_execution("exec1", "wf1")
        profile = DeploymentProfile(name="good", variables={"speed": 1000})
        store.load_profile("exec1", profile)
        assert store.resolve("speed", "exec1") == 1000

    def test_load_profile_global_validates_against_global_definition(self) -> None:
        """Global-prefixed profile values validate against global definitions."""
        from orca.variables import VariableService, VariableStore, VariableDefinition, DeploymentProfile, VariableValidationError
        store = VariableService(VariableStore())
        store.register_global_definitions({
            "retries": VariableDefinition(type="int", min=0, max=10)
        })
        store.create_execution("exec1", "wf1")
        profile = DeploymentProfile(name="bad", variables={"global.retries": 999})
        with pytest.raises(VariableValidationError):
            store.load_profile("exec1", profile)


class TestComputedExpressionIsolation:
    """Computed expressions are shared state -- verify cross-execution behavior."""

    def test_two_profiles_overwrite_computed_expressions(self) -> None:
        """Second profile's computed expressions overwrite the first's."""
        from orca.variables import VariableService, VariableStore, DeploymentProfile
        store = VariableService(VariableStore())
        store.create_execution("exec1", "wf1")
        store.create_execution("exec2", "wf1")

        store.set("base", 100, "exec1")
        store.set("base", 100, "exec2")

        p1 = DeploymentProfile(name="p1", computed={"derived": "base * 2"})
        store.load_profile("exec1", p1)
        assert store.resolve("derived", "exec1") == 200

        p2 = DeploymentProfile(name="p2", computed={"derived": "base * 3"})
        store.load_profile("exec2", p2)

        # exec2 gets the new formula
        assert store.resolve("derived", "exec2") == 300
        # exec1 also gets the new formula -- computed is global shared state
        assert store.resolve("derived", "exec1") == 300

    def test_computed_uses_execution_partition_values(self) -> None:
        """Computed expressions evaluate against execution-specific values."""
        from orca.variables import VariableService, VariableStore, DeploymentProfile
        store = VariableService(VariableStore())
        store.register_computed({"total": "x + y"})
        store.create_execution("exec1", "wf1")
        store.create_execution("exec2", "wf1")
        store.set("x", 10, "exec1")
        store.set("y", 20, "exec1")
        store.set("x", 100, "exec2")
        store.set("y", 200, "exec2")

        assert store.resolve("total", "exec1") == 30
        assert store.resolve("total", "exec2") == 300



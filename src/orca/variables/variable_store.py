"""Two-scope, execution-partitioned variable system.

Two scopes for variable schemas:
- Global: defined at system level, referenced as "global.var_name"
- Workflow: defined per WorkflowTemplate, referenced as plain "var_name"

Values live in three partitions. The submission partition is the narrowest and
wins: a submission is one request inside an execution, so its values are more
specific than values set on the execution as a whole. Values supplied on a
submit land there, which is why an operator editing a running variable has to
reach that layer rather than the execution partition.

Resolution order for global.var_name:
  submission partition > execution partition > global values > computed >
  global definition default

Resolution order for plain var_name (workflow-scoped):
  submission partition > execution partition > computed >
  workflow definition default
  (does NOT fall through to global values -- scopes are strict)

Layering (genuine Service-over-Store):
- ``IVariableStore`` is the dumb-store PRIMITIVE interface: the per-layer
  get/set/pop/has/iter accessors plus definition/computed registration and
  execution lifecycle. It is the persistence-swap seam -- a future persistent
  store implements exactly this, and ``VariableService`` runs over it unchanged.
- ``VariableStore`` is the in-memory implementation: the seven containers behind
  those primitives. No lock, no resolution, no validation.
- ``VariableService`` takes an ``IVariableStore``, owns the single
  ``threading.Lock``, and adds all resolution and validation as the full
  consumer API. Production always injects ``VariableService(VariableStore())``.
- ``IVariableResolver`` is the narrow read interface authored-code contexts
  depend on (just ``resolve``). ``VariableService`` and ``NullVariableResolver``
  both satisfy it.
"""

import re
import threading
from typing import Iterator, Protocol, Tuple

from orca.variables.deployment_profile import DeploymentProfile
from orca.variables.errors import OptionValue, UndefinedVariableError
from orca.variables.expression import evaluate_expression
from orca.variables.resolution import (
    SubmissionOverride,
    VariableBinding,
    VariableResolution,
    VariableSource,
)
from orca.variables.variable_definition import VariableDefinition

GLOBAL_PREFIX = "global."
_RESERVED_NAME_PATTERN = re.compile(r"^global$|^global\.", re.IGNORECASE)


def _validate_workflow_variable_name(name: str) -> None:
    """Reject names that collide with the global scope prefix."""
    if _RESERVED_NAME_PATTERN.match(name):
        raise ValueError(
            f"Workflow variable name '{name}' is reserved. "
            f"Names starting with 'global.' are global-scoped. "
            f"The bare name 'global' is also reserved."
        )


class IVariableStore(Protocol):
    """Dumb-store primitive interface = the persistence-swap seam.

    Declares exactly the accessors the in-memory ``VariableStore`` exposes:
    no resolution, no validation, no lock. ``VariableService`` runs over any
    implementation of this surface.
    """

    def register_global_definitions(
        self, definitions: dict[str, VariableDefinition],
    ) -> None: ...
    def get_global_definition(self, name: str) -> VariableDefinition | None: ...
    def iter_global_definitions(self) -> Iterator[Tuple[str, VariableDefinition]]: ...
    def register_workflow_definitions(
        self, workflow_name: str, definitions: dict[str, VariableDefinition],
    ) -> None: ...
    def get_workflow_definition(
        self, workflow_name: str, name: str,
    ) -> VariableDefinition | None: ...
    def iter_workflow_definitions(
        self, workflow_name: str,
    ) -> Iterator[Tuple[str, VariableDefinition]]: ...
    def register_computed(self, computed: dict[str, str]) -> None: ...
    def get_computed(self, name: str) -> str | None: ...
    def has_computed(self, name: str) -> bool: ...
    def iter_computed_names(self) -> Iterator[str]: ...
    def create_execution(self, execution_id: str, workflow_template_name: str) -> None: ...
    def remove_execution(self, execution_id: str) -> None: ...
    def has_execution(self, execution_id: str) -> bool: ...
    def workflow_for(self, execution_id: str) -> str | None: ...
    def get_global_value(self, name: str) -> OptionValue: ...
    def set_global_value(self, name: str, value: OptionValue) -> None: ...
    def pop_global_value(self, name: str) -> None: ...
    def has_global_value(self, name: str) -> bool: ...
    def iter_global_values(self) -> Iterator[Tuple[str, OptionValue]]: ...
    def get_execution_value(self, execution_id: str, name: str) -> OptionValue: ...
    def set_execution_value(
        self, execution_id: str, name: str, value: OptionValue,
    ) -> None: ...
    def pop_execution_value(self, execution_id: str, name: str) -> None: ...
    def has_execution_value(self, execution_id: str, name: str) -> bool: ...
    def iter_execution_values(
        self, execution_id: str,
    ) -> Iterator[Tuple[str, OptionValue]]: ...
    def get_submission_value(
        self, execution_id: str, submission_id: str, name: str,
    ) -> OptionValue: ...
    def set_submission_value(
        self, execution_id: str, submission_id: str, name: str, value: OptionValue,
    ) -> None: ...
    def pop_submission_value(
        self, execution_id: str, submission_id: str, name: str,
    ) -> None: ...
    def has_submission_value(
        self, execution_id: str, submission_id: str, name: str,
    ) -> bool: ...
    def iter_submission_ids(self, execution_id: str) -> Iterator[str]: ...
    def iter_submission_partitions(
        self, execution_id: str,
    ) -> Iterator[dict[str, OptionValue]]: ...


class IVariableResolver(Protocol):
    """Narrow read interface authored-code contexts depend on."""

    def resolve(
        self, name: str, execution_id: str, submission_id: str | None = None,
    ) -> OptionValue: ...


class NullVariableResolver(IVariableResolver):
    """Null-object resolver used as default when no variables are configured.

    Satisfies ``IVariableResolver`` (the contexts only ever call ``resolve``).
    The write guards reject mutation loudly so a misrouted set is a crash, not
    a silent no-op; the read methods return the empty-variable answer.
    """

    def resolve(
        self, name: str, execution_id: str, submission_id: str | None = None,
    ) -> OptionValue:
        raise UndefinedVariableError(name)

    def resolve_global(self, name: str) -> OptionValue:
        raise UndefinedVariableError(name)

    def has(
        self, name: str, execution_id: str, submission_id: str | None = None,
    ) -> bool:
        return False

    def set(self, name: str, value: OptionValue, execution_id: str) -> None:
        raise RuntimeError("Cannot set variables on NullVariableResolver")

    def set_global(self, name: str, value: OptionValue) -> None:
        raise RuntimeError("Cannot set variables on NullVariableResolver")


class VariableStore:
    """Dumb in-memory state holder for the variable system.

    Owns the seven containers and exposes primitive accessors only: definition
    and computed registration/lookup, execution lifecycle, and per-layer value
    operations for the global, execution, and submission layers. No lock, no
    resolution, no validation -- ``VariableService`` composes these primitives
    under its lock to implement the resolver contract.
    """

    def __init__(self) -> None:
        self._global_definitions: dict[str, VariableDefinition] = {}
        self._workflow_definitions: dict[str, dict[str, VariableDefinition]] = {}
        self._execution_workflow_map: dict[str, str] = {}
        self._global: dict[str, OptionValue] = {}
        self._execution_stores: dict[str, dict[str, OptionValue]] = {}
        # (execution_id, submission_id) -> name->value. Overrides the execution
        # partition so grouped submissions sharing one Execution stay isolated.
        self._submission_stores: dict[tuple[str, str], dict[str, OptionValue]] = {}
        self._computed: dict[str, str] = {}

    # --- Global definitions ---

    def register_global_definitions(
        self, definitions: dict[str, VariableDefinition],
    ) -> None:
        self._global_definitions.update(definitions)

    def get_global_definition(self, name: str) -> VariableDefinition | None:
        return self._global_definitions.get(name)

    def iter_global_definitions(self) -> Iterator[Tuple[str, VariableDefinition]]:
        return iter(self._global_definitions.items())

    # --- Workflow definitions ---

    def register_workflow_definitions(
        self, workflow_name: str, definitions: dict[str, VariableDefinition],
    ) -> None:
        for name in definitions:
            _validate_workflow_variable_name(name)
        # Authoritative per workflow: a re-add (REPLACE) gives exactly the
        # current definitions, no stale keys from a version that dropped a var.
        self._workflow_definitions[workflow_name] = dict(definitions)

    def get_workflow_definition(
        self, workflow_name: str, name: str,
    ) -> VariableDefinition | None:
        defs = self._workflow_definitions.get(workflow_name)
        if defs is None:
            return None
        return defs.get(name)

    def iter_workflow_definitions(
        self, workflow_name: str,
    ) -> Iterator[Tuple[str, VariableDefinition]]:
        return iter(self._workflow_definitions.get(workflow_name, {}).items())

    # --- Computed ---

    def register_computed(self, computed: dict[str, str]) -> None:
        self._computed.update(computed)

    def get_computed(self, name: str) -> str | None:
        return self._computed.get(name)

    def has_computed(self, name: str) -> bool:
        return name in self._computed

    def iter_computed_names(self) -> Iterator[str]:
        return iter(self._computed)

    # --- Execution lifecycle ---

    def create_execution(self, execution_id: str, workflow_template_name: str) -> None:
        """Create an empty execution partition. Idempotent: a repeat call for the
        same execution_id is a no-op so daemon-level profile loading can register
        eagerly before _run_workflow reaches its own registration."""
        if execution_id in self._execution_stores:
            return
        self._execution_stores[execution_id] = {}
        self._execution_workflow_map[execution_id] = workflow_template_name

    def remove_execution(self, execution_id: str) -> None:
        """Remove an execution partition, its workflow mapping, and any submission
        partitions associated with it."""
        self._execution_stores.pop(execution_id, None)
        self._execution_workflow_map.pop(execution_id, None)
        keys_to_drop = [k for k in self._submission_stores if k[0] == execution_id]
        for key in keys_to_drop:
            self._submission_stores.pop(key, None)

    def has_execution(self, execution_id: str) -> bool:
        return execution_id in self._execution_stores

    def workflow_for(self, execution_id: str) -> str | None:
        return self._execution_workflow_map.get(execution_id)

    # --- Global value layer ---
    # Value getters raise KeyError on absence (None is never a valid OptionValue).

    def get_global_value(self, name: str) -> OptionValue:
        return self._global[name]

    def set_global_value(self, name: str, value: OptionValue) -> None:
        self._global[name] = value

    def pop_global_value(self, name: str) -> None:
        self._global.pop(name, None)

    def has_global_value(self, name: str) -> bool:
        return name in self._global

    def iter_global_values(self) -> Iterator[Tuple[str, OptionValue]]:
        return iter(self._global.items())

    # --- Execution value layer ---

    def get_execution_value(self, execution_id: str, name: str) -> OptionValue:
        return self._execution_stores[execution_id][name]

    def set_execution_value(
        self, execution_id: str, name: str, value: OptionValue,
    ) -> None:
        self._execution_stores[execution_id][name] = value

    def pop_execution_value(self, execution_id: str, name: str) -> None:
        self._execution_stores[execution_id].pop(name, None)

    def has_execution_value(self, execution_id: str, name: str) -> bool:
        partition = self._execution_stores.get(execution_id)
        if partition is None:
            return False
        return name in partition

    def iter_execution_values(
        self, execution_id: str,
    ) -> Iterator[Tuple[str, OptionValue]]:
        return iter(self._execution_stores.get(execution_id, {}).items())

    # --- Submission value layer ---

    def get_submission_value(
        self, execution_id: str, submission_id: str, name: str,
    ) -> OptionValue:
        return self._submission_stores[(execution_id, submission_id)][name]

    def set_submission_value(
        self, execution_id: str, submission_id: str, name: str, value: OptionValue,
    ) -> None:
        key = (execution_id, submission_id)
        if key not in self._submission_stores:
            self._submission_stores[key] = {}
        self._submission_stores[key][name] = value

    def pop_submission_value(
        self, execution_id: str, submission_id: str, name: str,
    ) -> None:
        partition = self._submission_stores.get((execution_id, submission_id))
        if partition is not None:
            partition.pop(name, None)

    def has_submission_value(
        self, execution_id: str, submission_id: str, name: str,
    ) -> bool:
        partition = self._submission_stores.get((execution_id, submission_id))
        if partition is None:
            return False
        return name in partition

    def iter_submission_ids(self, execution_id: str) -> Iterator[str]:
        """Every submission that holds a partition under this execution.

        Materialised so a caller can write to the store while walking it.
        """
        return iter([
            sub_id for exec_id, sub_id in self._submission_stores
            if exec_id == execution_id
        ])

    def iter_submission_partitions(
        self, execution_id: str,
    ) -> Iterator[dict[str, OptionValue]]:
        for (exec_id, _sub_id), partition in self._submission_stores.items():
            if exec_id == execution_id:
                yield partition


class VariableService(IVariableResolver):
    """Orchestration over a dumb ``IVariableStore``.

    Owns the single ``threading.Lock`` (sync, because resolution is called from
    executor threads off the event loop via ``ctx.param``), all resolution and
    validation, and exposes the full consumer API by composing the store's
    primitive accessors. Depends on the ``IVariableStore`` interface so a
    persistent store can swap in without reimplementing resolution.
    """

    def __init__(self, store: IVariableStore) -> None:
        self._store = store
        self._lock = threading.Lock()

    # --- Lifecycle / registration (delegate under the lock) ---

    def create_execution(self, execution_id: str, workflow_template_name: str) -> None:
        with self._lock:
            self._store.create_execution(execution_id, workflow_template_name)

    def remove_execution(self, execution_id: str) -> None:
        with self._lock:
            self._store.remove_execution(execution_id)

    def register_global_definitions(
        self, definitions: dict[str, VariableDefinition],
    ) -> None:
        with self._lock:
            self._store.register_global_definitions(definitions)

    def register_workflow_definitions(
        self, workflow_name: str, definitions: dict[str, VariableDefinition],
    ) -> None:
        with self._lock:
            self._store.register_workflow_definitions(workflow_name, definitions)

    def register_computed(self, computed: dict[str, str]) -> None:
        with self._lock:
            self._store.register_computed(computed)

    # --- Resolution ---

    def resolve(
        self, name: str, execution_id: str, submission_id: str | None = None,
    ) -> OptionValue:
        with self._lock:
            return self._resolve_locked(name, execution_id, submission_id)

    def explain(self, name: str, execution_id: str) -> VariableResolution:
        """What every thread of ``execution_id`` resolves for ``name``.

        The submission partitions are listed separately because a batched or
        multi-group execution can hold a different value per submission, and a
        single answer would be wrong for all but one of them.
        """
        with self._lock:
            if not self._store.has_execution(execution_id):
                raise KeyError(f"Execution '{execution_id}' not found in variable store")
            try:
                fallthrough: VariableBinding | None = self._resolve_binding_locked(
                    name, execution_id,
                )
            except UndefinedVariableError:
                fallthrough = None
            overrides = tuple(
                SubmissionOverride(
                    submission_id=submission_id,
                    value=self._store.get_submission_value(
                        execution_id, submission_id, name,
                    ),
                )
                for submission_id in self._store.iter_submission_ids(execution_id)
                if self._store.has_submission_value(execution_id, submission_id, name)
            )
            return VariableResolution(
                name=name,
                execution_id=execution_id,
                value=None if fallthrough is None else fallthrough.value,
                source=None if fallthrough is None else fallthrough.source,
                overrides=overrides,
            )

    def resolve_global(self, name: str) -> OptionValue:
        """Resolve a global variable without an execution context.

        Walks the global value layer, then the global definition default.
        Computed variables are NOT evaluated (their expressions may reference
        execution-scoped state); ``resolve(name, execution_id)`` is the entry
        point for computed lookups. Accepts ``name`` bare or ``global.``-prefixed.
        """
        bare = name[len(GLOBAL_PREFIX):] if name.startswith(GLOBAL_PREFIX) else name
        with self._lock:
            if self._store.has_global_value(bare):
                return self._store.get_global_value(bare)
            defn = self._store.get_global_definition(bare)
            if defn is not None and defn.default is not None:
                return defn.default
            raise UndefinedVariableError(f"{GLOBAL_PREFIX}{bare}")

    def _resolve_locked(
        self, name: str, execution_id: str, submission_id: str | None = None,
    ) -> OptionValue:
        return self._resolve_binding_locked(name, execution_id, submission_id).value

    def _resolve_binding_locked(
        self, name: str, execution_id: str, submission_id: str | None = None,
    ) -> VariableBinding:
        """The one walk of the precedence order; value reads drop the source.

        Keeping a second copy for the source-reporting reads is how the two
        answers drift apart, so both callers come through here.
        """
        if not self._store.has_execution(execution_id):
            raise KeyError(f"Execution '{execution_id}' not found in variable store")

        if submission_id is not None:
            if self._store.has_submission_value(execution_id, submission_id, name):
                return VariableBinding(
                    self._store.get_submission_value(execution_id, submission_id, name),
                    VariableSource.SUBMISSION,
                )

        if self._store.has_execution_value(execution_id, name):
            return VariableBinding(
                self._store.get_execution_value(execution_id, name),
                VariableSource.EXECUTION,
            )

        if name.startswith(GLOBAL_PREFIX):
            return self._resolve_global_locked(name, execution_id)
        return self._resolve_workflow_locked(name, execution_id)

    def _resolve_global_locked(self, name: str, execution_id: str) -> VariableBinding:
        bare_name = name[len(GLOBAL_PREFIX):]

        if self._store.has_global_value(bare_name):
            return VariableBinding(
                self._store.get_global_value(bare_name), VariableSource.GLOBAL,
            )

        if self._store.has_computed(bare_name):
            return VariableBinding(
                self._evaluate_computed(bare_name, execution_id),
                VariableSource.COMPUTED,
            )

        defn = self._store.get_global_definition(bare_name)
        if defn is not None and defn.default is not None:
            return VariableBinding(defn.default, VariableSource.GLOBAL_DEFAULT)

        raise UndefinedVariableError(name)

    def _resolve_workflow_locked(self, name: str, execution_id: str) -> VariableBinding:
        if self._store.has_computed(name):
            return VariableBinding(
                self._evaluate_computed(name, execution_id), VariableSource.COMPUTED,
            )

        workflow_name = self._store.workflow_for(execution_id)
        if workflow_name is not None:
            defn = self._store.get_workflow_definition(workflow_name, name)
            if defn is not None and defn.default is not None:
                return VariableBinding(defn.default, VariableSource.WORKFLOW_DEFAULT)

        raise UndefinedVariableError(name)

    # --- Mutation (with validation) ---

    def set(self, name: str, value: OptionValue, execution_id: str) -> None:
        with self._lock:
            if not self._store.has_execution(execution_id):
                raise KeyError(f"Execution '{execution_id}' not found in variable store")
            defn = self._find_definition(name, execution_id)
            if defn is not None:
                defn.validate_value(name, value)
            self._store.set_execution_value(execution_id, name, value)

    def set_submission(
        self, name: str, value: OptionValue, execution_id: str, submission_id: str,
    ) -> None:
        with self._lock:
            if not self._store.has_execution(execution_id):
                raise KeyError(f"Execution '{execution_id}' not found in variable store")
            defn = self._find_definition(name, execution_id)
            if defn is not None:
                defn.validate_value(name, value)
            self._store.set_submission_value(execution_id, submission_id, name, value)

    def unset(self, name: str, execution_id: str) -> None:
        with self._lock:
            if not self._store.has_execution(execution_id):
                raise KeyError(f"Execution '{execution_id}' not found in variable store")
            self._store.pop_execution_value(execution_id, name)

    def unset_submission(
        self, name: str, execution_id: str, submission_id: str,
    ) -> None:
        """Drop one submission's override so it falls through to the layers below."""
        with self._lock:
            if not self._store.has_execution(execution_id):
                raise KeyError(f"Execution '{execution_id}' not found in variable store")
            self._store.pop_submission_value(execution_id, submission_id, name)

    def set_global(self, name: str, value: OptionValue) -> None:
        with self._lock:
            defn = self._store.get_global_definition(name)
            if defn is not None:
                defn.validate_value(name, value)
            self._store.set_global_value(name, value)

    def unset_global(self, name: str) -> None:
        with self._lock:
            self._store.pop_global_value(name)

    def load_profile(self, execution_id: str, profile: DeploymentProfile) -> None:
        """Bulk-set values from a deployment profile.

        Values with 'global.' prefix write to both the execution partition (so
        this execution sees them) and the global layer (so other executions do).
        Plain names go only to the execution partition.
        """
        with self._lock:
            if not self._store.has_execution(execution_id):
                raise KeyError(f"Execution '{execution_id}' not found in variable store")
            for key, value in profile.variables.items():
                defn = self._find_definition(key, execution_id)
                if defn is not None:
                    defn.validate_value(key, value)
                if key.startswith(GLOBAL_PREFIX):
                    bare = key[len(GLOBAL_PREFIX):]
                    self._store.set_global_value(bare, value)
                self._store.set_execution_value(execution_id, key, value)
            if profile.computed:
                self._store.register_computed(profile.computed)

    # --- Queries ---

    def has(
        self, name: str, execution_id: str, submission_id: str | None = None,
    ) -> bool:
        with self._lock:
            try:
                self._resolve_locked(name, execution_id, submission_id)
                return True
            except (UndefinedVariableError, KeyError):
                return False

    def has_global_value(self, name: str) -> bool:
        bare = name[len(GLOBAL_PREFIX):] if name.startswith(GLOBAL_PREFIX) else name
        with self._lock:
            return self._store.has_global_value(bare)

    def has_execution_value(self, name: str, execution_id: str) -> bool:
        with self._lock:
            return self._store.has_execution_value(execution_id, name)

    def has_submission_value(
        self, name: str, execution_id: str, submission_id: str,
    ) -> bool:
        """Partition membership, not resolvability: True only when this
        submission's own partition holds the name."""
        with self._lock:
            return self._store.has_submission_value(execution_id, submission_id, name)

    def get_all(self, execution_id: str) -> dict[str, OptionValue]:
        """Merged view: workflow defaults -> global defaults -> global values ->
        execution partition -> submission partitions. Submission overrides surface
        to the operator view; per-thread resolution still picks the right
        submission via ``resolve(name, execution_id, submission_id)``."""
        with self._lock:
            result: dict[str, OptionValue] = {}
            workflow_name = self._store.workflow_for(execution_id)

            if workflow_name is not None:
                for name, defn in self._store.iter_workflow_definitions(workflow_name):
                    if defn.default is not None:
                        result[name] = defn.default

            for name, defn in self._store.iter_global_definitions():
                if defn.default is not None:
                    result[f"{GLOBAL_PREFIX}{name}"] = defn.default

            for name, val in self._store.iter_global_values():
                result[f"{GLOBAL_PREFIX}{name}"] = val

            for name, val in self._store.iter_execution_values(execution_id):
                result[name] = val

            for partition in self._store.iter_submission_partitions(execution_id):
                result.update(partition)

            return result

    def get_all_with_source(
        self, execution_id: str,
    ) -> dict[str, tuple[OptionValue, VariableSource]]:
        """Like :meth:`get_all`, but each value is tagged with the layer it came
        from. A name two submissions disagree on is tagged
        ``SUBMISSION_DIVERGED``: one slot cannot hold both, so the tag says so
        rather than presenting one of them as the answer. Use :meth:`explain`
        for the per-submission values."""
        with self._lock:
            result: dict[str, tuple[OptionValue, VariableSource]] = {}
            workflow_name = self._store.workflow_for(execution_id)

            if workflow_name is not None:
                for name, defn in self._store.iter_workflow_definitions(workflow_name):
                    if defn.default is not None:
                        result[name] = (defn.default, VariableSource.WORKFLOW_DEFAULT)

            for name, defn in self._store.iter_global_definitions():
                if defn.default is not None:
                    result[f"{GLOBAL_PREFIX}{name}"] = (
                        defn.default, VariableSource.GLOBAL_DEFAULT,
                    )

            for name, val in self._store.iter_global_values():
                result[f"{GLOBAL_PREFIX}{name}"] = (val, VariableSource.GLOBAL)

            for name, val in self._store.iter_execution_values(execution_id):
                result[name] = (val, VariableSource.EXECUTION)

            for name, values in self._submission_values_by_name(execution_id).items():
                diverged = len(set(values)) > 1
                result[name] = (
                    values[-1],
                    VariableSource.SUBMISSION_DIVERGED if diverged
                    else VariableSource.SUBMISSION,
                )

            for name in self._store.iter_computed_names():
                if name not in result:
                    result[name] = (
                        self._evaluate_computed(name, execution_id),
                        VariableSource.COMPUTED,
                    )

            return result

    # --- Internal helpers (assume the lock is held) ---

    def _submission_values_by_name(
        self, execution_id: str,
    ) -> dict[str, list[OptionValue]]:
        """Every submission partition's values for this execution, keyed by name."""
        by_name: dict[str, list[OptionValue]] = {}
        for partition in self._store.iter_submission_partitions(execution_id):
            for name, val in partition.items():
                by_name.setdefault(name, []).append(val)
        return by_name

    def _find_definition(self, name: str, execution_id: str) -> VariableDefinition | None:
        if name.startswith(GLOBAL_PREFIX):
            bare = name[len(GLOBAL_PREFIX):]
            return self._store.get_global_definition(bare)
        workflow_name = self._store.workflow_for(execution_id)
        if workflow_name is not None:
            return self._store.get_workflow_definition(workflow_name, name)
        return None

    def _evaluate_computed(self, name: str, execution_id: str) -> OptionValue:
        expr = self._store.get_computed(name)
        if expr is None:
            raise UndefinedVariableError(name)
        context: dict[str, OptionValue] = {}
        workflow_name = self._store.workflow_for(execution_id)
        if workflow_name is not None:
            for defn_name, defn in self._store.iter_workflow_definitions(workflow_name):
                if defn.default is not None:
                    context[defn_name] = defn.default
        for defn_name, defn in self._store.iter_global_definitions():
            if defn.default is not None:
                context[defn_name] = defn.default
        for gname, gval in self._store.iter_global_values():
            context[gname] = gval
        for ename, eval_ in self._store.iter_execution_values(execution_id):
            context[ename] = eval_
        return evaluate_expression(expr, context)

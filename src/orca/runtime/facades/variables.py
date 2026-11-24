"""VariableFacade: the external UI surface over a `VariableService`.

Reads pass through untouched. Writes are marked `@dangerous` so CLI/REST
all get uniform confirmation prompts through the danger registry.

**Module boundary note.** This file sits at the UI-boundary layer. Default
argument values on dangerous methods (`confirm: bool = False`) are the
boundary convention documented in the runtime_interface module header; they
let external callers opt into strict behavior while keeping common cases
readable. Engine-internal code never calls facades -- it talks directly to
the `VariableService` and skips the gate.
"""

from orca.runtime.danger import DangerLevel, dangerous
from orca.runtime.facade_interfaces import IVariableFacade
from orca.variables.errors import OptionValue
from orca.variables.resolution import VariableResolution
from orca.runtime.global_variable_service import IGlobalVariableService
from orca.variables.variable_store import IVariableService


class VariableFacade(IVariableFacade):
    """Concrete VariableFacade implementation."""

    def __init__(self, store: IVariableService, globals_file: IGlobalVariableService) -> None:
        self._store = store
        self._globals_file = globals_file

    def get(self, name: str, execution_id: str) -> OptionValue:
        return self._store.resolve(name, execution_id)

    def explain(self, name: str, execution_id: str) -> VariableResolution:
        return self._store.explain(name, execution_id)

    def get_global(self, name: str) -> OptionValue:
        return self._store.resolve_global(name)

    def get_all(self, execution_id: str) -> dict[str, OptionValue]:
        return self._store.get_all(execution_id)

    def has(self, name: str, execution_id: str) -> bool:
        return self._store.has(name, execution_id)

    def has_global_value(self, name: str) -> bool:
        return self._store.has_global_value(name)

    def has_execution_value(self, name: str, execution_id: str) -> bool:
        return self._store.has_execution_value(name, execution_id)

    def has_submission_value(
        self, name: str, execution_id: str, submission_id: str,
    ) -> bool:
        return self._store.has_submission_value(name, execution_id, submission_id)

    @dangerous(
        name="variables.set",
        level=DangerLevel.OPERATOR,
        message="Set variable '{name}' in execution '{execution_id}'. "
                "In-flight actions will not see the new value; future actions will.",
    )
    def set(
        self, name: str, value: OptionValue, execution_id: str,
        *, reason: str | None = None,
    ) -> None:
        del reason  # consumed by @dangerous audit
        self._store.set(name, value, execution_id)

    @dangerous(
        name="variables.set_global",
        level=DangerLevel.CRITICAL,
        message="Set global variable '{name}'. Affects ALL executions that do not have "
                "a local override. In-flight actions will not see the new value.",
    )
    async def set_global(
        self, name: str, value: OptionValue,
        *, reason: str | None = None,
    ) -> None:
        del reason  # consumed by @dangerous audit
        await self._globals_file.save_value(name, value)
        self._store.set_global(name, value)

    @dangerous(
        name="variables.set_submission",
        level=DangerLevel.OPERATOR,
        message="Set variable '{name}' for submission '{submission_id}' in execution "
                "'{execution_id}'. This layer outranks the per-execution value, so it "
                "is what the submission's threads resolve. In-flight actions will not "
                "see the new value; future actions will.",
    )
    def set_submission(
        self, name: str, value: OptionValue, execution_id: str, submission_id: str,
        *, reason: str | None = None,
    ) -> None:
        del reason  # consumed by @dangerous audit
        self._store.set_submission(name, value, execution_id, submission_id)

    @dangerous(
        name="variables.unset",
        level=DangerLevel.OPERATOR,
        message="Remove variable '{name}' override in execution '{execution_id}'. "
                "Subsequent resolves fall through to global / computed / default.",
    )
    def unset(
        self, name: str, execution_id: str,
        *, reason: str | None = None,
    ) -> None:
        del reason  # consumed by @dangerous audit
        self._store.unset(name, execution_id)

    @dangerous(
        name="variables.unset_submission",
        level=DangerLevel.OPERATOR,
        message="Remove variable '{name}' override for submission '{submission_id}' in "
                "execution '{execution_id}'. Its threads fall through to the "
                "per-execution value, then global / computed / default.",
    )
    def unset_submission(
        self, name: str, execution_id: str, submission_id: str,
        *, reason: str | None = None,
    ) -> None:
        del reason  # consumed by @dangerous audit
        self._store.unset_submission(name, execution_id, submission_id)

    @dangerous(
        name="variables.unset_global",
        level=DangerLevel.CRITICAL,
        message="Remove global variable '{name}'. ALL executions that read this "
                "variable fall through to computed / default values. In-flight "
                "actions will not see the removal.",
    )
    async def unset_global(self, name: str, *, reason: str | None = None) -> None:
        del reason  # consumed by @dangerous audit
        await self._globals_file.clear_value(name)
        self._store.unset_global(name)

"""Which submitted variables a run refuses.

One pure decision. ``submit`` gathers the facts and raises on the answer, so a bad input
is refused before any device is touched. The rows are pinned in
``tests/test_submission_inputs.py``.
"""

from dataclasses import dataclass, field
from typing import Mapping

from orca.variables.errors import OptionValue, VariableValidationError
from orca.variables.variable_definition import VariableDefinition
from orca.variables.variable_store import GLOBAL_PREFIX


@dataclass(frozen=True)
class SubmittedInputs:
    workflow_name: str
    inline: Mapping[str, OptionValue] = field(default_factory=dict)
    profile: Mapping[str, OptionValue] = field(default_factory=dict)
    workflow_definitions: Mapping[str, VariableDefinition] = field(default_factory=dict)
    global_definitions: Mapping[str, VariableDefinition] = field(default_factory=dict)
    computed: frozenset[str] = frozenset()
    global_values: frozenset[str] = frozenset()
    set_on_joined_execution: frozenset[str] = frozenset()
    every_variable_is_read: bool = True
    """False for a standalone method: it carries its parent's definitions and may read few of them."""


def refusals_for(inputs: SubmittedInputs) -> list[str]:
    """Every reason these inputs cannot run, so one correction fixes them all."""
    return [
        *_undefined_names(inputs),
        *_bad_values(inputs),
        *_missing_values(inputs),
    ]


def _definition_of(name: str, inputs: SubmittedInputs) -> VariableDefinition | None:
    if name.startswith(GLOBAL_PREFIX):
        return inputs.global_definitions.get(name[len(GLOBAL_PREFIX):])
    return inputs.workflow_definitions.get(name)


def _is_known(name: str, inputs: SubmittedInputs) -> bool:
    bare = name[len(GLOBAL_PREFIX):] if name.startswith(GLOBAL_PREFIX) else name
    return (
        _definition_of(name, inputs) is not None
        or bare in inputs.computed
        or (name.startswith(GLOBAL_PREFIX) and bare in inputs.global_values)
    )


def _undefined_names(inputs: SubmittedInputs) -> list[str]:
    defined = ", ".join(sorted(inputs.workflow_definitions)) or "none"
    return [
        f"{name} is not a variable of workflow {inputs.workflow_name}. Defined: {defined}."
        for name in inputs.inline
        if not _is_known(name, inputs)
    ]


def _bad_values(inputs: SubmittedInputs) -> list[str]:
    refusals: list[str] = []
    for values in (inputs.profile, inputs.inline):
        for name, value in values.items():
            definition = _definition_of(name, inputs)
            if definition is None:
                continue
            try:
                definition.validate_value(name, value)
            except VariableValidationError as exc:
                refusals.append(str(exc))
    return refusals


def _missing_values(inputs: SubmittedInputs) -> list[str]:
    if not inputs.every_variable_is_read:
        return []
    supplied = {*inputs.inline, *inputs.profile, *inputs.computed, *inputs.set_on_joined_execution}
    return [
        f"{name} has no value and no default. Pass it in variables."
        for name, definition in inputs.workflow_definitions.items()
        if definition.default is None and name not in supplied
    ]

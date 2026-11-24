"""Variable definitions with validation constraints."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator
from typing_extensions import Self

from orca.variables.errors import OptionValue, VariableValidationError

_TYPE_MAP: dict[str, type] = {
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
}


class VariableDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["int", "float", "str", "bool"] = "str"
    default: OptionValue | None = None
    min: float | None = None
    max: float | None = None
    allowed_values: list[OptionValue] | None = None
    description: str = ""
    unit: str = ""

    def validate_value(self, name: str, value: OptionValue) -> None:
        expected_type = _TYPE_MAP.get(self.type)
        # Bool is a subclass of int in Python, so reject bool when int/float expected
        if expected_type is not None and expected_type != bool and isinstance(value, bool):
            raise VariableValidationError(
                name, value,
                f"expected type {self.type}, got bool"
            )
        if expected_type is not None and not isinstance(value, expected_type):
            # Allow int where float is expected (but not bool, already guarded above)
            if self.type == "float" and isinstance(value, int):
                pass
            else:
                raise VariableValidationError(
                    name, value,
                    f"expected type {self.type}, got {type(value).__name__}"
                )

        if self.min is not None and isinstance(value, (int, float)):
            if value < self.min:
                raise VariableValidationError(
                    name, value, f"below minimum {self.min}"
                )

        if self.max is not None and isinstance(value, (int, float)):
            if value > self.max:
                raise VariableValidationError(
                    name, value, f"above maximum {self.max}"
                )

        if self.allowed_values is not None and value not in self.allowed_values:
            raise VariableValidationError(
                name, value,
                f"not in allowed values: {self.allowed_values}"
            )


GLOBAL_VARIABLES_FILE = "global_variables.json"


_DECLARATION_FIELDS = frozenset({"type", "default", "min", "max", "allowed_values"})


class GlobalVariable(VariableDefinition):
    """One entry of `global_variables.json`: a declaration, the value an operator set, or both.

    ``value`` is what an operator set; ``default`` is what the author declared. An entry with no type, default,
    bounds or allowed values is a global nobody declared. A declared default and value must fit the declaration.
    """

    value: OptionValue | None = None

    @property
    def is_declared(self) -> bool:
        return bool(self.model_fields_set & _DECLARATION_FIELDS)

    @model_validator(mode="after")
    def _fits_its_declaration(self) -> Self:
        if not self.is_declared:
            return self
        for field, held in (("default", self.default), ("value", self.value)):
            if held is None:
                continue
            try:
                self.validate_value(field, held)
            except VariableValidationError as error:
                raise ValueError(str(error)) from error
        return self

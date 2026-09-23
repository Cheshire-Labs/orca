"""Variable definitions with validation constraints."""

from typing import Literal

from pydantic import BaseModel

from orca.variables.errors import OptionValue, VariableValidationError

_TYPE_MAP: dict[str, type] = {
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
}


class VariableDefinition(BaseModel):
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

"""Variable system errors."""

from typing import Union

OptionValue = Union[str, int, float, bool]


class UndefinedVariableError(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(f"Variable '{name}' is not defined in any layer and has no default")
        self.variable_name = name


class VariableTypeError(Exception):
    def __init__(self, name: str, expected: str, actual: str) -> None:
        super().__init__(
            f"Variable '{name}': expected type {expected}, got {actual}"
        )
        self.variable_name = name
        self.expected_type = expected
        self.actual_type = actual


class VariableValidationError(Exception):
    def __init__(self, name: str, value: OptionValue, reason: str) -> None:
        super().__init__(f"Variable '{name}' = {value!r}: {reason}")
        self.variable_name = name
        self.value = value

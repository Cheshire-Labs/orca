"""VariableRef[T]: generic value wrapper for the variable system.

Two concrete implementations:
- LiteralRef[T]: wraps a concrete value. resolve() returns it directly.
- NamedRef: references a named variable. Resolved by passing to a VariableResolver.

Users use Var() factory for named refs. ActionTemplate._wrap() creates LiteralRef.
"""

from abc import ABC, abstractmethod
from typing import Generic, TypeVar, Union


T = TypeVar("T")


class VariableRef(ABC, Generic[T]):
    """Base class: anything that can produce a value of type T."""

    @abstractmethod
    def resolve(self) -> T:
        raise NotImplementedError

    @property
    @abstractmethod
    def is_literal(self) -> bool:
        raise NotImplementedError

    @property
    def name(self) -> str | None:
        return None


class LiteralRef(VariableRef[T]):
    """Wraps a concrete value. resolve() returns it directly."""

    def __init__(self, value: T) -> None:
        self._value = value

    def resolve(self) -> T:
        return self._value

    @property
    def is_literal(self) -> bool:
        return True

    def __repr__(self) -> str:
        return f"LiteralRef({self._value!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, LiteralRef):
            return NotImplemented
        return self._value == other._value

    def __hash__(self) -> int:
        return hash(self._value)


class NamedRef(VariableRef[T]):
    """References a named variable. Just a name -- no store, no type, no default.

    Resolution happens externally via a VariableResolver that knows the
    execution context and walks the store hierarchy.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._resolved: list[T] = []

    def set_resolved_value(self, value: T) -> None:
        """Called by the resolver before execute(). Caches the value for this action execution."""
        self._resolved = [value]

    def clear_resolved_value(self) -> None:
        """Clear cached value (e.g., before retry to re-resolve)."""
        self._resolved = []

    def resolve(self) -> T:
        if not self._resolved:
            raise RuntimeError(
                f"Variable '{self._name}' has not been resolved. "
                f"The execution context must resolve variables before calling execute()."
            )
        return self._resolved[0]

    @property
    def is_literal(self) -> bool:
        return False

    @property
    def name(self) -> str:
        return self._name

    def __repr__(self) -> str:
        return f"NamedRef({self._name!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, NamedRef):
            return NotImplemented
        return self._name == other._name

    def __hash__(self) -> int:
        return hash(self._name)


VariableParam = Union[T, VariableRef[T]]


def Var(name: str) -> NamedRef:
    """Public factory for named variables."""
    return NamedRef(name)

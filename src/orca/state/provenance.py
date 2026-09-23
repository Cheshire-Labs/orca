"""How well the record knows a fact, and where the current answer came from.

Two separate questions, on every fact and not just contents. How well it is
known is ``Provenance``; who last spoke is ``Source``. Keeping them apart is
what stops a fourth state appearing every time a new kind of writer does.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Generic, TypeVar


class Provenance(Enum):
    """How much the record actually knows, and whether that needs a person.

    Three states, because there are only two questions worth asking: is there
    a value at all, and does somebody have to go and look. A boolean cannot
    tell "confirmed empty" from "nobody ever told me", which is the pair that
    has to be told apart before a number is acted on.

    There used to be a fourth, separating a value an operator stated from one
    the record folded on its own. Nothing ever branched on the difference: it
    is who last spoke, and ``Source`` on the same answer already says that.
    """

    UNKNOWN = "unknown"
    """Nothing has ever stated this. A read means exactly that, and never
    "empty"."""

    KNOWN = "known"
    """There is a value, and nothing has gone unobserved since the last thing
    anyone knew. Nobody needs to be asked."""

    STALE = "stale"
    """There is a value, but a stretch went unobserved since anyone last knew:
    a restart, a reconnect, an error pause. It is still the best value there
    is, and it is the only state worth asking an operator about."""


class Source(Enum):
    """Which layer produced the answer being read.

    Ordered by precedence, highest first: an operator outranks the record of
    what was commanded, which outranks the opening declaration.
    """

    OPERATOR = "operator"
    OPERATION = "operation"
    DECLARATION = "declaration"
    NONE = "none"


T = TypeVar("T")


@dataclass(frozen=True)
class Answer(Generic[T]):
    """A value together with how well it is known.

    Handing back a bare number is what let four surfaces report the same rack
    differently: each read looked equally confident, and one of them was
    guessing. An answer carries its own confidence so a caller cannot lose it.

    ``value`` is None exactly when ``provenance`` is UNKNOWN.
    """

    value: T | None
    provenance: Provenance
    source: Source

    @property
    def is_known(self) -> bool:
        return self.provenance is not Provenance.UNKNOWN

    @classmethod
    def unknown(cls) -> "Answer[T]":
        return cls(value=None, provenance=Provenance.UNKNOWN, source=Source.NONE)

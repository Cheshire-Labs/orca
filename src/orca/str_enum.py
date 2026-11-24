"""`StrEnum` on every Python this package supports."""

import sys
from enum import Enum

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:
    class StrEnum(str, Enum):
        """`enum.StrEnum` for Python 3.10: a member is its own string."""

        def __str__(self) -> str:
            return str.__str__(self)

        def __format__(self, spec: str) -> str:
            return str.__format__(self, spec)

__all__ = ["StrEnum"]

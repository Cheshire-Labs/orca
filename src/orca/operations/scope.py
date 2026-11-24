"""Discriminated-union value objects identifying *what an Operation acts on*.

Scope is data, not behavior. Pydantic
discriminated union over a `kind` literal. Closed set, exhaustive-checkable
at the type level. Reused across Pause, Resume, Recover, etc.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class ExecutionScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["execution"] = "execution"
    execution_id: str


class ThreadScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["thread"] = "thread"
    execution_id: str
    thread_id: str


Scope = Annotated[ExecutionScope | ThreadScope, Field(discriminator="kind")]

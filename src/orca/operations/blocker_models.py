"""The blocker list on the wire. Engine-free, so the CLI parses with these models."""

import hashlib
import json
from functools import lru_cache

from pydantic import BaseModel, ConfigDict, Field, JsonValue, create_model


class RemedyStepDTO(BaseModel):
    """One call in a remedy, named on the surface that sent it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verb: str
    action: str = Field(description="The action this step runs, the same on every surface.")
    label: str
    args: dict[str, JsonValue]
    rest: str = Field(description="`METHOD /path`, with `<arg>` where an argument goes in the path.")
    tool: str | None = Field(
        default=None, description="The named tool for this step. Null on a surface with no tools.",
    )
    cli: str
    needs: list[str] = Field(default_factory=list)
    query: list[str] = Field(
        default_factory=list,
        description="Argument names the REST route takes in the query string.",
    )


class RemedyDTO(BaseModel):
    """A named recovery: what it does, and the steps in order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    label: str
    explain: str
    steps: list[RemedyStepDTO]
    recommended: bool = False
    confirm: bool = False


class BlockerDTO(BaseModel):
    """One thing stopping the run, with the remedies that clear it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: str
    severity: str
    since: float | None = Field(
        default=None,
        description="Unix seconds. Null when nothing recorded a start time.",
    )
    headline: str
    remedies: list[RemedyDTO]
    detail: str | None = None
    device_name: str | None = None
    execution_id: str | None = None
    thread_id: str | None = None
    labware_id: str | None = None
    incident_id: str | None = None
    may_still_be_moving: bool = False
    blocks: list[str] = Field(default_factory=list)


class BlockersPayload(BaseModel):
    """Everything stopping the run, plus a way to tell one list from another."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    blockers: list[BlockerDTO]
    blocker_count: int
    blockers_fingerprint: str = Field(
        description="Changes when the set changes. Not ordered.",
    )
    blockers_known: bool = Field(
        default=True,
        description=(
            "False when the list could not be read. An empty list means nothing "
            "is in the way only while this is true."
        ),
    )


def payload_of(blockers: list[BlockerDTO], *, known: bool = True) -> BlockersPayload:
    return BlockersPayload(
        blockers=blockers,
        blocker_count=len(blockers),
        blockers_fingerprint=_fingerprint(blockers),
        blockers_known=known,
    )


def unknown_blockers() -> BlockersPayload:
    """Nobody could say. Never report this as nothing being in the way."""
    return payload_of([], known=False)


def _fingerprint(blockers: list[BlockerDTO]) -> str:
    # The headline is in it so a blocker whose situation moved on reads as a
    # different list even though its id is stable.
    material = json.dumps(
        [[b.id, b.headline, b.severity] for b in blockers], separators=(",", ":"),
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]


@lru_cache(maxsize=None)
def with_blockers(model: type[BaseModel]) -> type[BaseModel]:
    """An Operation's response plus the blocker fields, as one model."""
    return create_model(f"{model.__name__}AndBlockers", __base__=(model, BlockersPayload))


def still_blocked_line(payload: BlockersPayload) -> str:
    """What to say after a recovery call, so nobody reads success as finished."""
    if not payload.blockers_known:
        return "could not read what else is stopping the run; this is not an all-clear"
    if payload.blocker_count == 0:
        return "nothing else is in the way"
    if payload.blocker_count == 1:
        return "1 thing is still stopping the run"
    return f"{payload.blocker_count} things are still stopping the run"

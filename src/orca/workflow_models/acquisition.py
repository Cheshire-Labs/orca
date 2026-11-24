"""How a run takes hold of labware that already exists.

Two pure decisions. The submit check asks
``refusal_for`` about each labware a group names by barcode; the thread asks
``decide_start`` when it starts. Their callers only gather facts and carry the answer out.
The rows of both are pinned in ``tests/test_acquiring_existing_labware.py``.
"""

from dataclasses import dataclass
from enum import Enum


class StartDecision(str, Enum):
    SPAWN = "spawn"
    """Not on the deck: it is placed at the start (by a person in LIVE, by the engine in sim)."""
    USE_IN_PLACE = "use_in_place"
    MOVE = "move"
    """On the deck elsewhere: the arm carries it to the start like any other move."""
    ASK_OPERATOR = "ask_operator"
    """On the deck where no arm can reach the start from: a person puts it there."""


@dataclass(frozen=True)
class StartFacts:
    present: bool
    at_start: bool
    reachable: bool


def decide_start(facts: StartFacts) -> StartDecision:
    if not facts.present:
        return StartDecision.SPAWN
    if facts.at_start:
        return StartDecision.USE_IN_PLACE
    if facts.reachable:
        return StartDecision.MOVE
    return StartDecision.ASK_OPERATOR


@dataclass(frozen=True)
class MemberFacts:
    """What is true about one labware a submission names, at submit time."""

    labware_name: str
    labware_type: str
    wanted_type: str
    thread_template: str
    also_named_by: str | None = None
    """Another group member in the same submission that names this labware."""
    held_by: str | None = None
    """What already holds this labware: "thread <name>" or "execution <id>"."""
    in_jaws_of: str | None = None
    tip_layout_unknown: bool = False
    start_dispenses: bool = False
    """The thread's start hands out new labware (a stacker), so it takes none by name."""
    start: str = ""


def refusal_for(facts: MemberFacts) -> str | None:
    """Why this labware cannot start this thread, or None when it can."""
    name = facts.labware_name
    if facts.start_dispenses:
        return f"Thread {facts.thread_template} takes new labware from {facts.start}; name no labware for it."
    if facts.labware_type != facts.wanted_type:
        return f"{name} is a {facts.labware_type}; thread {facts.thread_template} needs a {facts.wanted_type}."
    if facts.also_named_by is not None:
        return f"{name} is also named by {facts.also_named_by}. One labware can start one thread."
    if facts.held_by is not None:
        return f"{name} is in use by {facts.held_by}. Wait for it to finish or stop it."
    if facts.in_jaws_of is not None:
        return f"{name} is in {facts.in_jaws_of}. Put it down or record where it is first."
    if facts.tip_layout_unknown:
        return no_stated_tips(name)
    return None


def no_stated_tips(rack_name: str) -> str:
    return f"Nothing says which tips {rack_name} holds. State them with set-tip-state."

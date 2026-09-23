"""Shared resolvers used by every CLI verb.

ID resolution. Every command that takes an execution id, thread id, or
reservation id accepts full UUIDs, unambiguous prefixes (>=4 chars), entity
names where applicable, or the literal `last` (most recently submitted in
this CLI session, persisted to `~/.orca/last.json`).

The system-module loader that used to live here is gone:
the daemon owns the system, and the CLI talks to it via HTTP. See
`orca.daemon.system_builder` for the daemon-side loader.
"""

import json
from pathlib import Path

from orca.cli import output


LAST_FILE = Path.home() / ".orca" / "last.json"


# -- ID resolution ------------------------------------------------------------


def _read_last() -> dict[str, str]:
    if not LAST_FILE.exists():
        return {}
    try:
        return json.loads(LAST_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _write_last(data: dict[str, str]) -> None:
    try:
        LAST_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_FILE.write_text(json.dumps(data, indent=2))
    except OSError:
        pass  # best-effort cache; never fail a command on this


def record_last_execution(execution_id: str) -> None:
    data = _read_last()
    data["execution_id"] = execution_id
    _write_last(data)


def get_last_execution_id() -> str | None:
    return _read_last().get("execution_id")


def resolve_id(
    requested: str,
    candidates: list[str],
    *,
    what: str = "id",
    min_prefix: int = 4,
) -> str:
    """Match `requested` against `candidates`. Exact > prefix > fail.

    Emits a CLI-appropriate exit on ambiguous / not-found. Returns on success.
    """
    if requested in candidates:
        return requested
    if len(requested) < min_prefix:
        output.fail(
            f"{what} {requested!r} too short to disambiguate "
            f"(need >= {min_prefix} chars)",
            code=output.EXIT_USAGE,
        )
    matches = [c for c in candidates if c.startswith(requested)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) == 0:
        output.not_found(what, requested)
    output.ambiguous(what, requested, matches)


def find_id_or_none(
    requested: str,
    candidates: list[str],
    *,
    min_prefix: int = 4,
) -> str | None:
    """Non-exiting variant of :func:`resolve_id` -- returns the resolved
    id on exact-or-unique-prefix match, ``None`` for any other outcome
    (too-short, no match, ambiguous).

    Used by verbs that fall back to a sibling lookup (e.g. ``labware where``
    tries id first then barcode); the calling code drives the failure
    message after both lookups miss.
    """
    if requested in candidates:
        return requested
    if len(requested) < min_prefix:
        return None
    matches = [c for c in candidates if c.startswith(requested)]
    if len(matches) == 1:
        return matches[0]
    return None


def resolve_execution_id(requested: str, known_ids: list[str]) -> str:
    """Resolve an execution id from user input.

    Accepts: full id, prefix (>=4 chars), or the literal `last`.
    """
    if requested == "last":
        last = get_last_execution_id()
        if last is None:
            output.fail(
                "no 'last' execution recorded in this session",
                code=output.EXIT_NOT_FOUND,
            )
        if last not in known_ids:
            output.fail(
                f"recorded 'last' execution {last!r} is no longer known to runtime",
                code=output.EXIT_NOT_FOUND,
            )
        return last
    return resolve_id(requested, known_ids, what="execution")


def resolve_thread_id(requested: str, known_ids: list[str]) -> str:
    return resolve_id(requested, known_ids, what="thread")


def resolve_reservation_id(requested: str, known_ids: list[str]) -> str:
    return resolve_id(requested, known_ids, what="reservation")

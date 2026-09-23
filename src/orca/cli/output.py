"""CLI output primitives: exit codes + JSON/table/quiet emission.

Every CLI verb goes through this module for its final write. Keeps Rich (or
its absence under `--no-color`) centralized and lets `--json` produce a
schema that's identical to what a future REST/MCP surface would return --
since snapshots are frozen dataclasses, `dataclasses.asdict()` is the single
JSON contract.

Stdout is for command output (tables, JSON). Stderr is for status/error
messages. `--quiet` suppresses stdout (exit code is the channel). NDJSON is
emitted one dataclass per line for streaming verbs (events, logs tail).
"""

import dataclasses
import json
import sys
from dataclasses import is_dataclass
from enum import Enum
from typing import Any, Iterable, NoReturn, Sequence

import httpx
import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table


# Exit codes (stable contract; scripts depend on these)
EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_USAGE = 2
EXIT_NOT_CONNECTED = 10
EXIT_NOT_FOUND = 20
EXIT_AMBIGUOUS = 21
EXIT_CONFLICT = 30
EXIT_INVALID_STATE = 31
EXIT_CONFIRMATION_DENIED = 40
EXIT_TIMEOUT = 50
EXIT_PLUGIN_ERROR = 60


def exit_code_for_status(http_status: int | None) -> int:
    """The exit code for a backend call that answered with this HTTP status."""
    if http_status == 404:
        return EXIT_NOT_FOUND
    if http_status == 409:
        return EXIT_CONFLICT
    if http_status == 400:
        return EXIT_USAGE
    return EXIT_GENERIC


def transport_failure(
    error: httpx.HTTPError, peer: str, timeout_s: float,
) -> tuple[str, int]:
    """The message and exit code for a request that got no answer.

    Only a failed connect means the peer is unreachable. A timeout or a
    dropped connection can come after the peer has started the work.
    """
    if isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout)):
        return f"cannot reach {peer}: {type(error).__name__}: {error}", EXIT_NOT_CONNECTED
    if isinstance(error, httpx.TimeoutException):
        return (
            f"{peer} did not answer within {timeout_s:g} s. The request may "
            "still complete: run `orca status` before retrying.",
            EXIT_TIMEOUT,
        )
    return (
        f"lost the connection to {peer}: {type(error).__name__}: {error}. "
        "The request may or may not have completed: run `orca status` before retrying.",
        EXIT_NOT_CONNECTED,
    )


def _stdout() -> Console:
    """Rich console bound to the current sys.stdout.

    Intentionally created per call: test harnesses (CliRunner, pytest capsys)
    monkey-patch sys.stdout, and a cached console would hold on to the
    pre-patch file and write nowhere visible.
    """
    return Console(file=sys.stdout, soft_wrap=False, highlight=False)


def _stderr() -> Console:
    return Console(file=sys.stderr, soft_wrap=False, highlight=False)


# -- Output mode (set once in app.py main callback, read here) ----------------


class OutputMode(Enum):
    TABLE = "table"
    JSON = "json"
    QUIET = "quiet"


_mode: OutputMode = OutputMode.TABLE


def set_mode(mode: OutputMode) -> None:
    global _mode
    _mode = mode


def get_mode() -> OutputMode:
    return _mode


# -- Serialization ------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    """Recursively convert frozen dataclasses / enums into JSON-safe types."""
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, Enum):
        return value.value if isinstance(value.value, (str, int, float)) else value.name
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def to_json(value: Any) -> str:
    """Produce a stable JSON string for any snapshot, dict, list, or scalar."""
    return json.dumps(_jsonable(value), indent=2, default=str)


# -- Emission -----------------------------------------------------------------


def emit_json(value: Any) -> None:
    """Write a JSON document to stdout (single object).

    No-op unless the active mode is JSON. Call sites typically pair this with
    ``emit_kv`` / ``emit_table`` so each verb always builds both forms and the
    active mode decides which one renders; without the mode check here, table
    mode would print both.

    Writes via ``sys.stdout.write`` rather than Rich so that long JSON string
    values are not auto-wrapped at the console width (which would embed a
    literal newline inside the quoted string and invalidate the JSON for
    downstream parsers).
    """
    if _mode != OutputMode.JSON:
        return
    sys.stdout.write(to_json(value) + "\n")
    sys.stdout.flush()


def emit_ndjson(value: Any) -> None:
    """Write a single NDJSON line to stdout (streaming verbs).

    Emits only in JSON mode. Streaming verbs that have no table rendering
    (events, logs tail) force JSON mode for the duration of the command so
    this still fires.
    """
    if _mode != OutputMode.JSON:
        return
    # No indent for NDJSON; one compact object per line.
    sys.stdout.write(json.dumps(_jsonable(value), default=str) + "\n")
    sys.stdout.flush()


def emit_table(
    title: str | None,
    columns: list[str],
    rows: Iterable[Sequence[str | None]],
) -> None:
    """Render a Rich table to stdout (skipped in JSON/quiet modes).

    Cells may be ``str`` or ``None``. ``None`` is the legitimate domain
    answer for nullable identifier fields (``thread_id``, ``execution_id``,
    ``operator_id``, etc.) on the rows passed in. The renderer converts
    None to an empty cell internally so call sites pass the field
    directly -- they must NOT coerce with ``or "..."`` or
    ``x if x is not None else "..."`` at the data layer, which would
    propagate a magic string into the data.
    """
    if _mode != OutputMode.TABLE:
        return
    table = Table(title=title, show_lines=False, header_style="bold cyan")
    for col in columns:
        table.add_column(col)
    for row in rows:
        table.add_row(*(cell or "" for cell in row))
    _stdout().print(table)


def emit_kv(title: str | None, pairs: list[tuple[str, str | None]]) -> None:
    """Render a simple key-value block (detail views).

    Values may be ``str`` or ``None``. ``None`` renders as an empty value;
    call sites pass nullable identifier fields directly without coercion.
    """
    if _mode != OutputMode.TABLE:
        return
    if title:
        _stdout().print(f"[bold cyan]{title}[/bold cyan]")
    for key, val in pairs:
        # Values are free text: driver messages, operator-written docstrings.
        # Rich would read a bracketed word in one as markup and eat it, or raise.
        _stdout().print(f"  [dim]{key}:[/dim] {escape(val or '')}")


def info(msg: str) -> None:
    """Status / progress message to stderr (not suppressed by --quiet)."""
    _stderr().print(msg)


def warn(msg: str) -> None:
    if _mode == OutputMode.JSON:
        # In JSON mode, warnings still go to stderr so stdout stays parseable.
        _stderr().print(f"[yellow]warning:[/yellow] {msg}")
    else:
        _stderr().print(f"[yellow]warning:[/yellow] {msg}")


def error(msg: str) -> None:
    _stderr().print(f"[red]error:[/red] {msg}")


# -- Fatal-exit helpers (raise typer.Exit with a contextual code) -------------


def fail(msg: str, code: int = EXIT_GENERIC) -> NoReturn:
    """Print an error and exit with the given code. Never returns."""
    error(msg)
    raise typer.Exit(code=code)


def not_found(what: str, identifier: str) -> NoReturn:
    fail(f"{what} not found: {identifier!r}", code=EXIT_NOT_FOUND)


def ambiguous(what: str, identifier: str, matches: list[str]) -> NoReturn:
    joined = "\n  ".join(matches[:10])
    fail(
        f"{what} prefix {identifier!r} matches multiple entries:\n  {joined}",
        code=EXIT_AMBIGUOUS,
    )


def invalid_state(msg: str) -> NoReturn:
    fail(msg, code=EXIT_INVALID_STATE)


def not_connected(msg: str = "no runtime available") -> NoReturn:
    fail(msg, code=EXIT_NOT_CONNECTED)


def confirmation_denied(msg: str = "confirmation denied") -> NoReturn:
    fail(msg, code=EXIT_CONFIRMATION_DENIED)

"""Entry point for running the Orca CLI.

Usage: python -m orca.cli
Or (once installed): orca <noun> <verb>

Delegates to the Typer app in `orca.cli.app`.
"""

import errno
import os
import sys

from orca.cli.app import app

# Windows reports a write to a pipe its reader closed as EINVAL, where POSIX reports EPIPE.
_CLOSED_PIPE = frozenset({errno.EPIPE, errno.EINVAL})


def _discard_what_is_left_to_write() -> None:
    os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())


def main() -> None:
    try:
        app()
    except OSError as failed:
        if failed.errno not in _CLOSED_PIPE:
            raise
        # The interpreter flushes stdout as it exits, and would fail on the closed pipe a second time.
        _discard_what_is_left_to_write()
        sys.exit(1)


if __name__ == "__main__":
    main()

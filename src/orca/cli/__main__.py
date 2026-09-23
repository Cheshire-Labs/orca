"""Entry point for running the Orca CLI.

Usage: python -m orca.cli
Or (once installed): orca <noun> <verb>

Delegates to the Typer app in `orca.cli.app`.
"""

import sys

from orca.cli import output
from orca.cli.app import app
from orca.cli.control_plane import ControlPlaneError


def main() -> None:
    # A backend client raises ControlPlaneError; without this the operator
    # gets a traceback instead of the message it carries.
    try:
        app()
    except ControlPlaneError as exc:
        output.error(str(exc))
        if exc.exit_code is not None:
            sys.exit(exc.exit_code)
        sys.exit(output.exit_code_for_status(exc.http_status))


if __name__ == "__main__":
    main()

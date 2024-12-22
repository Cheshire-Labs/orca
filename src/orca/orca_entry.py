

import argparse
from orca.cli.orca_cli import OrcaCmdShell
from orca.cli.orca_rest_api import uvicorn_server, setup_logging

def main():
    parser = argparse.ArgumentParser(prog="orca", description="Orca CLI Tool")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Interactive shell subcommand
    subparsers.add_parser("interactive", help="Enter interactive shell")

    # Server subcommand
    subparsers.add_parser("server", help="Start Orca server")

    # No subcommand means direct CLI command execution
    parser.add_argument("args", nargs=argparse.REMAINDER, help="Command to execute")

    # Parse the arguments
    args = parser.parse_args()

    if args.command == "interactive":
        OrcaCmdShell().cmdloop()
    elif args.command == "server":
        setup_logging()
        uvicorn_server.start()
    elif args.args:
        # Execute the command using OrcaCmdShell's onecmd
        OrcaCmdShell().onecmd(" ".join(args.args))
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
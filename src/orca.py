import logging

from cli.orca_cli import OrcaCmdShell


logging.basicConfig(handlers=[logging.StreamHandler()], level=logging.DEBUG)
OrcaCmdShell().cmdloop()
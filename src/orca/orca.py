import logging

from orca.cli.orca_cli import OrcaCmdShell


logging.basicConfig(handlers=[logging.StreamHandler()], level=logging.DEBUG)

def main():
    OrcaCmdShell().cmdloop()

if __name__ == "__main__":
    main()
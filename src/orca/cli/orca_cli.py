import argparse
import cmd
import logging
import traceback
import colorama

import shlex
from typing import Dict

from orca.cli.local_shell import LocalOrcaShell
from orca.cli.shell_interface import IOrcaShell


class OrcaCmdShell(cmd.Cmd):
    intro = "Welcome to the Orca Shell.  Type help or ? to list commands.\n"
    prompt = "\033[31morca> \033[0m"
    
    def __init__(self) -> None:
        colorama.init()
        super().__init__()
        self._orca_shell: IOrcaShell = LocalOrcaShell()
        self._parsers: Dict[str, argparse.ArgumentParser] = self._create_parsers()

    def do_load(self, arg: str):
        """Load the configuration file"""
        try:
            args = self._parsers["load"].parse_args(shlex.split(arg))
            print("Loading configuration file")
            self._orca_shell.load(args.config)
        except SystemExit as e:
            print(f"Argument parsing error: {e}")
            self._parsers["load"].print_help()
        except Exception as e:
            print(f"Error: {e}")
            traceback.print_exc()

    def do_init(self, arg: str):
        """Initialize the lab instruments"""
        try:            
            args = self._parsers["init"].parse_args(shlex.split(arg))
            print("Initializing lab instruments")
            self._orca_shell.init(args.config, args.resources)
        except SystemExit as e:
            print(f"Argument parsing error: {e}")
            self._parsers["init"].print_help()
        except Exception as e:
            print(f"Error: {e}")
            traceback.print_exc()

    def do_run(self, arg: str):
        """Run the workflow"""
        try:
            args = self._parsers["run"].parse_args(shlex.split(arg))
            print("Running workflow")
            self._orca_shell.run_workflow(args.workflow, args.config)
        except SystemExit as e:
            print(f"Argument parsing error: {e}")
            self._parsers["run"].print_help()
        except Exception as e:
            print(f"Error: {e}")
            traceback.print_exc()

    def do_run_method(self, arg: str):
        """Run a specific method"""
        try:
            args = self._parsers["run_method"].parse_args(shlex.split(arg))
            print("Running method")
            self._orca_shell.run_method(args.method, args.start_map, args.end_map, args.config)
        except SystemExit as e:
            
            print(f"Argument parsing error: {e}")
             
            self._parsers["run_method"].print_help()
        except Exception as e:
            print(f"Error: {e}")
            traceback.print_exc()

    def do_exit(self, arg: str):
        """Exit the shell"""
        print("Exiting Orca Shell")
        return True
    
    def do_quit(self, arg: str):
        """Exit the shell"""
        return self.do_exit(arg)

    def _create_parsers(self):
        """Create argument parsers for each command."""
        parsers = {}

        # Parser for the 'load' command
        load_parser = argparse.ArgumentParser(prog='load', description="Load the configuration file")
        load_parser.add_argument("--config", required=True, help="Configuration file")
        parsers['load'] = load_parser

        # Parser for the 'init' command
        init_parser = argparse.ArgumentParser(prog='init', description="Initialize the lab instruments")
        init_parser.add_argument("--config", help="Configuration file")
        init_parser.add_argument("--resources", help="List of resources to initialize. All others will be excluded.")
        parsers['init'] = init_parser

        # Parser for the 'run' command
        run_parser = argparse.ArgumentParser(prog='run', description="Run the workflow")
        run_parser.add_argument("--workflow", required=True, help="Workflow to be run")
        run_parser.add_argument("--stage", help="Development stage to be run")
        run_parser.add_argument("--config", help="Configuration file")
        parsers['run'] = run_parser

        # Parser for the 'run_method' command
        run_method_parser = argparse.ArgumentParser(prog='run_method', description="Run a specific method")
        run_method_parser.add_argument("--method", required=True, help="Method to be run")
        run_method_parser.add_argument("--start-map", required=True, help="JSON Dictionary of labware to start location mapping")
        run_method_parser.add_argument("--end-map", required=True, help="JSON Dictionary of labware to end location mapping")
        run_method_parser.add_argument("--stage", help="Development stage to be run")
        run_method_parser.add_argument("--config", help="Configuration file")
        parsers['run_method'] = run_method_parser

        return parsers
    
    def help_load(self):
        """Show help for the load command"""
        self._parsers['load'].print_help()
    
    def help_init(self):
        """Show help for the init command"""
        self._parsers['init'].print_help()

    def help_run(self):
        """Show help for the run command"""
        self._parsers['run'].print_help()

    def help_run_method(self):
        """Show help for the run_method command"""
        self._parsers['run_method'].print_help()

    def do_help(self, arg: str):
        if arg:
            try:
                # Try to show help for the specific command
                func_name = f"help_{arg.replace('-', '_')}"
                func = getattr(self, func_name)
                func()
            except AttributeError:
                print(f"No help available for {arg}")
        else:
            print("Available commands:")
            commands = [name[3:] for name in dir(self) if name.startswith('do_')]
            for command in commands:
                print(command)
            print("\nType 'help <command>' to get help on a specific command.")
    
if __name__ == "__main__":
    logging.basicConfig(handlers=[logging.StreamHandler()], level=logging.DEBUG)
    OrcaCmdShell().cmdloop()
    # C:\Users\miike\source\repos\cheshire-orca\examples\smc_assay\smc_assay_example.yml
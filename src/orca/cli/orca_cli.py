import argparse
import asyncio
import cmd
import traceback
import colorama

import shlex
from typing import Dict

from orca.cli.orca_api import OrcaApi


class OrcaCmdShell(cmd.Cmd):
    intro = "Welcome to the Orca Shell.  Type help or ? to list commands.\n"
    prompt = "\033[31morca> \033[0m"
    
    def __init__(self) -> None:
        colorama.init()
        super().__init__()
        self._orca_shell: OrcaApi = OrcaApi()
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
    
    def do_list_workflow_recipes(self, arg: str):
        """List available workflow recipes"""
        try:
            args = self._parsers["list_workflow_recipes"].parse_args(shlex.split(arg))
            print("Available Workflow Recipes:")
            for recipe in self._orca_shell.get_workflow_recipes():
                print(f"{recipe}")
        except SystemExit as e:
            print(f"Argument parsing error: {e}")
            self._parsers["list_workflow_recipes"].print_help()
        except Exception as e:
            print(f"Error: {e}")
            traceback.print_exc()

    def do_list_method_recipes(self, arg: str):
        """List available method recipes"""
        try:
            args = self._parsers["list_method_recipes"].parse_args(shlex.split(arg))
            print("Available Method Recipes:")
            for recipe in self._orca_shell.get_method_recipes():
                print(f"{recipe}")
        except SystemExit as e:
            print(f"Argument parsing error: {e}")
            self._parsers["list_method_recipes"].print_help()
        except Exception as e:
            print(f"Error: {e}")
            traceback.print_exc()

    def do_list_locations(self, arg: str):
        """List all locations"""
        try:
            args = self._parsers["list_locations"].parse_args(shlex.split(arg))
            print("Locations:")
            for location in self._orca_shell.get_locations():
                print(f"{location}")
        except SystemExit as e:
            print(f"Argument parsing error: {e}")
            self._parsers["list_locations"].print_help()
        except Exception as e:
            print(f"Error: {e}")
            traceback.print_exc()

    def do_list_resources(self, arg: str):
        """List all resources"""
        try:
            args = self._parsers["list_resources"].parse_args(shlex.split(arg))
            print("Resources:")
            for resource in self._orca_shell.get_resources():
                print(f"{resource}")
        except SystemExit as e:
            print(f"Argument parsing error: {e}")
            self._parsers["list_resources"].print_help()
        except Exception as e:
            print(f"Error: {e}")
            traceback.print_exc()

    def do_list_equipments(self, arg: str):
        """List all equipments"""
        try:
            args = self._parsers["list_equipments"].parse_args(shlex.split(arg))
            print("Equipments:")
            for equipment in self._orca_shell.get_equipments():
                print(f"{equipment}")
        except SystemExit as e:
            print(f"Argument parsing error: {e}")
            self._parsers["list_equipments"].print_help()
        except Exception as e:
            print(f"Error: {e}")
            traceback.print_exc()

    def do_list_transporters(self, arg: str):
        """List all transporters"""
        try:
            args = self._parsers["list_transporters"].parse_args(shlex.split(arg))
            print("Transporters:")
            for transporter in self._orca_shell.get_transporters():
                print(f"{transporter}")
        except SystemExit as e:
            print(f"Argument parsing error: {e}")
            self._parsers["list_transporters"].print_help()
        except Exception as e:
            print(f"Error: {e}")
            traceback.print_exc()

    def do_install_driver(self, arg: str):
        """Install the driver"""
        try:
            args = self._parsers["install_driver"].parse_args(shlex.split(arg))
            print("Installing driver")
            self._orca_shell.install_driver(args.name, args.url)
        except SystemExit as e:
            print(f"Argument parsing error: {e}")
            self._parsers["install_driver"].print_help()
        except Exception as e:
            print(f"Error: {e}")
            traceback.print_exc()

    def do_uninstall_driver(self, arg: str):
        """Uninstall the driver"""
        try:
            args = self._parsers["uninstall_driver"].parse_args(shlex.split(arg))
            print("Uninstalling driver")
            self._orca_shell.uninstall_driver(args.name)
        except SystemExit as e:
            print(f"Argument parsing error: {e}")
            self._parsers["uninstall_driver"].print_help()
        except Exception as e:
            print(f"Error: {e}")
            traceback.print_exc()

    def do_list_installed_drivers(self, arg: str):
        """List Installed drivers"""
        try:
            args = self._parsers["list_installed_drivers"].parse_args(shlex.split(arg))
            print("Installed Drivers:")
            for name in self._orca_shell.get_installed_drivers_info().items():
                print(f"{name}")
        except SystemExit as e:
            print(f"Argument parsing error: {e}")
            self._parsers["list_installed_drivers"].print_help()
        except Exception as e:
            print(f"Error: {e}")
            traceback.print_exc()


    def do_list_available_drivers(self, arg: str):
        """List Available drivers"""
        try:
            args = self._parsers["list_available_drivers"].parse_args(shlex.split(arg))
            print("Available Drivers:")
            for name, info in self._orca_shell.get_available_drivers_info().items():
                print(f"{name}")
        except SystemExit as e:
            print(f"Argument parsing error: {e}")
            self._parsers["list_available_drivers"].print_help()
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

        # Parser for the 'install_driver' command
        install_driver_parser = argparse.ArgumentParser(prog='install_driver', description="Install the driver")
        install_driver_parser.add_argument("--name", required=True, help="Driver name")
        install_driver_parser.add_argument("--url", help="URL to the driver's git repo")
        parsers['install_driver'] = install_driver_parser

        # Parser for the 'uninstall_driver' command
        uninstall_driver_parser = argparse.ArgumentParser(prog='uninstall_driver', description="Uninstall the driver")
        uninstall_driver_parser.add_argument("--name", required=True, help="Driver name")
        parsers['uninstall_driver'] = uninstall_driver_parser

        # Parser for the 'list_available_drivers' command
        list_available_drivers_parser = argparse.ArgumentParser(prog='list_available_drivers', description="List available drivers")
        parsers['list_available_drivers'] = list_available_drivers_parser

        # Parser for the 'list_installed_drivers' command
        list_installed_drivers_parser = argparse.ArgumentParser(prog='list_installed_drivers', description="List installed drivers")
        parsers['list_installed_drivers'] = list_installed_drivers_parser

        # Parser for 'list_workflow_recipes' command
        list_workflow_parser = argparse.ArgumentParser(prog='list_workflow_recipes', description="List available workflow recipes")
        parsers['list_workflow_recipes'] = list_workflow_parser

        # Parser for 'list_method_recipes' command
        list_method_parser = argparse.ArgumentParser(prog='list_method_recipes', description="List available method recipes")
        parsers['list_method_recipes'] = list_method_parser

        # Parser for 'list_locations' command
        list_locations_parser = argparse.ArgumentParser(prog='list_locations', description="List all locations")
        parsers['list_locations'] = list_locations_parser

        # Parser for 'list_resources' command
        list_resources_parser = argparse.ArgumentParser(prog='list_resources', description="List all resources")
        parsers['list_resources'] = list_resources_parser

        # Parser for 'list_equipments' command
        list_equipments_parser = argparse.ArgumentParser(prog='list_equipments', description="List all equipments")
        parsers['list_equipments'] = list_equipments_parser

        # Parser for 'list_transporters' command
        list_transporters_parser = argparse.ArgumentParser(prog='list_transporters', description="List all transporters")
        parsers['list_transporters'] = list_transporters_parser

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

    def help_install_driver(self):
        """Show help for the install_driver command"""
        self._parsers['install_driver'].print_help()

    def help_uninstall_driver(self):
        """Show help for the uninstall_driver command"""
        self._parsers['uninstall_driver'].print_help()

    def help_list_available_drivers(self):
        """Show help for the list_drivers command"""
        self._parsers['list_available_drivers'].print_help()

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

def run_interactive_shell():
    shell = OrcaCmdShell()

    async def main_loop():
        loop = asyncio.get_running_loop()
        shell.cmdloop()

    asyncio.run(main_loop())
    
if __name__ == "__main__":
    OrcaCmdShell().cmdloop()
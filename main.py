import argparse
import subprocess
from abc import ABC
from typing import Any, Dict, List
from pyaml import yaml
from dataclasses import dataclass



class IRoboticArm(IResource):
    def pick(self, picking_name: str) -> None:
        raise NotImplementedError
    def place(self, placing_name: str) -> None:
        raise NotImplementedError

    def set_plate_type(self, plate_type: str) -> None:
        raise NotImplementedError


class MockACell(IRoboticArm):
    def __init__(self):

    def pick(self, picking_name: str) -> None:
        raise NotImplementedError
    
    def place(self, placing_name: str) -> None:
        raise NotImplementedError


class HamiltonVantage(BaseResource):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._exe_path = r"C:\Program Files (x86)\HAMILTON\Bin\HxRun.exe"
        if self._options['exe_path'] is not None:
            self._exe_path = self._options['exe_path']
        if self._options['protocol'] is None:
            raise ValueError("No protocol set")
        self._protocol = self._options['protocol']

    def initialize(self) -> bool:
        # add a simple hamilton initialization script here

        raise NotImplementedError

    def load_plate(self) -> None:
        print("Move carriage to load position")

    def unload_plate(self) -> None:
        print("Move carriage to unload position")

    async def execute(self) -> None:
        try:
            self._is_running = True
            subprocess.run([self._exe_path, "-t", self._protocol], shell=True, check=True)
        except subprocess.CalledProcessError as e:
            print(f"Error: {e}")
        finally:
            self._is_running = False

class MockResource(BaseResource):
    def __init__(self, config: Dict[str, Any], resource_name: str):
        super().__init__(config)
        self._resource_name = resource_name

    def load_plate(self) -> None:
        self._is_running = True
        print(f"{self._resource_name} open plate door")
        self._is_running = False

    def unload_plate(self) -> None:
        self._is_running = True
        print(f"{self._resource_name} close plate door")
        self._is_running = False

    def is_running(self) -> bool:
        return self._is_running

    def execute(self) -> None:
        self._is_running = True
        print("f{self.resource_name} execute")
        self._is_running = False


class ActionCommand:
    def __init__(self, resource: IResource, command: str, options: Dict[str, Any]):
        self.resource: IResource = resource
        self.command = command
        self.options: Dict[str, Any] = options

    def execute(self):
        self.resource.execute()

class Method:
    def __init__(self):
        self.actions: List[ActionCommand] = []

    def execute(self):
        for action in self.actions:
            # get the plate pad location for that resource
            resource = action.resource

           # with in this the robot will need to manage the resource load

            # Run resource execute
            action.execute()



class Workflow:
    def __init__(self):
        self.steps: List[Method] = []

    def execute(self):
        for step in self.steps:


            step.execute()




@dataclass
class LabwareConfigInfo:
    name: str
    type: str


class Cheshire:
    def __init__(self):


    def _load_conifg(self, config_file: str):
        self.config = Config(config_file)
        self.config.load_resources()

    def _configure_system(self):

        # configure the labware

        # configure the resources
        # configure the methods
        # configure the workflows

    def run(self):
        # configure the system

        # build the commands to execute in the workflow



class Config:

    def __init__(self, config_file: str):
        with open(config_file) as f:
            self._yaml = yaml.load(f, Loader=yaml.FullLoader)
            self._labware: Dict[str, LabwareConfigInfo] = {}
            self._resources = {}
            self._resource_to_plate_pad = {}

    def _load_labware(self) -> None:
        for l in self._yaml['labwares']:
            if 'name' not in l:
                raise ValueError("No labware name defined in config")
            if 'type' not in l:
                raise ValueError("No labware type defined in config")
            self._labware[l['name']] = l
    def load_resources(self) -> None:
        if 'resources' not in self._yaml:
            raise ValueError("No resources defined in config")
        self._resources = {}
        for r in self._yaml['resources']:
            if 'type' not in r:
                raise ValueError("No resource type defined in config")
            if r['type'] == 'vantage':
                resource = HamiltonVantage(r)
            elif r['type'] == 'cwash':
                resource = MockResource(r, "CWash")
            elif r['type'] == 'mantis':
                resource = MockResource(r, "Mantis")
            elif r['type'] == 'analytic-jena':
                resource = MockResource(r, "Analytic Jena")
            elif r['type'] == 'tapestation-4200':
                resource = MockResource(r, "Tapestation 4200")
            elif r['type'] == 'acell':
                resource = MockRoboticArm(r)
            elif r['type'] == 'mock-robot':
                resource = MockRoboticArm(r)
            else:
                raise ValueError(f"Unknown resource type: {r['type']}")
            self._resources[r] = resource
            if r['plate-pad']:
                self._resources[r] = r['plate-pad']



def run(config_file:str, method:str = None, workflow:str = None):


    raise NotImplementedError()

def check():
    raise NotImplementedError()

def init():
    raise NotImplementedError()

def main():
    parser = argparse.ArgumentParser(description="Lab Automation Orchestrator")
    subparsers = parser.add_subparsers(title="subcommands", dest="subcommand")

    # RUN
    run_parser = subparsers.add_parser("run", help="Run the workflow")
    run_parser.add_argument("--config", help="Configuration file")
    run_parser.add_argument("--method", help="Method to be run")
    run_parser.add_argument("--workflow", help="Workflow to be run")

    # INIT
    init_parser = subparsers.add_parser("init", help="Initialize the lab instruments")
    init_parser.add_argument("--config", help="Configuration file")

    # CHECK
    check_parser = subparsers.add_parser("check", help="Check syntax errors within the configuration")
    check_parser.add_argument("--config", help="Configuration file")
    run_parser.add_argument("--method", help="Method to check")
    run_parser.add_argument("--workflow", help="Workflow to check")


# Press the green button in the gutter to run the script.
if __name__ == '__main__':
    main()


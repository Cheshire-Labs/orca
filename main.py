import argparse
import asyncio
from typing import Any, Dict, Optional
from method_executor import MethodExecutor
from resource_models.labware import LabwareTemplate
from resource_models.location import Location
from workflow_executor import WorkflowExecuter
from yml_config_builder.config_file import ConfigFile
import json

class Orca:
    @staticmethod
    def run_workflow(config_file: str, 
                     workflow_name: Optional[str] = None, 
                     options: Dict[str, Any] = {}):
        loop: asyncio.AbstractEventLoop | None = asyncio.get_event_loop()
        config = ConfigFile(config_file)
        config.set_command_line_options(options)
        system = config.get_system()
        
        if workflow_name is None:
            raise ValueError("workflow is None.  Workflow must be a string")
        try:
            workflow_template = system.get_workflow_template(workflow_name)
        except KeyError:
            raise LookupError(f"Workflow {workflow_name} is not defined with then System.  Make sure it is included in the config file and the config file loaded correctly.")
        workflow_template = system.get_workflow_template(workflow_name)
        
        executer = WorkflowExecuter(workflow_template, 
                                    system, 
                                    system, 
                                    system)
        executer.execute()

    @staticmethod
    def run_method(config_file: str, 
                   method_name: Optional[str] = None, 
                   start_map_json: Optional[str] = None, 
                   end_map_json: Optional[str] = None, 
                   options: Dict[str, str] = {}):
        config = ConfigFile(config_file)
        config.set_command_line_options(options)
        system = config.get_system()
        if method_name is None:
            raise ValueError("method is None.  Method must be a string")
        try:
            method_template = system.get_method_template(method_name)
        except KeyError:
            raise LookupError(f"Method {method_name} is not defined with then System.  Make sure it is included in the config file and the config file loaded correctly.")
        if start_map_json is None:
            raise ValueError("start_map is None.  Start_map must be a string")
        if end_map_json is None:
            raise ValueError("end_map is None.  End_map must be a string")
        
        def labware_location_hook(d: Dict[str, str]):
            conversion: Dict[LabwareTemplate, Location] = {}
            for key, value in d.items():
                try:
                    template = system.get_labware_template(key)
                except KeyError:
                    raise ValueError(f"Labware {key} is not defined in the system")
                try:
                    location = system.get_location(value)
                except KeyError:
                    raise ValueError(f"Location {value} is not defined in the system")
                conversion[template] = location
            return conversion

        start_map: Dict[LabwareTemplate, Location] = json.loads(start_map_json, object_hook=labware_location_hook)
        end_map: Dict[LabwareTemplate, Location] = json.loads(end_map_json, object_hook=labware_location_hook)

        method_template = system.get_method_template(method_name)
        executer = MethodExecutor(method_template,
                                 start_map, 
                                 end_map,
                                 system,
                                 system)
        executer.execute()

    @staticmethod
    def check():
        raise NotImplementedError()
    
    @staticmethod
    def init():
        raise NotImplementedError()

def main():
    parser = argparse.ArgumentParser(description="Lab Automation Orchestrator")
    subparsers = parser.add_subparsers(title="subcommands", dest="subcommand")

    # RUN
    run_parser = subparsers.add_parser("run", help="Run the workflow")
    run_parser.add_argument("--config", help="Configuration file")
    run_parser.add_argument("--workflow", help="Workflow to be run")
    run_parser.add_argument("--stage", help="Development stage to be run")

    # RUN METHOD
    run_method_parser = subparsers.add_parser("run-method", help="Run a specific method")
    run_method_parser.add_argument("--config", help="Configuration file")
    run_method_parser.add_argument("--stage", help="Development stage to be run")
    run_method_parser.add_argument("--method", help="Method to be run")
    run_method_parser.add_argument("--start-map", help="Json Dictionary of labware to start location mapping")
    run_method_parser.add_argument("--end-map", help="Json Dictionary of labware to end location mapping")

    # INIT
    init_parser = subparsers.add_parser("init", help="Initialize the lab instruments")
    init_parser.add_argument("--config", help="Configuration file")

    # CHECK
    check_parser = subparsers.add_parser("check", help="Check syntax errors within the configuration")
    check_parser.add_argument("--config", help="Configuration file")
    run_parser.add_argument("--method", help="Method to check")

    args = parser.parse_args()
    options = {}
    if args.stage:
        options["stage"] = args.stage

    try:
        if args.subcommand == "run":
            Orca.run_workflow(args.config, args.workflow, options)
        elif args.subcommand == "run-method":
            Orca.run_method(args.config, args.method, args.start_map, args.end_map, options)
    except ValueError as ve:
        print(f"Error: {ve}")
    except LookupError as le:
        print(f"Error: {le}")

    



if __name__ == '__main__':
    # Orca.run(config_file="tests\\mock_config1.yml", workflow="test-workflow2")
    # Orca.run(config_file="examples\\smc_assay\\smc_assay_example.yml", workflow_name="smc-assay")
    # Orca.run_method(config_file="examples\\smc_assay\\smc_assay_example.yml", 
    #          method_name="incubate-2hrs",
    #          start_map_json=json.dumps({"plate-1": "pad_1"}),
    #          end_map_json=json.dumps({"plate-1": "pad_3"}))
    # Orca.run_method(config_file="examples\\smc_assay\\smc_assay_example.yml",
    #                 method_name="add-detection-antibody",
    #                 start_map_json=json.dumps({"plate-1": "pad_1", "tips-96": "pad_3"}),
    #                 end_map_json=json.dumps({"plate-1": "pad_6", "tips-96": "pad_2"}),
    #                 options={"stage": "prod"})
    Orca.run_workflow(config_file="examples\\smc_assay\\smc_assay_example.yml", 
                      workflow_name="smc-assay", 
                      options={"stage": "prod"})

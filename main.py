import argparse
from typing import Optional
from workflow_executor import WorkflowExecuter
from yml_config_builder.template_builders import ConfigFile




class Orca:
    @staticmethod
    def run(config_file: str, workflow_name: Optional[str] = None):
        config = ConfigFile(config_file)
        system_template = config.get_system_template()
        system = system_template.create_system_instance()
        if workflow_name is None:
            raise ValueError("workflow is None.  Workflow must be a string")
        if workflow_name not in system_template.workflows.keys():
            raise LookupError(f"Workflow {workflow_name} is not defined with then System.  Make sure it is included in the config file and the config file loaded correctly.")
       
        workflow = system.workflows[workflow_name]
        executer = WorkflowExecuter(workflow, system.system_graph)
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

    # INIT
    init_parser = subparsers.add_parser("init", help="Initialize the lab instruments")
    init_parser.add_argument("--config", help="Configuration file")

    # CHECK
    check_parser = subparsers.add_parser("check", help="Check syntax errors within the configuration")
    check_parser.add_argument("--config", help="Configuration file")
    run_parser.add_argument("--method", help="Method to check")

    args = parser.parse_args()
    if args.subcommand == "run":
        try:
            Orca.run(config_file=args.config, workflow_name=args.workflow)
        except ValueError as ve:
            print(f"Error: {ve}")
        except LookupError as le:
            print(f"Error: {le}")


if __name__ == '__main__':
    # Orca.run(config_file="tests\\mock_config1.yml", workflow="test-workflow2")
    Orca.run(config_file="examples\\smc_assay\\smc_assay_example.yml", workflow_name="smc-assay")
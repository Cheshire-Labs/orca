import time
import threading
import asyncio
import logging
from logging import Handler, LogRecord
from typing import Any, Dict, List

from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException
import uvicorn

from orca.cli.socketio_mount import socketio_mount
from orca.cli.orca_api import OrcaApi
from orca.logger.socketio_logger_handler import SocketIOHandler
from orca.resource_models.labware import AnyLabwareTemplate, LabwareTemplate

orca_logger = logging.getLogger("orca")
app = FastAPI()
sio = socketio_mount(app)
socketio_handler = SocketIOHandler(sio)
if not any(isinstance(h, type(socketio_handler)) for h in orca_logger.handlers):
    orca_logger.addHandler(socketio_handler)
    orca_logger.setLevel(logging.DEBUG)

orca_api: OrcaApi = OrcaApi()


@sio.on("connect", namespace="/logging")  # type: ignore
async def handle_connect(sid, environ) -> None:
    print(f"Client connected: {sid}")

@sio.on("disconnect", namespace="/logging")  # type: ignore
async def handle_disconnect(sid) -> None:
    print(f"Client disconnected: {sid}")




# REST API endpoints
@app.post("/load")
async def load(data: Dict[str, Any]) -> Dict[str, str]:
    config_file = data.get("configFile")
    orca_logger.info(f"Configuration loaded from {config_file}")
    if config_file is None:
        raise HTTPException(status_code=400, detail="Config file is required.")
    orca_api.load(config_file)
    return {"message": "Configuration loaded successfully."}



@app.post("/init")
async def init(data: Dict[str, Any]):
    config_file = data.get("configFile")
    resource_list = data.get("resourceList", [])
    stage = data.get("stage", None)
    orca_api.init(config_file, resource_list, stage)
    return {"message": "Initialization complete."}


@app.post("/run_workflow")
async def run_workflow(data: Dict[str, Any]) -> Dict[str, Any]:
    workflow_name = data.get("workflowName")
    if workflow_name is None:
        raise HTTPException(status_code=400, detail="Workflow name is required.")
    config_file = data.get("configFile", None)
    stage = data.get("stage", None)
    workflow_id = orca_api.run_workflow(
        workflow_name, config_file, stage
    )
    orca_logger.info(f"Workflow {workflow_name} started with ID {workflow_id}")
    return {"workflowId": workflow_id}


@app.post("/run_method")
async def run_method(data: Dict[str, Any]) -> Dict[str, Any]:
    method_name = data.get("methodName")
    if method_name is None:
        raise HTTPException(status_code=400, detail="Method name is required.")
    start_map = data.get("startMap", {})
    end_map = data.get("endMap", {})
    config_file = data.get("configFile", None)
    stage = data.get("stage", None)
    method_id = orca_api.run_method(
        method_name,
        start_map,
        end_map,
        config_file,
        stage
    )
    orca_logger.info(f"Method {method_name} started with ID {method_id}")
    return {"methodId": method_id}


@app.get("/get_deployment_stages")
async def get_deployment_stages() -> Dict[str, Any]:
    deployment_stages = orca_api.get_deployment_stages()
    return {"deploymentStages": deployment_stages}

@app.get("/get_workflow_recipes")
async def get_workflow_recipes() -> Dict[str, Any]:
    recipes = orca_api.get_workflow_recipes()
    dict_recipes = {}
    for name, r in recipes.items():
        thread_recipes = {}
        for tr in r.thread_templates:
            thread_recipes[tr.name] = {
                "name": tr.name,
                "startLocation": tr.start_location.name,
                "endLocation": tr.end_location.name,
                "labwareTemplate": tr.labware_template.name,
            }
        dict_recipes[name] = {
            "name": r.name,
            "threadRecipes": thread_recipes,
        }

    return {"workflowRecipes": dict_recipes}


@app.get("/test")
async def test() -> Dict[str, str]:
    return {"status": "route reachable"}


@app.get("/get_method_recipes")
async def get_method_recipes() -> Dict[str, Any]:
    recipes = orca_api.get_method_recipes()
    dict_recipes = {}
    for name, r in recipes.items():
        dict_recipes[name] = {
            "name": r.name,
            "inputs": [labware.name for labware in r.inputs],
            "outputs": [labware.name for labware in r.outputs],
        }
    return {"methodRecipes": dict_recipes}


@app.get("/get_running_workflows")
async def get_running_workflows() -> Dict[str, Any]:
    running_workflows = orca_api.get_running_workflows()
    return {"workflows": running_workflows}


@app.get("/get_running_methods")
async def get_running_methods() -> Dict[str, Any]:
    running_methods = orca_api.get_running_methods()
    return {"methods": running_methods}


@app.get("/get_method_recipe_input_labwares/{method_name}")
def get_method_recipe_input_labwares(method_name: str) -> Dict[str, Any]:
    method_recipe = orca_api.get_method_recipes()[method_name]
    labware_inputs: List[str] = []
    any_count: int = 0
    for labware in method_recipe.inputs:
        if isinstance(labware, AnyLabwareTemplate):
            labware_inputs.append(f"$ANY_{any_count}")
            any_count += 1
        elif isinstance(labware, LabwareTemplate):
            labware_inputs.append(labware.name)
        else:
            raise TypeError(
                f"Labware {labware} is not a recognized labware template type"
            )
    return {"inputLabwares": labware_inputs}


@app.get("/get_method_recipe_output_labwares/{method_name}")
def get_method_recipe_output_labwares(method_name: str) -> Dict[str, Any]:
    method_recipe = orca_api.get_method_recipes()[method_name]
    labware_outputs: List[str] = []
    any_count: int = 0
    for labware in method_recipe.outputs:
        if isinstance(labware, AnyLabwareTemplate):
            labware_outputs.append(f"$ANY_{any_count}")
            any_count += 1
        elif isinstance(labware, LabwareTemplate):
            labware_outputs.append(labware.name)
        else:
            raise TypeError(
                f"Labware {labware} is not a recognized labware template type"
            )
    return {"outputLabwares": labware_outputs}


@app.get("/get_labware_recipes")
def get_labware_recipes() -> Dict[str, Any]:
    recipes = orca_api.get_labware_recipes()
    return {"labwareRecipes": recipes}


@app.get("/get_locations")
def get_locations() -> Dict[str, Any]:
    locations = orca_api.get_locations()
    return {"locations": locations}


@app.get("/get_resources")
def get_resources() -> Dict[str, Any]:
    resources = orca_api.get_resources()
    return {"resources": resources}


@app.get("/get_equipments")
def get_equipments() -> Dict[str, Any]:
    equipments = orca_api.get_equipments()
    return {"equipments": equipments}


@app.get("/get_transporters")
def get_transporters() -> Dict[str, Any]:
    transporters = orca_api.get_transporters()
    return {"transporters": transporters}


@app.post("/stop")
async def stop() -> Dict[str, str]:
    orca_api.stop()
    return {"message": "Orca stopped."}


@app.get("/get_installed_drivers_info")
async def get_installed_drivers_info() -> Dict[str, Any]:
    driver_info = orca_api.get_installed_drivers_info()
    drivers = {name: info.model_dump_json() for name, info in driver_info.items()}
    return {"installedDriversInfo": drivers}


@app.get("/get_available_drivers_info")
async def get_available_drivers_info() -> Dict[str, Any]:
    driver_info = orca_api.get_available_drivers_info()
    drivers = {name: info.model_dump_json() for name, info in driver_info.items()}
    return {"availableDriversInfo": drivers}


@app.post("/install_driver/")
async def install_driver(data: Dict[str, Any]) -> Dict[str, str]:
    driver_name = data.get("driverName", None)
    driver_repo_url = data.get("driverRepoUrl", None)
    if driver_name is None:
        raise HTTPException(status_code=400, detail="Driver name is required.")
    orca_api.install_driver(driver_name, driver_repo_url)
    return {"message": f"Driver '{driver_name}' installed successfully."}


@app.post("/uninstall_driver")
async def uninstall_driver(data: Dict[str, Any]) -> Dict[str, str]:
    driver_name = data.get("driverName", None)
    if driver_name is None:
        raise HTTPException(status_code=400, detail="Driver name is required.")
    orca_api.uninstall_driver(driver_name)
    return {"message": f"Driver '{driver_name}' uninstalled successfully."}


@app.get("/shutdown")
async def shutdown() -> Dict[str, str]:
    """API route to shut down the server."""
    try:
        response = {"message": "Server shutdown: success"}

        orca_logger.info("Shutdown request received, shutting down Orca server")
        loop = asyncio.get_running_loop()
        loop.call_later(1, uvicorn_server.stop)

        return response
    except Exception as e:
        orca_logger.error(f"Error sending shutdown response: {e}")
        return {"message": "Server shutdown: failed"}


# Uvicorn server instance for graceful shutdown
class UvicornServer(uvicorn.Server):
    def install_signal_handlers(self):
        pass

    def __init__(self, config: uvicorn.Config):
        super().__init__(config)
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.run)
        self.thread.start()

        # Wait for server to start
        while not self.started:
            time.sleep(1e-3)

        return self.thread

    def stop(self):
        self.should_exit = True
        if self.thread:
            self.thread.should_abort_immediately = True  # type: ignore
            self.thread = None


uvicorn_server = UvicornServer(
    config=uvicorn.Config(
        app=app, host="127.0.0.1", port=5000, log_level="debug", loop="asyncio"
    )
)

if __name__ == "__main__":
    uvicorn_server.run()

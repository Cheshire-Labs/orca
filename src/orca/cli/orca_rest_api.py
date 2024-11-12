import logging
from logging import Handler, LogRecord
from typing import Any, Dict, List, cast

import socketio
from fastapi import FastAPI, HTTPException
from fastapi_socketio import SocketManager
import asyncio

from orca.cli.orca_api import  OrcaApi
from orca.resource_models.labware import AnyLabwareTemplate, LabwareTemplate
import uvicorn

orca_api: OrcaApi = OrcaApi()

def socketio_mount(
    app: FastAPI,
    async_mode: str = "asgi",
    mount_path: str = "/socket.io/",
    socketio_path: str = "socket.io",
    logger: bool = False,
    engineio_logger: bool = False,
    cors_allowed_origins="*",
    **kwargs
) -> socketio.AsyncServer:
    """Mounts an async SocketIO app over an FastAPI app."""

    sio = socketio.AsyncServer(async_mode=async_mode,
                      cors_allowed_origins=cors_allowed_origins,
                      logger=logger,
                      engineio_logger=engineio_logger, **kwargs)

    sio_app = socketio.ASGIApp(sio, socketio_path=socketio_path)

    # mount
    app.add_route(mount_path, route=sio_app, methods=["GET", "POST"]) # type: ignore
    app.add_websocket_route(mount_path, sio_app) # type: ignore

    return sio

# sio: Any = socketio.AsyncServer(async_mode="asgi")
# socket_app = socketio.ASGIApp(sio)
app = FastAPI()
sio = socketio_mount(app)

# socket_manager = SocketManager(app=app, cors_allowed_origins="*", logger=True, engineio_logger=True)

class SocketIOHandler(Handler):
    """A logging handler that emits records via SocketIO."""

    async def emit(self, record: LogRecord) -> None:
        """Emit a log record via SocketIO."""
        try:
            await sio.emit('logMessage', {'data': self.format(record)})
        except Exception as e:
            print(f"Error sending log message: {e}")



@sio.on('connect', namespace='/logging') # type: ignore
async def handle_connect(sid, environ) -> None:
    print(f"Client connected: {sid}")
    # handler = SocketIOHandler()
    # orca_api.set_logging_destination(handler, "INFO")
    # await socket_manager.emit('logMessage', {'data': 'Logging connected to Orca server'}, namespace='/logging')

@sio.on('message', namespace='/logging') # type: ignore
async def handle_test(sid, data) -> None:
    await sio.emit('logMessage', {'data': 'Test response'}, namespace='/logging', room=sid)

@sio.on('disconnect', namespace='/logging') # type: ignore
async def handle_disconnect(sid) -> None:
    print(f"Client disconnected: {sid}")



# REST API endpoints
@app.post("/load")
async def load(data: Dict[str, Any]) -> Dict[str, str]:
    config_file = data.get("configFile")
    if config_file is None:
        raise HTTPException(status_code=400, detail="Config file is required.")
    orca_api.load(config_file)
    return {"message": "Configuration loaded successfully."}

@app.post("/init")
async def init(data: Dict[str, Any]):
    config_file = data.get("configFile")
    resource_list = data.get("resourceList", [])
    options = data.get("options", {})
    orca_api.init(config_file=config_file, resource_list=resource_list, options=options)
    return {"message": "Initialization complete."}

@app.post("/run_workflow")
async def run_workflow(data: Dict[str, Any]) -> Dict[str, Any]:
    workflow_name = data.get("workflowName")
    if workflow_name is None:
        raise HTTPException(status_code=400, detail="Workflow name is required.")
    config_file = data.get("configFile", None)
    options = data.get("options", {})
    workflow_id = orca_api.run_workflow(workflow_name=workflow_name, 
                                        config_file=config_file, 
                                        options=options)
    return {"workflowId": workflow_id}

@app.post("/run_method")
async def run_method(data: Dict[str, Any]) -> Dict[str, Any]:
    method_name = data.get("methodName")
    if method_name is None:
        raise HTTPException(status_code=400, detail="Method name is required.")
    start_map_json = data.get("startMap", {})
    end_map_json = data.get("endMap", {})
    config_file = data.get("configFile", None)
    options = data.get("options", {})
    method_id = orca_api.run_method(method_name=method_name, 
                                    start_map_json=start_map_json, 
                                    end_map_json=end_map_json,
                                    config_file=config_file, 
                                    options=options)
    return {"methodId": method_id}


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
                "labwareTemplate": tr.labware_template.name
            }
        dict_recipes[name] = {
            "name": r.name,
            "threadRecipes": thread_recipes,
        }

    return {"workflowRecipes": dict_recipes}

@app.get("/test")
async def test() -> Dict[str, str]:
    logging.info("Test pinged")
    return {"status": "route reachable"}

@app.get("/get_method_recipes")
async def get_method_recipes() -> Dict[str, Any]:
    recipes = orca_api.get_method_recipes()
    dict_recipes = {}
    for name, r in recipes.items():
        dict_recipes[name] = {
            "name": r.name,
            "inputs": [labware.name for labware in r.inputs],
            "outputs": [labware.name for labware in r.outputs]
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


@app.get('/get_method_recipe_input_labwares')
def get_method_recipe_input_labwares(data: Dict[str, Any]) -> Dict[str, Any]:
    method_name = data.get('methodName')
    if not method_name:
        raise HTTPException(400, 'Method name is required')
    method_recipe = orca_api.get_method_recipes()[method_name]
    labware_inputs: List[str] =  []
    any_count: int = 0
    for labware in method_recipe.inputs:
        if isinstance(labware, AnyLabwareTemplate):
            labware_inputs.append(f"$ANY_{any_count}")
            any_count += 1
        elif isinstance(labware, LabwareTemplate):
            labware_inputs.append(labware.name)
        else:
            raise TypeError(f"Labware {labware} is not a recognized labware template type")
    return {"inputLabwares": labware_inputs}

@app.get('/get_method_recipe_output_labwares')
def get_method_recipe_output_labwares(data: Dict[str, Any]) -> Dict[str, Any]:
    method_name = data.get('methodName')
    if not method_name:
        raise HTTPException(status_code=400, detail="Method name is required.")
    method_recipe = orca_api.get_method_recipes()[method_name]
    labware_outputs: List[str] =  []
    any_count: int = 0
    for labware in method_recipe.outputs:
        if isinstance(labware, AnyLabwareTemplate):
            labware_outputs.append(f"$ANY_{any_count}")
            any_count += 1
        elif isinstance(labware, LabwareTemplate):
            labware_outputs.append(labware.name)
        else:
            raise TypeError(f"Labware {labware} is not a recognized labware template type")
    return {"outputLabwares": labware_outputs}

@app.get('/get_labware_recipes')
def get_labware_recipes() -> Dict[str, Any]:
    recipes = orca_api.get_labware_recipes()
    return {"labwareRecipes": recipes}



@app.get('/get_locations')
def get_locations() -> Dict[str, Any]:
    locations = orca_api.get_locations()
    return {"locations": locations}

@app.get('/get_resources')
def get_resources() -> Dict[str, Any]:
    resources = orca_api.get_resources()
    return {"resources": resources}

@app.get('/get_equipments')
def get_equipments() -> Dict[str, Any]:
    equipments = orca_api.get_equipments()
    return {"equipments": equipments}

@app.get('/get_transporters')
def get_transporters() -> Dict[str, Any]:
    transporters = orca_api.get_transporters()
    return {"transporters": transporters}

@app.post("/stop")
async def stop() -> Dict[str, str]:
    orca_api.stop()
    return {"message": "Orca stopped."}

@app.get("/get_installed_drivers_info")
async def get_installed_drivers_info() -> Dict[str, Any]:
    drivers = orca_api.get_installed_drivers_info()
    return {"installedDriversInfo": drivers}

@app.get("/get_available_drivers_info")
async def get_available_drivers_info() -> Dict[str, Any]:
    drivers = orca_api.get_available_drivers_info()
    return {"availableDriversInfo": drivers}

@app.post("/install_driver")
async def install_driver(data: Dict[str, Any]) -> Dict[str, str]:
    driver_name = data.get("driverName")
    if driver_name is None:
        raise HTTPException(status_code=400, detail="Driver name is required.")
    driver_repo_url = data.get("driverRepoUrl")
    orca_api.install_driver(driver_name, driver_repo_url)
    return {"message": f"Driver '{driver_name}' installed successfully."}

@app.post("/uninstall_driver")
async def uninstall_driver(data: Dict[str, Any]) -> Dict[str, str]:
    driver_name = data.get("driverName")
    if driver_name is None:
        raise HTTPException(status_code=400, detail="Driver name is required.")
    orca_api.uninstall_driver(driver_name)
    return {"message": f"Driver '{driver_name}' uninstalled successfully."}

@app.get("/shutdown")
async def shutdown() -> Dict[str, str]:
    """API route to shut down the server."""
    logging.info("Shutdown request received")
    asyncio.create_task(stop_server())
    return {"message": "Server shutdown initiated"}

async def stop_server() -> None:
    """Triggers the server shutdown."""
    logging.info("Shutting down Orca server")
    uvicorn_server.should_exit = True

# Custom logging setup
def setup_logging() -> None:
    logging.basicConfig(level=logging.DEBUG)
    logging.info("Logging setup complete")

# Uvicorn server instance for graceful shutdown
# uvicorn_server = uvicorn.Server(
#     config=uvicorn.Config(app, host="127.0.0.1", port=5000)
# )
# @app.on_event("startup")
# async def startup_event():
#     logger = logging.getLogger("uvicorn.access")
#     handler = logging.StreamHandler()
#     handler.setLevel(logging.DEBUG)
#     handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
#     logger.addHandler(handler)

if __name__ == "__main__":
    setup_logging()
    uvicorn.run(app, host="127.0.0.1", port=5000)
    
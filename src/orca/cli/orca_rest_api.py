import time
import threading
import asyncio
import logging
from logging import Handler, LogRecord
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException
import socketio  # type: ignore
import uvicorn

from orca.cli.socketio_mount import socketio_mount
from orca.cli.orca_api import OrcaApi
from orca.resource_models.labware import AnyLabwareTemplate, LabwareTemplate


orca_api: OrcaApi = OrcaApi()


# sio: Any = socketio.AsyncServer(async_mode="asgi")
# socket_app = socketio.ASGIApp(sio)
app = FastAPI()
sio = socketio_mount(app)

# socket_manager = SocketManager(app=app, cors_allowed_origins="*", logger=True, engineio_logger=True)


class SocketIOHandler(Handler):
    """A logging handler that emits records via Socket.IO."""

    def __init__(self, sio: socketio.AsyncServer):
        super().__init__()
        self.sio = sio
        self.loop: asyncio.AbstractEventLoop | None = None

    def emit(self, record: LogRecord) -> None:
        """Emit a log record via Socket.IO."""

        try:
            message = {"data": self.format(record)}
            # Use Socket.IO's built-in background task function
            print(f"Sending log message: {message}")
            if self.loop is None:
                self.loop = asyncio.get_running_loop()
            asyncio.run_coroutine_threadsafe(self._send_log_message(message), self.loop)
            # self.sio.start_background_task(self._send_log_message, message)
        except Exception as e:
            print(f"Error sending log message: {e}")

    async def _send_log_message(self, message: dict) -> None:
        """Coroutine to send log message via Socket.IO."""
        try:
            await self.sio.emit("logMessage", message, namespace="/logging")
        except Exception as e:
            print(f"Failed to emit log message: {e}")


async def send_response(message: str) -> Dict[str, str]:
    try:
        await sio.emit("logMessage", {"data": message}, namespace="/logging")
        return {"message": message}
    except Exception as e:
        print(f"Error sending response: {e}")
        return {"message": message}  # Still return even if Socket.IO fails


# class SocketIOHandler(Handler):
#     """A logging handler that emits records via SocketIO."""
#     def __init__(self, sio: socketio.AsyncServer):
#         super().__init__()
#         self._sio = sio
#         self.loop = asyncio.new_event_loop()
#         # self.loop = asyncio.get_event_loop()

#     def emit(self, record):
#         self.format(record)
#         self.loop.create_task( self._async_emit(record.message))

#     async def _async_emit(self, message):
#         await self._sio.emit('logMessage', {'data': message}, namespace='/logging') # await my_async_write_function(message)

#     def close(self) -> None:
#         self.loop.run_until_complete(asyncio.gather(*asyncio.all_tasks(self.loop)))
#         self.loop.close()


socketio_handler = SocketIOHandler(sio)


@sio.on("connect", namespace="/logging")  # type: ignore
async def handle_connect(sid, environ) -> None:
    print(f"Client connected: {sid}")
    # handler = SocketIOHandler()
    orca_api.set_logging_destination(socketio_handler, "INFO")
    logging.info("Logging connected to Orca server")
    # await socket_manager.emit('logMessage', {'data': 'Logging connected to Orca server'}, namespace='/logging')


@sio.on("message", namespace="/logging")  # type: ignore
async def handle_test(sid, data) -> None:
    await sio.emit("logMessage", {"data": "Test response"}, namespace="/logging")


@sio.on("disconnect", namespace="/logging")  # type: ignore
async def handle_disconnect(sid) -> None:
    print(f"Client disconnected: {sid}")


# REST API endpoints
@app.post("/load")
async def load(data: Dict[str, Any]) -> Dict[str, str]:
    config_file = data.get("configFile")
    if config_file is None:
        raise HTTPException(status_code=400, detail="Config file is required.")
    orca_api.load(config_file)
    return await send_response("Configuration loaded successfully.")


@app.post("/init")
async def init(data: Dict[str, Any]):
    config_file = data.get("configFile")
    resource_list = data.get("resourceList", [])
    options = data.get("options", {})
    orca_api.init(config_file=config_file, resource_list=resource_list, options=options)
    return await send_response("Initialization complete.")


@app.post("/run_workflow")
async def run_workflow(data: Dict[str, Any]) -> Dict[str, Any]:
    workflow_name = data.get("workflowName")
    if workflow_name is None:
        raise HTTPException(status_code=400, detail="Workflow name is required.")
    config_file = data.get("configFile", None)
    options = data.get("options", {})
    workflow_id = orca_api.run_workflow(
        workflow_name=workflow_name, config_file=config_file, options=options
    )
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
    method_id = orca_api.run_method(
        method_name=method_name,
        start_map_json=start_map_json,
        end_map_json=end_map_json,
        config_file=config_file,
        options=options,
    )
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
    return await send_response("Orca stopped.")


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
    return await send_response(f"Driver '{driver_name}' installed successfully.")


@app.post("/uninstall_driver")
async def uninstall_driver(data: Dict[str, Any]) -> Dict[str, str]:
    driver_name = data.get("driverName", None)
    if driver_name is None:
        raise HTTPException(status_code=400, detail="Driver name is required.")
    orca_api.uninstall_driver(driver_name)
    return await send_response(f"Driver '{driver_name}' uninstalled successfully.")


@app.get("/shutdown")
async def shutdown() -> Dict[str, str]:
    """API route to shut down the server."""
    try:
        response = await send_response("Server shutdown: success")
        logging.info("Shutdown request received, shutting down Orca server")
        loop = asyncio.get_running_loop()
        # Schedule the server shutdown
        loop.call_later(2, uvicorn_server.stop)
        return response
    except Exception as e:
        logging.error(f"Error sending shutdown response: {e}")
        return await send_response("Server shutdown: failed")


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


# Custom logging setup
# @app.on_event("startup")
# async def startup_event():
#     logger = logging.getLogger("uvicorn.access")
#     handler = logging.StreamHandler()
#     handler.setLevel(logging.DEBUG)
#     handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
#     logger.addHandler(handler)
def setup_logging() -> None:
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)

    # Clear existing handlers to avoid duplicates
    if not any(isinstance(h, type(socketio_handler)) for h in logger.handlers):
        logger.handlers = []  # Remove all existing handlers
        logger.addHandler(socketio_handler)
        logger.addHandler(logging.StreamHandler())
        print("SocketIOHandler registered with the logger")


if __name__ == "__main__":
    setup_logging()
    uvicorn_server.start()

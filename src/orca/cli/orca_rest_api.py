import logging
from logging import Handler, LogRecord
from typing import List

from flask_socketio import SocketIO
from flask import Flask, jsonify, request, Response
from flask.helpers import abort
from orca.cli.orca_api import  OrcaApi
from orca.resource_models.labware import AnyLabwareTemplate, LabwareTemplate

orca_api = OrcaApi()

app = Flask(__name__)

socketio = SocketIO(app, async_mode='eventlet')


class SocketIOHandler(Handler):
    """A logging handler that emits records with SocketIO."""
    
    # _instance = None
    # _lock = threading.Lock()  # To ensure thread-safety

    # def __new__(cls, *args, **kwargs):
    #     # Use double-checked locking to create a singleton instance
    #     if not cls._instance:
    #         with cls._lock:
    #             if not cls._instance:
    #                 cls._instance = super(SocketIOHandler, cls).__new__(cls)
    #     return cls._instance

    # def __init__(self):
    #     # Ensure __init__ is only called once
    #     if not hasattr(self, '_initialized'):
    #         super().__init__()
    #         self._initialized = True

    

    def emit(self, record: LogRecord) -> None:
        """Emit a log record with SocketIO."""
        socketio.emit('logMessage', {'data': self.format(record)}, namespace='/logging')


# WebSocket event to send logs
@socketio.on('connect', '/logging')
def handle_connect():
    print("Client connected")
    # socketio_logging: Handler = SocketIOHandler()
    # orca_api.set_logging_destination(socketio_logging, "INFO")
    # socketio.emit('logMessage', {'data': 'Socket Logging Connected to Orca Server'})

@socketio.on('disconnect', '/logging')
def handle_disconnect():
    print("Client disconnected")

@app.route('/load', methods=['POST'])
def load() -> Response:
    data = request.get_json()
    config_file = data.get("configFile")
    orca_api.load(config_file)
    return jsonify({"message": "Configuration loaded successfully."})

@app.route('/init', methods=['POST'])
def init() -> Response:
    data = request.get_json()
    config_file = data.get("configFile")
    resource_list = data.get("resourceList", [])
    options = data.get("options", {})
    orca_api.init(config_file=config_file, resource_list=resource_list, options=options)
    return jsonify({"message": "Initialization complete."})

@app.route('/run_workflow', methods=['POST'])
def run_workflow() -> Response:
    data = request.get_json()
    workflow_name = data.get("workflowName")
    config_file = data.get("configFile", None)
    options = data.get("options", {})
    workflow_id = orca_api.run_workflow(workflow_name=workflow_name, 
                                        config_file=config_file, 
                                        options=options)
    return jsonify({"workflowId": workflow_id})

@app.route('/run_method', methods=['POST'])
def run_method() -> Response:
    data = request.get_json()
    method_name = data.get("methodName")
    start_map_json = data.get("startMap")
    end_map_json = data.get("endMap")
    config_file = data.get("configFile", None)
    options = data.get("options", {})
    method_id = orca_api.run_method(method_name=method_name, 
                                    start_map_json=start_map_json, 
                                    end_map_json=end_map_json,
                                    config_file=config_file, 
                                    options=options)
    return jsonify({"methodId": method_id})

@app.route('/get_workflow_recipes', methods=['GET'])
def get_workflow_recipes() -> Response:
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

    return jsonify({"workflowRecipes": dict_recipes})

@app.route('/test', methods=['GET'])
def test() -> Response:
    logging.info("Test pinged")
    return jsonify({"status": "route reachable"})

@app.route('/get_method_recipes', methods=['GET'])
def get_method_recipes() -> Response:
    recipes = orca_api.get_method_recipes()
    dict_recipes = {}
    for name, r in recipes.items():
        dict_recipes[name] = {
            "name": r.name,
            "inputs": [labware.name for labware in r.inputs],
            "outputs": [labware.name for labware in r.outputs]
        }
    return jsonify({"methodRecipes": dict_recipes})


@app.route('/get_method_recipe_input_labwares', methods=['GET'])
def get_method_recipe_input_labwares() -> Response:
    method_name = request.args.get('methodName')
    if not method_name:
        abort(400, 'Method name is required')
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
    return jsonify({"inputLabwares": labware_inputs})

@app.route('/get_method_recipe_output_labwares', methods=['GET'])
def get_method_recipe_output_labwares() -> Response:
    method_name = request.args.get('methodName')
    if not method_name:
        abort(400, 'Method name is required')
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
    return jsonify({"outputLabwares": labware_outputs})

@app.route('/get_labware_recipes', methods=['GET'])
def get_labware_recipes() -> Response:
    recipes = orca_api.get_labware_recipes()
    return jsonify({"labwareRecipes": recipes})

@app.route('/get_running_workflows', methods=['GET'])
def get_running_workflows() -> Response:
    running_workflows = orca_api.get_running_workflows()
    return jsonify({"workflows": running_workflows})

@app.route('/get_running_methods', methods=['GET'])
def get_running_methods() -> Response:
    running_methods = orca_api.get_running_methods()
    return jsonify({"methods": running_methods})

@app.route('/get_locations', methods=['GET'])
def get_locations() -> Response:
    locations = orca_api.get_locations()
    return jsonify({"locations": locations})

@app.route('/get_resources', methods=['GET'])
def get_resources() -> Response:
    resources = orca_api.get_resources()
    return jsonify({"resources": resources})

@app.route('/get_equipments', methods=['GET'])
def get_equipments() -> Response:
    equipments = orca_api.get_equipments()
    return jsonify({"equipments": equipments})

@app.route('/get_transporters', methods=['GET'])
def get_transporters() -> Response:
    transporters = orca_api.get_transporters()
    return jsonify({"transporters": transporters})

@app.route('/stop', methods=['POST'])
def stop() -> Response:
    orca_api.stop()
    return jsonify({"message": "Orca stopped."})

@app.route('/get_installed_drivers_info', methods=['GET'])
def get_installed_drivers_info() -> Response:
    drivers = orca_api.get_installed_drivers_info()
    return jsonify({"installedDriversInfo": drivers})

@app.route('/get_available_drivers_info', methods=['GET'])
def get_available_drivers_info() -> Response:
    drivers = orca_api.get_available_drivers_info()
    return jsonify({"availableDriversInfo": drivers})

@app.route('/install_driver', methods=['POST'])
def install_driver() -> Response:
    data = request.get_json()
    driver_name = data.get("driverName")
    driver_repo_url = data.get("driverRepoUrl")
    orca_api.install_driver(driver_name, driver_repo_url)
    return jsonify({"message": f"Driver '{driver_name}' installed successfully."})

@app.route('/uninstall_driver', methods=['POST'])
def uninstall_driver() -> Response:
    data = request.get_json()
    driver_name = data.get("driverName")
    orca_api.uninstall_driver(driver_name)
    return jsonify({"message": f"Driver '{driver_name}' uninstalled successfully."})

def shutdown_server():
    func = request.environ.get('werkzeug.server.shutdown')
    if func is None:
        raise RuntimeError('Not running with the Werkzeug Server')
    func()

@app.route('/shutdown', methods=['GET'])
def shutdown():
    socketio.stop()
    return 'Server shut down'

if __name__ == "__main__":
    # app.run(host="127.0.0.1", port=5000, debug=True)
    logging.basicConfig(handlers=[logging.StreamHandler(), SocketIOHandler()], level=logging.DEBUG)
    socketio.run(app, host="127.0.0.1", port=5000, debug=True, allow_unsafe_werkzeug=True)
    # asyncio.run(socketio.run(logging_app, host="127.0.0.1", port=5001, debug=True, allow_unsafe_werkzeug=True))
    
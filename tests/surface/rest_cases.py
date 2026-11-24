"""One or more cases per daemon route, run in order against one daemon.

A case names the route as OpenAPI spells it, the request it sends, the status it expects, and a key the
answer must carry (or, for a refusal, the error code). A route whose cases are all refusals is listed in
`REFUSAL_ONLY` with the reason success cannot be produced in simulation.
"""

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import JsonValue

from tests.surface.context import COORDS, Context, as_dict, as_list, plate_geometry
from tests.surface.workflow_sources import SHAKE_ACTION

Body = JsonValue | Callable[[Context], JsonValue]
Query = dict[str, str | int]


@dataclass(frozen=True)
class RestCase:
    method: str
    template: str
    status: int
    expect: str
    path: str = ""
    body: Body = None
    params: Query | None = None
    world: str = "lab"
    before: Callable[[Context], None] | None = None
    after: Callable[[Context, JsonValue], None] | None = None
    contains: str = ""

    @property
    def key(self) -> str:
        return f"{self.method} {self.template}"

    @property
    def case_id(self) -> str:
        return f"{self.key} -> {self.status}" + (f" {self.path}" if self.path else "")


R = RestCase
E = "/api/executions/"
L = "/api/labware-instances/"


def _note(name: str) -> Callable[[Context, JsonValue], None]:
    def store(c: Context, body: JsonValue) -> None:
        c.notes[name] = str(as_dict(body)[name])
    return store


def note_labware(id_key: str, name_key: str) -> Callable[[Context, JsonValue], None]:
    def store(c: Context, body: JsonValue) -> None:
        labware = as_dict(as_dict(body)["labware"])
        c.notes[id_key] = str(labware["id"])
        c.notes[name_key] = str(labware["name"])
    return store


def park_all_incidents(c: Context, body: JsonValue) -> None:
    for key in ("inc_listed", "inc_extend", "inc_abort", "inc_done"):
        c.notes[key] = c.world.park_timeout()


def park_incident(name: str) -> Callable[[Context, JsonValue], None]:
    def store(c: Context, body: JsonValue) -> None:
        c.notes[name] = c.world.park_timeout()
    return store


def _manual_step(c: Context, body: JsonValue) -> None:
    c.notes["manual_eid"] = str(as_dict(body)["execution_id"])
    c.notes["step_id"] = c.wait_for_manual_step()


def _scope(c: Context, kind: str) -> JsonValue:
    scope: dict[str, JsonValue] = {"kind": kind, "execution_id": c.notes["e2"]}
    if kind == "thread":
        scope["thread_id"] = c.notes["t2"]
    return scope


def _tp(position: str) -> JsonValue:
    return {"position_id": position, "coord_type": "cartesian", "coords": COORDS, "orientation": "right"}


def _geometry_body(name: str, display: str, *, named: bool = True) -> Callable[[Context], JsonValue]:
    def body(c: Context) -> JsonValue:
        entry: dict[str, JsonValue] = {"display_name": display, "category": "plate", "geometry": plate_geometry(name)}
        return {"labware_type": name, **entry} if named else entry
    return body


JOINTS: dict[str, JsonValue] = {"rail": 5.0, "base": 10.0, "shoulder": 20.0, "elbow": 30.0, "wrist": 40.0, "gripper": 0.0}
ALL_TIPS: list[JsonValue] = [f"{row}{col}" for col in range(1, 13) for row in "ABCDEFGH"]
T = "/api/transporters/robotic_arm/"
H = "/api/liquid-handlers/liquid_handler/"
HD = "/api/liquid-handlers/{device_id}/"
D = "/api/devices/"
TH = "/api/executions/{execution_id}/threads/{thread_id}/"
TH_AT = E + "{e2}/threads/{t2}/"
CASES: list[RestCase] = [
    # reads that need no run
    R("GET", "/health", 200, "status"),
    R("GET", "/api/runtime/status", 200, "built"),
    R("GET", "/api/topology", 200, "devices"),
    R("GET", "/api/topology", 200, "source", params={"source": "true"}),
    R("GET", "/api/system", 200, "name"),
    R("GET", "/api/workflows", 200, "workflows"),
    R("GET", "/api/methods", 200, "methods"),
    R("GET", "/api/thread-templates", 200, "threads"),
    R("GET", "/api/labware-instances", 200, "labware"),
    R("GET", "/api/executions", 200, "executions"),
    R("GET", "/api/workflows/{name}", 200, "workflow", path="/api/workflows/learn_orca"),
    R("GET", "/api/methods/{name}", 200, "method", path="/api/methods/shake_step"),
    R("GET", "/api/plugins", 200, "[]"),
    R("GET", "/api/devices", 200, "devices"),
    R("GET", "/api/devices/{device_id}", 200, "engine", path="/api/devices/shaker_1"),
    R("GET", "/api/devices/{device_id}/introspection", 200, "capabilities", path="/api/devices/shaker_1/introspection"),
    R("GET", "/api/audit", 200, "[]", params={"limit": 3}),
    R("GET", "/api/events", 200, "[]", params={"limit": 3}),
    R("GET", "/api/interventions/next", 200, "next_cursor", params={"timeout_seconds": 1}),
    R("GET", "/api/unsettled", 200, "unsettled"),
    R("GET", "/api/ops-history", 200, "records", params={"limit": 2}),
    # a finished run
    R("POST", "/api/executions", 200, "execution_id",
      body={"workflow_name": "learn_orca", "run_mode": "SIM", "sim_speedup": 1},
      after=lambda c, b: c.pause_run(str(as_dict(b)["execution_id"]))),
    R("GET", "/api/executions/{execution_id}", 200, "threads", path=E + "{eid}"),
    R("GET", "/api/executions/{execution_id}/threads/{thread_id}", 200, "status",
      path="/api/executions/{eid}/threads/{tid}"),
    R("GET", "/api/submissions", 200, "submissions", params={"execution_id": "{eid}"}),
    R("GET", "/api/submissions/{submission_id}", 200, "submission", path="/api/submissions/{sid}"),
    R("GET", "/api/variables", 200, "[]"),
    R("GET", "/api/reservations", 200, "[]", params={"execution_id": "{eid}"}),
    R("GET", "/api/reservations", 200, "[]"),
    R("GET", "/api/reservations", 404, "execution_not_found", params={"execution_id": "no-such-run"}),
    R("GET", "/api/manual-steps", 200, "pending"),
    R("GET", "/api/labware-instances/{labware_id}/journey", 200, "entries", path=L + "{plate_id}/journey"),
    R("GET", "/api/labware-instances/{labware_id}/contents", 200, "volumes", path=L + "{plate_id}/contents"),
    R("GET", "/api/labware-instances/{labware_id}/contents", 200, "tip_positions_present", path=L + "{rack_id}/contents"),
    R("GET", "/api/incidents", 200, "[]", after=park_all_incidents),
    R("GET", "/api/incidents/{incident_id}", 200, "category", path="/api/incidents/{inc_listed}"),
    R("POST", "/api/incidents/{incident_id}/ack", 200, "acknowledged_count", path="/api/incidents/{inc_listed}/ack",
      body={"reason": "surface suite"}),
    R("POST", "/api/incidents/ack-all", 200, "acknowledged_count", body={"reason": "surface suite"}),
    # recoverable-timeout decisions
    R("POST", "/api/incidents/{incident_id}/recoverable-timeout/extend", 200, "decision",
      path="/api/incidents/{inc_extend}/recoverable-timeout/extend",
      body={"additional_seconds": 5.0, "reason": "surface suite"}),
    R("POST", "/api/incidents/{incident_id}/recoverable-timeout/abort", 200, "decision",
      path="/api/incidents/{inc_abort}/recoverable-timeout/abort",
      body={"operator_name": "suite", "reason": "surface suite"}),
    R("POST", "/api/incidents/{incident_id}/recoverable-timeout/mark-complete", 200, "decision",
      path="/api/incidents/{inc_done}/recoverable-timeout/mark-complete",
      body={"operator_name": "suite", "reason": "surface suite"}),
    # variables on the paused run
    R("PUT", "/api/variables/global/{name}", 200, "status", path="/api/variables/global/rest_g",
      body={"value": 5, "reason": "surface suite"}),
    R("GET", "/api/variables/global/{name}", 200, "value", path="/api/variables/global/rest_g"),
    R("GET", "/api/executions/{execution_id}/variables", 200, "global.rest_g", path=E + "{e2}/variables"),
    R("PUT", "/api/executions/{execution_id}/variables/{name}", 200, "status", path=E + "{e2}/variables/rest_v",
      body={"value": "x", "reason": "surface suite"}),
    R("GET", "/api/executions/{execution_id}/variables/{name}", 200, "overrides", path=E + "{e2}/variables/rest_v"),
    R("GET", "/api/executions/{execution_id}/variables/{name}", 404, "variable_not_found",
      path=E + "{e2}/variables/no_such_variable"),
    R("GET", "/api/executions/{execution_id}/variables/{name}", 404, "execution_not_found",
      path=E + "no-such-run/variables/rest_v"),
    R("PUT", "/api/executions/{execution_id}/submissions/{submission_id}/variables/{name}", 200, "status",
      path=E + "{e2}/submissions/{s2}/variables/rest_s", body={"value": True, "reason": "surface suite"}),
    R("DELETE", "/api/executions/{execution_id}/submissions/{submission_id}/variables/{name}", 200, "existed",
      path=E + "{e2}/submissions/{s2}/variables/rest_s"),
    R("DELETE", "/api/executions/{execution_id}/variables/{name}", 200, "existed", path=E + "{e2}/variables/rest_v"),
    R("DELETE", "/api/variables/global/{name}", 200, "existed", path="/api/variables/global/rest_g"),
    R("PUT", "/api/executions/{execution_id}/sim-speedup", 200, "sim_speedup", path=E + "{e2}/sim-speedup",
      body={"sim_speedup": 3}),
    # controls on the paused run
    R("POST", "/api/pause", 200, "result", body=lambda c: {"scope": _scope(c, "execution"), "reason": "surface suite"}),
    R("GET", "/api/reservations", 200, "[]", params={"execution_id": "{e2}"},
      after=lambda c, b: c.notes.update(reservation=str(as_dict(as_list(b)[0])["reservation_id"]))),
    R("POST", TH + "skip-method", 200, "status", path=TH_AT + "skip-method",
      body={"method_name": "seal_step", "reason": "surface suite"}),
    R("POST", TH + "insert-method", 200, "status", path=TH_AT + "insert-method",
      body={"template_name": "shake_step", "where": "tail", "reason": "surface suite"}),
    R("POST", TH + "replace-method", 200, "execution_id", path=TH_AT + "replace-method",
      body={"target_name": "evaluate_qc_step", "template_name": "shake_step", "reason": "surface suite"}),
    R("POST", TH + "skip-action", 200, "status", path=TH_AT + "skip-action",
      body={"action_command": "read_plate", "reason": "surface suite"}),
    R("POST", TH + "insert-action", 422, "validation_error", path=TH_AT + "insert-action",
      body={"action_code": SHAKE_ACTION, "where": "tail", "reason": "surface suite"}),
    R("POST", TH + "replace-action", 422, "validation_error", path=TH_AT + "replace-action",
      body={"target_command": "shake", "action_code": SHAKE_ACTION, "reason": "surface suite"}),
    R("POST", TH + "recover", 200, "status", path=TH_AT + "recover",
      body={"decision": "abort_method", "reason": "surface suite"}),
    R("POST", "/api/executions/{execution_id}/threads", 200, "status", path=E + "{e2}/threads",
      body={"template_name": "tips", "reason": "surface suite"}),
    R("POST", TH + "recover", 422, "validation_error", path=TH_AT + "recover", body={"decision": "retry"}),
    R("DELETE", "/api/executions/{execution_id}/reservations/{reservation_id}", 200, "blockers",
      path="/api/executions/{e2}/reservations/{reservation}", params={"reason": "surface suite"}),
    R("POST", "/api/resume", 200, "result", body=lambda c: {"scope": _scope(c, "execution")}),
    R("POST", "/api/pause", 200, "result", body=lambda c: {"scope": _scope(c, "thread")}),
    R("POST", "/api/resume", 200, "result", body=lambda c: {"scope": _scope(c, "thread")}),
    R("GET", "/api/executions/{execution_id}/threads/{thread_id}", 200, "status", path=TH_AT.removesuffix("/")),
    R("POST", "/api/executions/{execution_id}/close", 200, "phase", path=E + "{e2}/close"),
    R("POST", "/api/executions/{execution_id}/stop", 200, "status", path=E + "{e2}/stop"),
    R("POST", "/api/executions/{execution_id}/stop", 200, "status", path=E + "{e2}/stop",
      body={"confirm": True, "reason": "surface suite"}),
    R("DELETE", "/api/executions/{execution_id}", 200, "status", path=E + "{e2}", params={"reason": "surface suite"}),
    # setup records
    R("GET", "/api/access-configs", 200, "[]"),
    R("POST", "/api/access-configs", 201, "name", body={"name": "rest_cfg", "access_type": "vertical", "reason": "s"}),
    R("GET", "/api/access-configs/{name}", 200, "access_type", path="/api/access-configs/rest_cfg"),
    R("PUT", "/api/access-configs/{name}", 200, "name", path="/api/access-configs/rest_cfg",
      body={"gripper_offset": 9, "reason": "s"}),
    R("DELETE", "/api/access-configs/{name}", 204, "", path="/api/access-configs/rest_cfg"),
    R("GET", "/api/grip-profiles", 200, "[]"),
    R("PATCH", "/api/grip-profiles/{labware_type}", 200, "patch", path="/api/grip-profiles/rest_type",
      body={"set": {"speed": 50}, "reason": "s"}),
    R("GET", "/api/grip-profiles/{labware_type}", 200, "patch", path="/api/grip-profiles/rest_type"),
    R("DELETE", "/api/grip-profiles/{labware_type}", 204, "", path="/api/grip-profiles/rest_type"),
    R("GET", "/api/labware-catalog", 200, "labware", params={"category": "trough"}),
    R("POST", "/api/labware-catalog", 201, "labware_type", body=_geometry_body("rest_plate", "Rest plate")),
    R("GET", "/api/labware-catalog/{labware_type}", 200, "display_name", path="/api/labware-catalog/rest_plate"),
    R("PUT", "/api/labware-catalog/{labware_type}", 200, "display_name", path="/api/labware-catalog/rest_plate",
      body=_geometry_body("rest_plate", "Rest plate 2", named=False)),
    R("DELETE", "/api/labware-catalog/{labware_type}", 204, "", path="/api/labware-catalog/rest_plate"),
    R("GET", "/api/move-defaults", 200, "[]"),
    R("PATCH", "/api/move-defaults/{device_id}", 200, "parameters", path="/api/move-defaults/robotic_arm",
      body={"set": {"speed": 60}, "reason": "s"}),
    R("GET", "/api/move-defaults/{device_id}", 200, "parameters", path="/api/move-defaults/robotic_arm"),
    R("DELETE", "/api/move-defaults/{device_id}", 204, "", path="/api/move-defaults/robotic_arm"),
    R("GET", "/api/profiles", 200, "[]"),
    R("POST", "/api/profiles", 201, "name", body={"name": "rest_prof", "variables": {"a": 1}, "reason": "s"}),
    R("GET", "/api/profiles/{name}", 200, "variables", path="/api/profiles/rest_prof"),
    R("PUT", "/api/profiles/{name}", 200, "variables", path="/api/profiles/rest_prof",
      body={"description": "d", "variables": {"a": 2}, "reason": "s"}),
    R("DELETE", "/api/profiles/{name}", 204, "", path="/api/profiles/rest_prof"),
    R("GET", "/api/devices/{device_id}/setup", 200, "setup", path=D + "shaker_1/setup"),
    R("PUT", "/api/devices/{device_id}/setup", 200, "setup", path=D + "shaker_1/setup",
      body={"setup": {"client": "local"}, "reason": "s"}),
    R("GET", "/api/deck-layouts", 200, "[]"),
    R("GET", "/api/deck-layouts", 200, "[]", params={"device_id": "liquid_handler"}),
    R("GET", "/api/deck-layouts", 404, "liquid_handler_not_found", params={"device_id": "shaker_1"}),
    R("GET", "/api/deck-layouts/{device_id}/{name}", 200, "config", path="/api/deck-layouts/liquid_handler/default",
      after=lambda c, b: c.notes.update(deck=str(as_dict(as_dict(b)["config"]).get("deck_type")))),
    R("PUT", "/api/deck-layouts/{device_id}/{name}", 200, "name", path="/api/deck-layouts/liquid_handler/rest_deck",
      body=lambda c: {"config": {"deck_type": c.notes["deck"], "resources": []}, "reason": "s"}),
    R("PUT", "/api/deck-layouts/{device_id}/{name}", 200, "name", path="/api/deck-layouts/liquid_handler/rest_deck",
      body=lambda c: {"config": {"deck_type": c.notes["deck"], "resources": []}, "reason": "s"}),
    R("DELETE", "/api/deck-layouts/{device_id}/{name}", 204, "", path="/api/deck-layouts/liquid_handler/rest_deck"),
    R("GET", "/api/teachpoints", 200, "[]", params={"device_id": "robotic_arm"}),
    R("GET", "/api/teachpoints/{device_id}/{name}", 200, "coords", path="/api/teachpoints/robotic_arm/pad_1"),
    R("POST", "/api/teachpoints/{device_id}", 201, "created", path="/api/teachpoints/robotic_arm",
      body={"teachpoints": [_tp("rest_tp")], "reason": "s"}),
    R("POST", "/api/teachpoints/{device_id}", 409, "already_exists", path="/api/teachpoints/robotic_arm",
      body={"teachpoints": [_tp("rest_tp")]}),
    R("PUT", "/api/teachpoints/{device_id}/{name}", 200, "coords", path="/api/teachpoints/robotic_arm/rest_tp",
      body={"coords": {**COORDS, "x": 8.0}, "reason": "s"}),
    R("PATCH", "/api/teachpoint-overrides/{device_id}/{labware_type}/{name}", 200, "by_labware",
      path="/api/teachpoint-overrides/robotic_arm/rest_type/rest_tp", body={"set": {"speed": 10}, "reason": "s"}),
    R("DELETE", "/api/teachpoint-overrides/{device_id}/{labware_type}/{name}", 204, "",
      path="/api/teachpoint-overrides/robotic_arm/rest_type/rest_tp"),
    R("POST", "/api/teachpoints/{device_id}", 201, "updated", path="/api/teachpoints/robotic_arm",
      body={"teachpoints": [_tp("rest_tp")], "overwrite": True, "reason": "s"}),
    R("POST", "/api/teachpoints/{device_id}/capture", 201, "position_id",
      path="/api/teachpoints/robotic_arm/capture", body={"position_id": "rest_cap", "orientation": "right", "reason": "s"}),
    R("DELETE", "/api/teachpoints/{device_id}/{name}", 204, "", path="/api/teachpoints/robotic_arm/rest_cap"),
    R("DELETE", "/api/teachpoints/{device_id}/{name}", 204, "", path="/api/teachpoints/robotic_arm/rest_tp"),
    # devices
    R("POST", D + "{device_id}/take-control", 200, "status", path=D + "shaker_1/take-control", body={"reason": "s"}),
    R("POST", D + "{device_id}/release-control", 200, "status", path=D + "shaker_1/release-control", body={"reason": "s"}),
    R("POST", D + "{device_id}/clear-fault", 200, "cleared", path=D + "shaker_1/clear-fault", body={"reason": "s"}),
    R("POST", D + "{device_id}/disconnect", 200, "status", path=D + "shaker_1/disconnect", body={"reason": "s"}),
    R("POST", D + "{device_id}/connect", 200, "status", path=D + "shaker_1/connect", body={"reason": "s"}),
    R("POST", D + "{device_id}/initialize", 200, "status", path=D + "shaker_1/initialize", body={"reason": "s"}),
    R("PUT", D + "{device_id}/mode", 200, "switched", path=D + "shaker_1/mode", body={"mode": "SIM", "confirm": True}),
    R("GET", HD + "deck-comparison", 200, "agrees", path=H + "deck-comparison"),
    R("GET", HD + "mounted-tips", 200, "mounted", path=H + "mounted-tips"),
    R("PUT", HD + "mounted-tips", 200, "device_name", path=H + "mounted-tips", body={"mounted": [], "reason": "s"}),
    R("POST", HD + "mounted-tips/confirm", 200, "device_name", path=H + "mounted-tips/confirm", body={"reason": "s"}),
    R("POST", HD + "reconcile-deck", 200, "device_name", path=H + "reconcile-deck", body={"reason": "s"}),
    R("POST", D + "{device_id}/commands", 200, "success", path=D + "shaker_1/commands",
      body={"commands": [{"command": "stop", "params": {}}]}),
    R("POST", D + "{device_id}/commands", 422, "validation_error", path=D + "liquid_handler/commands",
      body={"commands": [{"command": "configure_deck", "params": {}}]}),
    R("POST", D + "{device_id}/shake", 200, "success", path=D + "shaker_1/shake", body={"speed": 300, "duration": 1}),
    R("POST", D + "{device_id}/stop", 200, "success", path=D + "shaker_1/stop"),
    R("POST", D + "{device_id}/open", 200, "success", path=D + "shaker_1/open"),
    R("POST", D + "{device_id}/close", 200, "success", path=D + "shaker_1/close"),
    R("POST", D + "{device_id}/lock-plate", 200, "success", path=D + "shaker_1/lock-plate"),
    R("POST", D + "{device_id}/unlock-plate", 200, "success", path=D + "shaker_1/unlock-plate"),
    R("POST", D + "{device_id}/seal", 200, "success", path=D + "sealer/seal", body={"temperature": 100, "duration": 1}),
    # transporter
    R("POST", "/api/transporters/{device_id}/home", 200, "success", path=T + "home"),
    R("POST", "/api/transporters/{device_id}/move-to-safe", 200, "success", path=T + "move-to-safe"),
    R("POST", "/api/transporters/{device_id}/halt", 200, "success", path=T + "halt"),
    R("POST", "/api/transporters/{device_id}/set-speed", 200, "success", path=T + "set-speed", body={"speed": 0.5}),
    R("GET", "/api/transporters/{device_id}/speed", 200, "success", path=T + "speed"),
    R("POST", "/api/transporters/{device_id}/move-to", 200, "success", path=T + "move-to", body={"position_id": "pad_1"}),
    R("GET", "/api/transporters/{device_id}/cartesian-position", 200, "success", path=T + "cartesian-position"),
    R("POST", "/api/teachpoints/{device_id}", 201, "created", path="/api/teachpoints/robotic_arm", body={"teachpoints": [{
        "position_id": "rest_joint", "coord_type": "joint", "coords": JOINTS}], "reason": "s"}),
    R("POST", "/api/transporters/{device_id}/move-to", 200, "success", path=T + "move-to", body={"position_id": "rest_joint"}),
    R("GET", "/api/transporters/{device_id}/joint-position", 200, "success", path=T + "joint-position"),
    R("POST", "/api/transporters/{device_id}/open-gripper", 200, "success", path=T + "open-gripper", body={}),
    R("POST", "/api/transporters/{device_id}/close-gripper", 200, "success", path=T + "close-gripper", body={}),
    R("POST", "/api/transporters/{device_id}/move-single-axis", 200, "success", path=T + "move-single-axis",
      body={"axis": "z", "position": 100}),
    R("POST", "/api/transporters/{device_id}/move-single-axis-relative", 200, "success",
      path=T + "move-single-axis-relative", body={"axis": "z", "distance": 5}),
    R("POST", "/api/transporters/{device_id}/set-free-mode", 200, "success", path=T + "set-free-mode",
      body={"axes": "none"}),
    R("POST", "/api/transporters/{device_id}/move-to-coords", 200, "success", path=T + "move-to-coords",
      body={"teachpoint": {"position_id": "pad_1", "coord_type": "cartesian", **COORDS, "orientation": "right"}}),
    # labware, moved by hand and by arm
    R("POST", L + "clear-all", 200, "cleared", body={"force": True, "reason": "s"}),
    R("POST", "/api/labware-instances", 200, "labware", body={
        "template_name": "sample_plate", "barcode": "REST1", "location": "pad_1", "reason": "s"},
      after=note_labware("l1", "n1")),
    R("GET", L + "{labware_id}", 200, "labware", path=L + "{l1}"),
    R("GET", "/api/labware-barcodes/{barcode}", 200, "labware", path="/api/labware-barcodes/REST1"),
    R("PUT", L + "{labware_id}/barcode", 200, "status", path=L + "{l1}/barcode",
      body={"new_barcode": "REST1B", "reason": "s"}),
    R("PUT", L + "{labware_id}/well-volumes", 200, "status", path=L + "{l1}/well-volumes",
      body={"well_volumes": {"A1": 100, "A2": 100}, "reason": "s"}),
    R("POST", L + "{labware_id}/well-volumes/confirm", 200, "status", path=L + "{l1}/well-volumes/confirm",
      body={"reason": "s"}),
    R("PUT", L + "{labware_id}/carry-override", 200, "carry_override", path=L + "{l1}/carry-override",
      body={"set": {"speed": 30}, "reason": "s"}),
    R("DELETE", L + "{labware_id}/carry-override", 200, "carry_override", path=L + "{l1}/carry-override",
      params={"reason": "s"}),
    R("POST", "/api/transporters/{device_id}/pick-plate", 200, "success", path=T + "pick-plate", body={"position_id": "pad_1"}),
    R("POST", "/api/movers/{device_id}/release-hold", 200, "discharged", path="/api/movers/robotic_arm/release-hold",
      body={"to_location": "pad_1", "reason": "s"}),
    R("POST", "/api/transporters/{device_id}/pick-plate", 200, "success", path=T + "pick-plate", body={"position_id": "pad_1"}),
    R("POST", "/api/transporters/{device_id}/place-plate", 200, "success", path=T + "place-plate", body={"position_id": "pad_2"}),
    R("POST", L + "{labware_id}/edit-location", 200, "status", path=L + "{l1}/edit-location",
      body={"location": "liquid_handler/carrier-7-2", "reason": "s"}),
    R("POST", L + "{labware_id}/reset-location", 200, "status", path=L + "{l1}/reset-location",
      body={"location": "liquid_handler/carrier-7-2", "reason": "s"}),
    # On a liquid handler's deck, so the volumes are pushed to the driver under the handler's lock.
    R("PUT", L + "{labware_id}/well-volumes", 200, "status", path=L + "{l1}/well-volumes",
      body={"well_volumes": {"A1": 50}, "reason": "s"}),
    R("POST", "/api/labware-instances", 200, "labware", body={
        "template_name": "tips", "location": "liquid_handler/carrier-15-1", "reason": "s"},
      after=note_labware("l2", "n2")),
    R("PUT", L + "{labware_id}/tips", 200, "status", path=L + "{l2}/tips",
      body={"tip_positions_present": ["A1", "B1", "C1", "D1"], "reason": "s"}),
    R("POST", L + "{labware_id}/tips/confirm", 200, "status", path=L + "{l2}/tips/confirm", body={"reason": "s"}),
    R("POST", L + "{labware_id}/tips/mark-used", 200, "tip_positions_present", path=L + "{l2}/tips/mark-used",
      body={"positions": ["D1"], "reason": "s"}),
    # liquid handler
    R("POST", HD + "configure-deck", 200, "success", path=H + "configure-deck", body={"layout_name": "default"}),
    R("POST", HD + "reconcile-deck", 200, "device_name", path=H + "reconcile-deck", body={"reason": "s"}),
    R("GET", HD + "deck-state", 200, "success", path=H + "deck-state"),
    R("POST", HD + "reconcile-hardware-state", 200, "success", path=H + "reconcile-hardware-state"),
    R("POST", HD + "pick-up-tips", 200, "success", path=H + "pick-up-tips",
      body=lambda c: {"picks": [{"tip_rack": c.notes["n2"], "positions": ["A1"]}]}),
    R("POST", "/api/liquid-handlers/{device_id}/aspirate", 200, "success", path=H + "aspirate",
      body=lambda c: {"aspirations": [{"labware": c.notes["n1"], "positions": ["A1"], "volumes": [5]}]}),
    R("POST", "/api/liquid-handlers/{device_id}/dispense", 200, "success", path=H + "dispense",
      body=lambda c: {"dispenses": [{"labware": c.notes["n1"], "positions": ["A2"], "volumes": [5]}]}),
    R("POST", "/api/liquid-handlers/{device_id}/mix", 200, "success", path=H + "mix",
      body=lambda c: {"labware": c.notes["n1"], "positions": ["A2"], "volume": 5, "repetitions": 2}),
    R("POST", HD + "liquid-probe", 422, "validation_error", path=H + "liquid-probe",
      body=lambda c: {"labware": c.notes["n1"], "positions": ["A2"]}),
    R("POST", HD + "drop-tips", 200, "success", path=H + "drop-tips", body={"to_waste": True}),
    R("POST", HD + "pick-up-tips", 200, "success", path=H + "pick-up-tips",
      body=lambda c: {"picks": [{"tip_rack": c.notes["n2"], "positions": ["B1"]}]}),
    R("POST", HD + "discard-tips", 200, "success", path=H + "discard-tips", body={}),
    R("POST", HD + "discard-stranded-tips", 502, "command_error", path=H + "discard-stranded-tips",
      contains="no tip-presence sensor"),
    R("PUT", L + "{labware_id}/tips", 200, "status", path=L + "{l2}/tips",
      body={"tip_positions_present": ALL_TIPS, "reason": "s"}),
    R("POST", HD + "pick-up-tips96", 200, "success", path=H + "pick-up-tips96",
      body=lambda c: {"tip_rack": c.notes["n2"]}),
    R("POST", "/api/liquid-handlers/{device_id}/aspirate96", 200, "success", path=H + "aspirate96",
      body=lambda c: {"labware": c.notes["n1"], "volume": 5}),
    R("POST", "/api/liquid-handlers/{device_id}/dispense96", 200, "success", path=H + "dispense96",
      body=lambda c: {"labware": c.notes["n1"], "volume": 5}),
    R("POST", HD + "return-tips96", 200, "success", path=H + "return-tips96"),
    R("POST", HD + "drop-tips96", 200, "success", path=H + "drop-tips96", body={"to_waste": True}),
    R("POST", HD + "move-plate", 200, "success", path=H + "move-plate",
      body=lambda c: {"plate": c.notes["n1"], "to_position": "carrier-7-3"}),
    R("POST", L + "{labware_id}/discharge", 200, "status", path=L + "{l1}/discharge", body={"force": True, "reason": "s"}),
    R("POST", L + "{labware_id}/discharge", 200, "status", path=L + "{l2}/discharge", body={"force": True, "reason": "s"}),
    R("POST", "/api/workflows/{workflow_name}/load-run-labware", 200, "blockers",
      path="/api/workflows/learn_orca/load-run-labware"),
    # check run, method run, submissions
    # In a lab no run has started in, so the run brings its devices up under their locks.
    R("POST", "/api/method-executions", 201, "execution_id", body={
        "workflow_name": "learn_orca", "method_name": "shake_step", "labware_start": {"dest_plate": "pad_1"},
        "labware_end": {"dest_plate": "pad_2"}, "run_mode": "SIM"}, world="unstarted"),
    R("POST", "/api/submissions/{submission_id}/clear-labware", 200, "cleared",
      path="/api/submissions/{sid}/clear-labware", body={"reason": "s"}),
    # manual step
    R("POST", "/api/executions", 200, "execution_id",
      body={"workflow_name": "surface_manual", "run_mode": "SIM"}, before=Context.clear_every_fault,
      after=_manual_step),
    R("POST", "/api/executions/{execution_id}/manual-steps/{step_id}/confirm", 200, "status",
      path=E + "{manual_eid}/manual-steps/{step_id}/confirm", body={"reason": "s"}),
]

HAMILTON: list[RestCase] = [
    R("POST", D + "{device_id}/centrifuge", 200, "success", path=D + "centrifuge/centrifuge",
      body={"g": 100, "duration": 1}, world="hamilton"),
    R("POST", D + "{device_id}/open", 200, "success", path=D + "centrifuge/open", world="hamilton"),
    R("POST", D + "{device_id}/close", 200, "success", path=D + "centrifuge/close", world="hamilton"),
    R("POST", D + "{device_id}/dispense", 422, "validation_error", path=D + "biotek_1/dispense", world="hamilton"),
    R("POST", D + "{device_id}/open-lid", 200, "success", path=D + "thermocycler_1/open-lid", world="hamilton"),
    R("POST", D + "{device_id}/close-lid", 200, "success", path=D + "thermocycler_1/close-lid", world="hamilton"),
    R("POST", D + "{device_id}/deactivate-block", 200, "success", path=D + "thermocycler_1/deactivate-block", world="hamilton"),
    R("POST", D + "{device_id}/deactivate-lid", 200, "success", path=D + "thermocycler_1/deactivate-lid", world="hamilton"),
    R("POST", D + "{device_id}/set-block-temperature", 200, "success", path=D + "thermocycler_1/set-block-temperature",
      body={"temperature": [37.0]}, world="hamilton"),
    R("POST", D + "{device_id}/set-lid-temperature", 200, "success", path=D + "thermocycler_1/set-lid-temperature",
      body={"temperature": [105.0]}, world="hamilton"),
    R("POST", D + "{device_id}/run-protocol", 200, "success", path=D + "thermocycler_1/run-protocol", world="hamilton",
      body={"protocol": {"stages": [{"steps": [{"temperature": [95.0], "hold_seconds": 1}], "repeats": 1}]},
            "block_max_volume": 50}),
    R("POST", D + "{device_id}/set-temperature", 200, "success", path=D + "sealer/set-temperature",
      body={"temperature": 37}, world="hamilton"),
    R("GET", D + "{device_id}/temperature", 200, "success", path=D + "sealer/temperature", world="hamilton"),
    *[
        R("GET", D + "{device_id}/" + name, 200, "success", path=D + "thermocycler_1/" + name, world="hamilton")
        for name in (
            "block-current-temperature", "block-target-temperature", "lid-current-temperature",
            "lid-target-temperature", "lid-open", "lid-status", "block-status", "hold-time",
            "current-cycle-index", "total-cycle-count", "current-step-index", "total-step-count",
        )
    ],
]

ALL_CASES = [*CASES, *HAMILTON]

REFUSAL_ONLY: dict[str, str] = {
    "POST /api/liquid-handlers/{device_id}/discard-stranded-tips": "the sim handler has no tip sensor",
    "POST /api/executions/{execution_id}/threads/{thread_id}/insert-action":
        "the action needs a free slot on a device the paused thread is not at",
    "POST /api/executions/{execution_id}/threads/{thread_id}/replace-action":
        "the action needs a free slot on a device the paused thread is not at",
    "POST /api/liquid-handlers/{device_id}/liquid-probe": "the simulated liquid handler does not support liquid_probe",
    "POST /api/devices/{device_id}/dispense": "no simulated device in the examples supports dispense",
}

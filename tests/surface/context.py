"""What the cases share: a finished run, a paused run, and the ids the walk of a live daemon turns up."""

import time
from dataclasses import dataclass, field

from pydantic import JsonValue

from tests.surface.world import World

_PAUSED_OR_DONE = ("PAUSED", "COMPLETED", "CREATED")
FINISHED_RUN_LIMIT_S = 240.0
_BEFORE_THE_PAUSE_S = 4.0
_PAUSE_LIMIT_S = 90.0
PAUSED_RUN_LIMIT_S = _BEFORE_THE_PAUSE_S + _PAUSE_LIMIT_S
COORDS: dict[str, JsonValue] = {"x": 5.0, "y": 6.0, "z": 7.0, "yaw": 0.0, "pitch": 90.0, "roll": 180.0}


def plate_geometry(labware_type: str) -> dict[str, JsonValue]:
    wells: list[JsonValue] = [
        {
            "identifier": f"{'ABCDEFGH'[row]}{col + 1}", "row": row, "col": col,
            "position_x": 10.7 + 9 * col, "position_y": 70.7 - 9 * row, "position_z": 6.7,
            "size_x": 6.96, "size_y": 6.96, "size_z": 10.9, "max_volume": 392.0, "bottom_type": "flat", "volume": 0.0,
        }
        for row in range(8) for col in range(12)
    ]
    return {
        "category": "plate", "labware_type": labware_type, "display_name": labware_type, "vendor": "corning",
        "plr_class_name": "Cor_Falcon_96_wellplate_340ul_Fb_Black", "num_rows": 8, "num_cols": 12,
        "size_x": 127.76, "size_y": 85.48, "size_z": 14.4, "wells": wells,
    }


def as_dict(value: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(value, dict), value
    return value


def as_list(value: JsonValue) -> list[JsonValue]:
    assert isinstance(value, list), value
    return value


@dataclass
class Context:
    """Ids of the records the cases act on, found once from the daemon."""

    world: World
    notes: dict[str, str] = field(default_factory=dict)

    def post(self, path: str, body: JsonValue) -> dict[str, JsonValue]:
        return as_dict(self.world.request("POST", path, json=body).body)

    def clear_every_fault(self) -> None:
        """A fresh run refuses a faulted device, and an abort earlier in the suite can leave one."""
        for device in as_list(as_dict(self.world.request("GET", "/api/devices").body)["devices"]):
            self.post(f"/api/devices/{as_dict(device)['name']}/clear-fault", {"reason": "surface suite"})

    def submit(self, *, sim_speedup: float | None = None) -> str:
        body: dict[str, JsonValue] = {"workflow_name": "learn_orca", "run_mode": "SIM"}
        if sim_speedup is not None:
            body["sim_speedup"] = sim_speedup
        return str(self.post("/api/executions", body)["execution_id"])

    def threads(self, execution_id: str) -> list[dict[str, JsonValue]]:
        reply = self.world.request("GET", f"/api/executions/{execution_id}")
        return [as_dict(t) for t in as_list(as_dict(reply.body)["threads"])]

    def submission_of(self, execution_id: str) -> str:
        reply = self.world.request("GET", "/api/submissions", params={"execution_id": execution_id})
        subs = as_list(as_dict(reply.body)["submissions"])
        return str(as_dict(subs[0])["id"])

    def labware_of_the_run(self, execution_id: str, template: str) -> str:
        """The run's own labware: one the run took away at its end is no longer listed, and its history still reads."""
        return next(str(t["labware_id"]) for t in self.threads(execution_id) if str(t["name"]).startswith(template))

    def build_finished_run(self) -> None:
        eid = self.submit()
        assert self.world.wait_execution(eid, FINISHED_RUN_LIMIT_S) == "completed"
        self.notes["eid"] = eid
        self.notes["tid"] = str(self.threads(eid)[0]["id"])
        self.notes["sid"] = self.submission_of(eid)
        self.notes["plate_id"] = self.labware_of_the_run(eid, "dest_plate")
        self.notes["rack_id"] = self.labware_of_the_run(eid, "tips")

    def build_paused_run(self) -> None:
        self.pause_run(self.submit(sim_speedup=1))

    def pause_run(self, eid: str) -> None:
        self.notes["e2"] = eid
        time.sleep(_BEFORE_THE_PAUSE_S)
        scope: JsonValue = {"kind": "execution", "execution_id": eid}
        self.post("/api/pause", {"scope": scope, "reason": "surface suite"})
        end = time.time() + _PAUSE_LIMIT_S
        threads = self.threads(eid)
        while time.time() < end and not all(t["status"] in _PAUSED_OR_DONE for t in threads):
            time.sleep(1)
            threads = self.threads(eid)
        self.notes["t2"] = str(next(t["id"] for t in threads if str(t["name"]).startswith("dest_plate")))
        self.notes["s2"] = self.submission_of(eid)

    def wait_for_manual_step(self) -> str:
        end = time.time() + 60
        while time.time() < end:
            pending = as_list(as_dict(self.world.request("GET", "/api/manual-steps").body)["pending"])
            if pending:
                return str(as_dict(pending[0])["step_id"])
            time.sleep(0.5)
        raise AssertionError("the manual-step workflow never asked for a step")

    def labware_name_starting(self, prefix: str) -> str:
        items = as_list(as_dict(self.world.request("GET", "/api/labware-instances").body)["labware"])
        return next(str(as_dict(i)["name"]) for i in items if str(as_dict(i)["id"]).startswith(prefix))

    def execution_id_starting(self, prefix: str) -> str:
        items = as_list(as_dict(self.world.request("GET", "/api/executions").body)["executions"])
        return next(str(as_dict(i)["id"]) for i in items if str(as_dict(i)["id"]).startswith(prefix))

    def labware_id_starting(self, prefix: str) -> str:
        items = as_list(as_dict(self.world.request("GET", "/api/labware-instances").body)["labware"])
        return next(str(as_dict(i)["id"]) for i in items if str(as_dict(i)["id"]).startswith(prefix))

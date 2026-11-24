"""One or more cases per `orca` verb, run in order against one daemon in simulation.

A `$name` in an argument is replaced from the context's notes. A verb whose cases all end in a refusal is
listed in `CLI_REFUSAL_ONLY` with the reason success cannot be produced in simulation.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from string import Template

from tests.surface.context import COORDS, Context, as_dict, as_list, plate_geometry
from tests.surface.workflow_sources import SHAKE_ACTION

Hook = Callable[[Context, str], None]


@dataclass(frozen=True)
class CliCase:
    verb: str
    argv: tuple[str, ...]
    exit_code: int
    expect: str
    yes: bool = False
    world: str = "lab"
    before: Callable[[Context], None] | None = None
    after: Hook | None = None

    def resolved(self, c: Context) -> list[str]:
        words = [Template(a).safe_substitute(c.notes) for a in self.argv]
        return ["-y", *self.verb.split(), *words] if self.yes else [*self.verb.split(), *words]

    @property
    def case_id(self) -> str:
        return f"{self.verb} {' '.join(self.argv)}".strip()[:110]


def V(verb: str, *argv: str, exit_code: int = 0, expect: str = "", yes: bool = False, world: str = "lab",
      before: Callable[[Context], None] | None = None, after: Hook | None = None) -> CliCase:
    return CliCase(verb, argv, exit_code, expect, yes, world, before, after)


def _register_noting(key: str) -> Hook:
    def note(c: Context, output: str) -> None:
        match = re.search(r"registered labware (\w+)", output)
        assert match, output
        c.notes[key] = match.group(1)
        c.notes[key] = c.labware_id_starting(match.group(1))
        c.notes[key + "_name"] = c.labware_name_starting(match.group(1))
    return note


def _wait_e2(c: Context) -> None:
    c.world.wait_execution(c.notes["e2"], 90.0)


def _files(c: Context) -> None:
    folder = c.world.folder.parent / "cli-files"
    folder.mkdir(exist_ok=True)
    (folder / "action.py").write_text(SHAKE_ACTION, encoding="utf-8")
    (folder / "coords.json").write_text(json.dumps(COORDS), encoding="utf-8")
    (folder / "geometry.json").write_text(json.dumps(plate_geometry("cli_plate")), encoding="utf-8")
    (folder / "deck.json").write_text(json.dumps({"deck_type": "STARlet", "resources": []}), encoding="utf-8")
    (folder / "setup.json").write_text(json.dumps({"client": "local"}), encoding="utf-8")
    (folder / "export.json").write_text("", encoding="utf-8")
    c.notes["files"] = str(folder).replace("\\", "/")


def _first_reservation(c: Context, output: str) -> None:
    reservations = as_list(c.world.request("GET", "/api/reservations").body)
    c.notes["reservation"] = str(as_dict(reservations[0])["reservation_id"])


def _manual(c: Context) -> None:
    submitted = c.post("/api/executions", {"workflow_name": "surface_manual", "run_mode": "SIM"})
    c.notes["manual_eid"] = str(submitted["execution_id"])
    c.notes["step_id"] = c.wait_for_manual_step()


def _park_three(c: Context) -> None:
    for key in ("inc_extend", "inc_abort", "inc_done", "inc_listed"):
        c.notes[key] = c.world.park_timeout()


ARM = "robotic_arm"
LH = "liquid_handler"
S = "surface suite"

CLI_CASES: list[CliCase] = [
    V("version", expect="orca"),
    V("describe system", expect="Learn Orca"),
    V("describe workflow", "learn_orca", expect="learn_orca"),
    V("describe method", "shake_step", expect="shake_step"),
    V("describe thread", "dest_plate", expect="dest_plate"),
    V("describe location", "pad_1", expect="pad_1"),
    V("describe device", "shaker_1", expect="shaker_1"),
    V("workflow list", expect="learn_orca"),
    V("workflow get", "learn_orca", expect="learn_orca"),
    V("workflow load", "$files/action.py", exit_code=2, before=_files, expect="local daemon builds"),
    V("method list", expect="shake_step"),
    V("method get", "shake_step", expect="shake_step"),
    V("thread list", expect="Thread templates"),
    V("location list", expect="pad_1"),
    V("topology get", expect="devices"),
    V("runtime status", expect="runtime built"),
    V("device list", expect="shaker_1"),
    V("device info", "shaker_1", expect="shaker_1"),
    V("device capabilities", "shaker_1", expect="IShaker"),
    V("device registry show", "shaker_1", expect="declared_kind"),
    V("device mounted-tips", LH, expect=LH),
    V("device compare-deck", LH, expect=LH),
    V("device setup", "shaker_1", expect="{}"),
    V("events list", "--limit", "5", expect="cursor"),
    V("audit list", expect="action"),
    V("intervention next", "--timeout", "1", expect="no intervention"),
    V("state unsettled", expect="settle"),
    V("manual-step list", expect="Manual steps"),
    V("execution list", expect="learn_orca"),
    V("execution detail", "$eid", expect="learn_orca"),
    V("execution threads", "$eid", expect="Threads"),
    V("execution thread detail", "$eid", "$tid", expect="thread"),
    V("submission list", "--execution", "$eid", expect="Submissions"),
    V("submission detail", "$sid", expect="submission"),
    V("var list", "--execution", "$eid", expect="Variables"),
    V("ops-history get", "$eid", expect="OpsHistory"),
    V("ops-history search", "--execution-id", "$eid", expect="OpsHistory search"),
    V("labware list", expect="Labware"),
    V("labware catalog", expect="Labware catalog"),
    V("labware get", "Cor_Falcon_96_wellplate_340ul_Fb_Black", expect="plate"),
    V("labware where", "$plate_id", expect="dest_plate"),
    V("labware journey", "$plate_id", expect="Journey"),
    V("labware contents", "$plate_id", expect="wells"),
    V("labware load-run", "learn_orca", yes=True, expect="reagent_trough"),
    # variables
    V("var set-global", "cli_g", "hello", "--reason", S, yes=True, expect="cli_g"),
    V("var get-global", "cli_g", expect="cli_g"),
    V("var set", "cli_v", "42", "--execution", "$e2", "--reason", S, yes=True, expect="cli_v"),
    V("var get", "cli_v", "--execution", "$e2", expect="cli_v"),
    V("var unset", "cli_v", "--execution", "$e2", "--reason", S, yes=True, expect="cli_v"),
    V("var unset-global", "cli_g", "--reason", S, yes=True, expect="cli_g"),
    V("execution sim-speedup", "$e2", "3", expect="sim_speedup"),
    # incidents
    V("incident list", before=_park_three, expect="Incidents"),
    V("incident get", "$inc_listed", expect="RECOVERABLE_TIMEOUT"),
    V("incident ack", "$inc_listed", "--reason", S, yes=True, expect="acknowledged"),
    V("incident ack-all", "--reason", S, yes=True, expect="acknowledged"),
    V("incident recoverable-timeout extend", "$inc_extend", "--additional-seconds", "5", "--reason", S, expect="extend"),
    V("incident recoverable-timeout abort", "$inc_abort", "--operator", "suite", "--reason", S, yes=True, expect="abort"),
    V("incident recoverable-timeout mark-complete", "$inc_done", "--operator", "suite", "--reason", S, yes=True, expect="mark"),
    # paused run
    V("execution pause", "$e2", "--reason", S, yes=True, expect="pause"),
    V("reservation list", "--execution", "$e2", expect="Reservat", after=_first_reservation),
    V("plugin list", expect="Plugins"),
    V("execution thread skip-method", "$e2", "$t2", "--method-name", "seal_step", "--reason", S, yes=True, expect="skip"),
    V("execution thread insert-method", "$e2", "$t2", "--template", "shake_step", "--where", "tail", "--reason", S,
      yes=True, expect="insert"),
    V("execution thread replace-method", "$e2", "$t2", "evaluate_qc_step", "--template", "shake_step", "--reason", S,
      yes=True, before=_files, expect="replace"),
    V("execution thread skip-action", "$e2", "$t2", "--action-command", "read_plate", "--reason", S, yes=True, expect="skip"),
    V("execution thread insert-action", "$e2", "$t2", "$files/action.py", "--where", "tail", "--reason", S, yes=True,
      exit_code=2, expect="validation_error"),
    V("execution thread replace-action", "$e2", "$t2", "shake", "$files/action.py", "--reason", S, yes=True, exit_code=2, expect="validation_error"),
    V("execution thread recover", "$e2", "$t2", "abort-method", "--reason", S, yes=True, expect="abort-method"),
    V("execution thread spawn", "$e2", "tips", "--reason", S, yes=True, expect="spawn"),
    V("execution thread recover", "$e2", "$t2", "retry", yes=True, exit_code=2, expect="validation_error"),
    V("reservation cancel", "$reservation", "--execution", "$e2", "--reason", S, yes=True, expect="cancel"),
    V("execution resume", "$e2", "--reason", S, expect="resume"),
    V("execution thread pause", "$e2", "$t2", "--reason", S, yes=True, expect="pause"),
    V("execution thread resume", "$e2", "$t2", "--reason", S, expect="resumed"),
    V("execution close", "$e2", "--reason", S, yes=True, expect="phase"),
    V("execution stop", "$e2", "--reason", S, yes=True, expect="stop"),
    V("execution stop", "$e2", "--confirm", "--reason", S, yes=True, expect="aborted"),
    V("execution remove", "$e2", "--reason", S, yes=True, before=_wait_e2, expect="remove"),
    V("labware clear-all", "--yes", "--reason", S, yes=True, expect="cleared"),
    # setup records
    V("access-configs create", "cli_cfg", "--access-type", "vertical", "--gripper-offset", "12", "--reason", S, expect="created"),
    V("access-configs list", expect="cli_cfg"),
    V("access-configs show", "cli_cfg", expect="vertical"),
    V("access-configs update", "cli_cfg", "--access-type", "horizontal", "--horizontal-clearance", "55", "--reason", S, expect="updated"),
    V("access-configs delete", "cli_cfg", "--reason", S, expect="deleted"),
    V("grip-profiles set", "cli_type", "--speed", "40", "--z-offset", "1.5", "--reason", S, expect="grip profile"),
    V("grip-profiles list", expect="cli_type"),
    V("grip-profiles show", "cli_type", expect="speed"),
    V("grip-profiles reset", "cli_type", "--reason", S, expect="reset"),
    V("labware add", "cli_plate", "--display-name", "Cli plate", "--category", "plate", "--geometry-file", "$files/geometry.json", expect="added"),
    V("labware update", "cli_plate", "--display-name", "Cli plate 2", "--category", "plate", "--geometry-file", "$files/geometry.json", expect="updated"),
    V("labware delete", "cli_plate", expect="deleted"),
    V("move-defaults set", ARM, "--speed", "30", "--jaw-opening", "130", "--reason", S, expect=ARM),
    V("move-defaults list", expect=ARM),
    V("move-defaults show", ARM, expect=ARM),
    V("move-defaults reset", ARM, "--reason", S, expect="reset"),
    V("profiles create", "cli_prof", "--description", "d", "--vars", "a=1,b=two", "--reason", S, expect="created"),
    V("profiles list", expect="cli_prof"),
    V("profiles show", "cli_prof", expect="cli_prof"),
    V("profiles update", "cli_prof", "--vars", "a=5", "--reason", S, expect="updated"),
    V("profiles delete", "cli_prof", "--reason", S, expect="deleted"),
    V("deck-layouts save", LH, "cli_deck", "--config-file", "$files/deck.json", "--reason", S, expect="saved"),
    V("deck-layouts list", LH, expect="cli_deck"),
    V("deck-layouts show", LH, "cli_deck", expect="STARlet"),
    V("deck-layouts delete", LH, "cli_deck", "--reason", S, expect="deleted"),
    V("teachpoints create", ARM, "cli_tp", "--coord-type", "cartesian", "--coords-file", "$files/coords.json",
      "--orientation", "right", "--reason", S, expect="created"),
    V("teachpoints list", ARM, expect="cli_tp"),
    V("teachpoints show", ARM, "cli_tp", expect="cli_tp"),
    V("teachpoints update", ARM, "cli_tp", "--coords", json.dumps({**COORDS, "x": 9.0}), "--reason", S, expect="updated"),
    V("teachpoints labware set", ARM, "cli_tp", "cli_type", "--z-offset", "2", "--speed", "20", "--reason", S, expect="cli_type"),
    V("teachpoints labware list", ARM, "cli_tp", expect="cli_type"),
    V("teachpoints labware clear", ARM, "cli_tp", "cli_type", "--reason", S, expect="cleared"),
    V("teachpoints capture", ARM, "cli_cap", "--orientation", "right", "--reason", S, expect="cli_cap"),
    V("teachpoints export", ARM, "--out", "$files/export.json", expect="exported"),
    V("teachpoints import", ARM, "$files/export.json", "--overwrite", "--reason", S, expect="saved"),
    V("teachpoints delete", ARM, "cli_tp", "--reason", S, expect="deleted"),
    # devices
    V("device take-control", "shaker_1", "--reason", S, expect="held"),
    V("device release-control", "shaker_1", "--reason", S, expect="released"),
    V("device clear-fault", "shaker_1", "--reason", S, expect="fault"),
    V("device disconnect", "shaker_1", "--reason", S, expect="disconnected"),
    V("device connect", "shaker_1", "--reason", S, expect="connected"),
    V("device initialize", "shaker_1", "--reason", S, expect="initialized"),
    V("device mode", "shaker_1", "SIM", "--confirm", expect="SIM"),
    V("device send", "shaker_1", "shake", "speed=500", "duration=1", expect="success"),
    V("device batch", "shaker_1", "--commands", '[{"command":"shake","params":{"speed":300,"duration":1}},{"command":"stop","params":{}}]', expect=""),
    V("device write-setup", "shaker_2", "$files/setup.json", "--reason", S, expect="written"),
    V("device set-mounted-tips", LH, "--reason", S, expect="recorded"),
    V("device confirm-mounted-tips", LH, "--reason", S, expect="confirmed"),
    V("device reconcile-deck", LH, "--reason", S, expect="re-seeded"),
    V("device configure-deck", LH, "default", "--reason", S, expect="configured"),
    V("device move-to", ARM, "pad_2", expect="moved"),
    V("device open-gripper", ARM, expect="opened"),
    # labware by hand and by arm
    V("labware register", "sample_plate", "--barcode", "CLI1", "--location", "pad_1", "--reason", S,
      expect="registered", after=_register_noting("lw")),
    V("labware edit-barcode", "$lw", "CLI1B", "--reason", S, expect="barcode"),
    V("labware set-volume", "$lw", "--well", "A1=100", "--well", "A2=100", "--reason", S, expect="volumes set"),
    V("labware confirm-volumes", "$lw", "--reason", S, expect="confirmed"),
    V("labware carry", "$lw", "--speed", "25", "--z-offset", "1", "--reason", S, expect="carrying"),
    V("labware carry-normally", "$lw", "--reason", S, expect="normally"),
    V("device pick-plate", ARM, "pad_1", expect="picked"),
    V("device release-hold", ARM, "--to", "pad_1", "--reason", S, expect="released"),
    V("device pick-plate", ARM, "pad_1", expect="picked"),
    V("device place-plate", ARM, "pad_2", expect="placed"),
    V("labware edit-location", "$lw", "liquid_handler/carrier-7-2", "--reason", S, expect="location"),
    V("labware reset-location", "$lw", "liquid_handler/carrier-7-2", "--reason", S, expect="reset"),
    V("labware register", "tips", "--location", "liquid_handler/carrier-15-1", "--reason", S,
      expect="registered", after=_register_noting("lt")),
    V("labware set-tips", "$lt", "--position", "A1", "--position", "B1", "--reason", S, expect="tip layout"),
    V("labware confirm-tips", "$lt", "--reason", S, expect="confirmed"),
    V("labware mark-tips-used", "$lt", "--position", "A1", "--reason", S, expect="tips"),
    V("device send", LH, "pick_up_tips", "--params", '{"picks":[{"tip_rack":"$lt_name","positions":["B1"]}]}', expect="success"),
    V("device aspirate", LH, "--request", '{"aspirations":[{"labware":"$lw_name","positions":["A1"],"volumes":[5]}]}', expect="aspirate"),
    V("device dispense", LH, "--request", '{"dispenses":[{"labware":"$lw_name","positions":["A2"],"volumes":[5]}]}', expect="dispense"),
    V("device mix", LH, "--request", '{"labware":"$lw_name","positions":["A2"],"volume":5,"repetitions":2}', expect="mix"),
    V("device send", LH, "drop_tips", "--params", '{"to_waste": true}', expect="success"),
    V("labware discharge", "$lw", "--force", "--reason", S, expect="discharged"),
    # runs
    V("labware clear-all", "--yes", "--reason", S, yes=True, before=Context.clear_every_fault, expect="cleared"),
    V("run", "learn_orca", "--run-mode", "SIM", "--wait", expect="submitted"),
    V("submission submit", "learn_orca", "--run-mode", "SIM", yes=True, expect="accepted"),
    V("method execute", "learn_orca", "shake_step", "--labware-start", '{"dest_plate": "pad_1"}',
      "--labware-end", '{"dest_plate": "pad_2"}', "--run-mode", "SIM", expect="started"),
    V("labware clear-submission", "$sid", "--reason", S, yes=True, expect="cleared"),
    # manual step
    V("manual-step confirm", "$step_id", "--execution", "$manual_eid", "--reason", S, before=_manual, expect="confirmed"),
]

CLI_REFUSAL_ONLY: dict[str, str] = {
    "workflow load": "loading a workflow file is cloud only; the local daemon builds its package folder",
    "execution thread insert-action": "the action needs a free slot on a device the paused thread is not at",
    "execution thread replace-action": "the action needs a free slot on a device the paused thread is not at",
}

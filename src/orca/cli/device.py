"""`orca device ...` noun sub-app.

Read verbs: list, info (with --capabilities), capabilities,
registry list/show.
Mutation verbs: send and invoke, which both dispatch one command by name,
and the lifecycle steps connect, initialize, disconnect.

All read verbs (`list`, `info`, `info --capabilities`, `capabilities`,
the `registry ...` sub-verbs) and the mutation verbs (`send`, `invoke`,
`connect`, `initialize`, `disconnect`) are classified `both`: they target the resolved
control-plane client. The rich `device_info` / `list_device_snapshots`
snapshots are on the control-plane contract on both backends, so device
reads render identically against either.
"""

import json
from collections.abc import Sequence

import typer
from pydantic import JsonValue

from orca.cli import output
from orca.cli.backend import get_client
from orca.cli.control_plane import (
    DaemonDeviceDTO,
    DeviceFaultDTO,
    DeviceRegistryEntryDTO,
)
from orca.daemon.schemas import DeviceInvocationResultDTO
from orca.operations.device_models import DeckDisagreementDTO, MountedTipDTO
from orca.runtime.run_modes import WorkflowRunMode

MODE_HELP = (
    "Which world the verb means: LIVE (default), DEVICE_SIM, or PURE_SIM. "
    "The topology sim_override ratchet applies on top."
)


app = typer.Typer(help="Device inventory and direct invocation.", no_args_is_help=True)
registry_app = typer.Typer(
    help="Unified device registry: topology + connection cards.",
    no_args_is_help=True,
)
app.add_typer(registry_app, name="registry")


@app.command("list")
def list_devices() -> None:
    """List all devices in the system with their runtime snapshot.

    Both backends render the rich snapshot (busy / initialized / mode);
    `list_device_snapshots` is on the control-plane contract on both.
    """
    snaps = get_client().list_device_snapshots()
    if output.get_mode().value == "json":
        output.emit_json([d.model_dump(mode="json") for d in snaps])
        return
    output.emit_table(
        "Devices",
        ["name", "type", "busy", "initialized", "mode", "fault"],
        [
            [
                d.name, d.type_name, str(d.is_busy),
                str(d.is_initialized), d.effective_mode.value,
                _fault_column(d.fault),
            ]
            for d in snaps
        ],
    )


def _fault_column(fault: DeviceFaultDTO | None) -> str:
    """What stopped this device, short enough to scan a table for.

    The workflow will not drive a faulted device, so a row that showed only
    busy / initialized reads healthy on a machine nothing may touch.
    """
    if fault is None:
        return ""
    return f"{fault.command} ({fault.outcome})"


def _param_names(params: JsonValue) -> list[str]:
    """Parameter names off an introspection method record, or none.

    `methods` is a JSON blob the driver's own introspection produced, so the
    value is a mapping only by convention.
    """
    if not isinstance(params, dict):
        return []
    return sorted(params)


@app.command("capabilities")
def capabilities(
    device_id: str = typer.Argument(..., help="Device id / name."),
) -> None:
    """Show driver introspection: interfaces, capabilities, methods.

    Same payload shape on both backends:
    * Local daemon: GET /devices/{name}/introspection.
    * Hosted deployment: GET /api/devices/{id}/capabilities.

    Coexists with `orca device info --capabilities`, which prints the
    daemon's per-command-descriptor list (different content).
    """
    info = get_client().device_introspection(device_id)
    if output.get_mode().value == "json":
        output.emit_json(info.model_dump(mode="json"))
        return
    output.emit_kv(f"device {info.name}", [
        ("type", info.type or ""),
        ("interfaces", ", ".join(info.interfaces) or "-"),
        ("capabilities", ", ".join(info.capabilities) or "-"),
        ("provides_state", str(info.provides_state)),
    ])
    if not info.methods:
        output.info("(no methods advertised)")
        return
    output.emit_table(
        f"Methods ({info.name})",
        ["name", "kind", "returns", "params"],
        [
            [
                method_name,
                str(meta.get("kind", "")),
                str(meta.get("returns") or ""),
                ", ".join(_param_names(meta.get("params"))),
            ]
            for method_name, meta in sorted(info.methods.items())
        ],
    )


@app.command("info")
def info(
    name: str = typer.Argument(...),
    capabilities: bool = typer.Option(
        False, "--capabilities",
        help="Also list the device's supported capabilities + parameters.",
    ),
) -> None:
    """Show one device's snapshot and (optionally) its capabilities."""
    client = get_client()
    snap = client.device_info(name)
    if output.get_mode().value == "json":
        payload: dict[str, JsonValue] = snap.model_dump(mode="json")
        if capabilities:
            caps = client.device_capabilities(name)
            payload["capabilities"] = [c.model_dump(mode="json") for c in caps]
        output.emit_json(payload)
        return
    output.emit_kv(f"device {snap.name}", [
        ("type", snap.type_name),
        ("initialized", str(snap.is_initialized)),
        ("busy", str(snap.is_busy)),
        ("mode", snap.effective_mode.value),
        ("locations", ", ".join(snap.position_ids) or "-"),
        ("loaded", ", ".join(snap.loaded_labware_ids) or "-"),
    ])
    if capabilities:
        caps = client.device_capabilities(name)
        if not caps:
            output.info(f"(no capabilities advertised on '{name}')")
            return
        output.emit_table(
            f"Capabilities ({name})",
            ["capability", "danger", "cli", "params"],
            [
                [
                    c.capability, c.danger_level,
                    "yes" if c.cli_accessible else "no",
                    ", ".join(
                        f"{p.name}:{p.type_name}"
                        + ("?" if not p.required else "")
                        for p in c.params
                    ),
                ]
                for c in caps
            ],
        )


def _parse_kv_pairs(pairs: list[str] | None) -> dict[str, JsonValue]:
    """Parse [key=value, ...] into a typed dict. Best-effort type coercion."""
    if not pairs:
        return {}
    out: dict[str, JsonValue] = {}
    for pair in pairs:
        if "=" not in pair:
            output.fail(
                f"invalid argument {pair!r}; expected key=value",
                code=output.EXIT_USAGE,
            )
        k, _, v = pair.partition("=")
        k = k.strip()
        v = v.strip()
        if v.lower() == "true":
            out[k] = True
        elif v.lower() == "false":
            out[k] = False
        else:
            try:
                out[k] = int(v)
            except ValueError:
                try:
                    out[k] = float(v)
                except ValueError:
                    out[k] = v
    return out


def _parse_options(
    options_flag: str | None, positional_pairs: list[str] | None,
) -> dict[str, JsonValue]:
    """Pick the richest source of options/kwargs.

    --params JSON wins if provided; otherwise fall back to positional
    key=value pairs. Returns {} when neither is given.
    """
    if options_flag:
        try:
            parsed = json.loads(options_flag)
        except json.JSONDecodeError as e:
            output.fail(
                f"--params must be valid JSON: {e}",
                code=output.EXIT_USAGE,
            )
        if not isinstance(parsed, dict):
            output.fail(
                "--params must be a JSON object (e.g. '{\"speed\": 1000}')",
                code=output.EXIT_USAGE,
            )
        return parsed
    return _parse_kv_pairs(positional_pairs)


def _emit_invocation_result(result: DeviceInvocationResultDTO) -> None:
    """Render a DeviceInvocationResultDTO for both JSON and table modes."""
    dumped = result.model_dump(mode="json")
    if output.get_mode().value == "json":
        output.emit_json(dumped)
        return
    output.emit_kv("device invocation", [
        ("device", dumped["device_name"]),
        ("command", dumped["command_or_capability"]),
        ("success", str(dumped["success"])),
        ("duration_s", f"{dumped['duration_seconds']:.3f}"),
        ("value_type", dumped["value_type"]),
        ("value", dumped["value"] if dumped["value"] is not None else ""),
    ])


@app.command("send")
def send(
    name: str = typer.Argument(...),
    command: str = typer.Argument(...),
    options: list[str] = typer.Argument(
        None, help="key=value pairs (alternative to --params).",
    ),
    params: str = typer.Option(
        "", "--params", help="JSON-encoded options dict, e.g. '{\"speed\": 1000}'.",
    ),
    mode: WorkflowRunMode | None = typer.Option(None, "--mode", help=MODE_HELP),
    confirm: bool = typer.Option(
        False, "--confirm",
        help="Acknowledge a command the driver flags requires_confirm: the "
             "raw vendor console, whose payload nothing between here and the "
             "controller can read. No other command needs it.",
    ),
) -> None:
    """Send a command to a device by name.

    Reaches any command the device advertises, including the vendor commands
    a driver forwards to its hardware. Bypasses workflow coordination; if the
    device is busy with a workflow action this will contend on the device lock.
    """
    client = get_client()
    opts = _parse_options(params, options)
    result = client.device_execute(
        name, command, options=opts, mode=mode, confirm=confirm,
    )
    _emit_invocation_result(result)


@app.command("invoke")
def invoke(
    name: str = typer.Argument(...),
    capability: str = typer.Argument(
        ..., help="Method name as listed by `orca device capabilities`.",
    ),
    kwargs_pairs: list[str] = typer.Argument(
        None, help="key=value pairs (alternative to --params).",
    ),
    params: str = typer.Option(
        "", "--params", help="JSON-encoded kwargs dict.",
    ),
    mode: WorkflowRunMode | None = typer.Option(None, "--mode", help=MODE_HELP),
    confirm: bool = typer.Option(
        False, "--confirm",
        help="Acknowledge a command the driver flags requires_confirm: the "
             "raw vendor console, whose payload nothing between here and the "
             "controller can read. No other command needs it.",
    ),
) -> None:
    """Invoke a method on a device.

    Pass the name as it appears in `orca device capabilities <name>` output.
    An interface row carries an orca-side namespace (`shaker.shake`) which is
    not part of the method name and is dropped; the bare form works too. A
    vendor command's prefix names the object it lives on (`gripper.ungrip`)
    and is part of the name, so it is sent whole.
    """
    client = get_client()
    kwargs = _parse_options(params, kwargs_pairs)
    result = client.device_invoke(
        name, capability, kwargs=kwargs, mode=mode, confirm=confirm,
    )
    _emit_invocation_result(result)


@app.command("initialize")
def initialize(
    name: str = typer.Argument(...),
    mode: WorkflowRunMode | None = typer.Option(None, "--mode", help=MODE_HELP),
) -> None:
    """Initialize or re-initialize a device.

    Brings the device up and resets driver state. Can drop the labware it was
    tracking, reset calibration, and interrupt an action currently holding the
    device lock. It asks for no motion; homing is its own command.

    A clean run clears any fault standing on the device.
    """
    client = get_client()
    client.device_initialize(name, mode=mode)
    output.info(f"device {name} initialized")


@app.command("connect")
def connect(
    name: str = typer.Argument(...),
    mode: WorkflowRunMode | None = typer.Option(None, "--mode", help=MODE_HELP),
) -> None:
    """Open the link to a device. Moves nothing and readies nothing.

    Use this to take a device without initializing it. `initialize` is the
    step that makes it ready to command.
    """
    client = get_client()
    client.device_connect(name, mode=mode)
    output.info(f"device {name} connected")


@app.command("disconnect")
def disconnect(
    name: str = typer.Argument(...),
    mode: WorkflowRunMode | None = typer.Option(None, "--mode", help=MODE_HELP),
) -> None:
    """Hand a device back. Moves nothing, but ends the session.

    Anything mid-action on this device fails, and hardware that holds motor
    power while connected (a PF400 arm) drops it here.
    """
    client = get_client()
    client.device_disconnect(name, mode=mode)
    output.info(f"device {name} disconnected")


@app.command("release-hold")
def release_hold(
    mover_name: str = typer.Argument(..., help="Mover the record says is holding."),
    to: str | None = typer.Option(
        None, "--to",
        help="Where the labware really is now. Omit to discharge it instead.",
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Discharge even while a live thread still carries the labware. "
             "Ignored when --to is given: a carrying thread is told to plan "
             "again, not stranded.",
    ),
    reason: str = typer.Option(..., "--reason", help="Why (audit trail)."),
) -> None:
    """Free a mover the record says is holding a plate.

    An abort taken while a plate is genuinely in the jaws leaves a real hold,
    and a mover holding one refuses every later pick. Give `--to` the position
    you actually put the plate: the jaws are freed and the thread carrying it
    plans a fresh move from there. Leave `--to` off when the jaws are empty and
    the record is wrong, and the labware is discharged.

    No mover is driven either way. Push the corrected world to the instrument
    afterwards with `orca device reconcile-deck`.
    """
    client = get_client()
    result = client.release_mover_hold(
        mover_name, reason, to_location=to, force=force,
    )
    if result.discharged:
        output.info(
            f"[cyan]{result.mover_name}[/cyan] released; "
            f"'{result.labware_name}' discharged"
        )
    else:
        output.info(
            f"[cyan]{result.mover_name}[/cyan] released; "
            f"'{result.labware_name}' -> '{result.released_to}'"
        )


@app.command("reconcile-deck")
def reconcile_deck(
    name: str = typer.Argument(..., help="Liquid handler name."),
    mode: WorkflowRunMode | None = typer.Option(None, "--mode", help=MODE_HELP),
) -> None:
    """Re-seed a liquid handler from the world model. A state push, no motion.

    Re-dispatches the deck layout and re-declares every labware the ledger
    places on its deck. Use after anything that rebuilt the driver's session
    outside orca (a run cancelled on the touchscreen, a driver-internal
    recovery), so a RETRY can find its labware again. Initialize / connect
    through orca re-seed automatically.
    """
    client = get_client()
    result = client.device_reconcile_deck(name, mode=mode)
    _report_disagreements(name, result.disagreements)
    output.info(f"device {name} deck re-seeded from the world model")


@app.command("mounted-tips")
def mounted_tips(
    name: str = typer.Argument(..., help="Liquid handler name."),
) -> None:
    """What the record says this head is carrying.

    Reads only. `unknown` means nothing has ever said, which is different from
    a head known to be carrying nothing. A channel marked inferred was counted
    from zero because the pick named none; correct it with set-mounted-tips.
    """
    client = get_client()
    result = client.device_get_mounted_tips(name)
    if not result.mounted:
        output.info(f"{name}: {result.provenance}, no tips recorded on any channel")
        return
    output.info(f"{name}: {result.provenance}")
    for tip in result.mounted:
        guessed = " (channel number inferred)" if tip.channel_is_inferred else ""
        output.info(f"  channel {tip.channel}: {tip.tip_rack} {tip.position}{guessed}")


@app.command("set-mounted-tips")
def set_mounted_tips(
    name: str = typer.Argument(..., help="Liquid handler name."),
    tip: list[str] = typer.Option(
        [], "--tip",
        help="channel=rack:position, repeated. Pass none to say the head is empty.",
    ),
    reason: str | None = typer.Option(None, "--reason", help="Why you are stating it."),
) -> None:
    """State what the head is carrying.

    Absolute: any channel you do not name is recorded as carrying nothing.
    This is what the head is seeded from after a restart, so a wrong answer
    here makes the next pick wrong.
    """
    mounted = [_parse_mounted_tip(entry) for entry in tip]
    client = get_client()
    client.device_set_mounted_tips(name, mounted, reason=reason)
    output.info(f"{name}: recorded {len(mounted)} channel(s)")


@app.command("confirm-mounted-tips")
def confirm_mounted_tips(
    name: str = typer.Argument(..., help="Liquid handler name."),
    reason: str | None = typer.Option(None, "--reason", help="Why you looked."),
) -> None:
    """Agree with what the record already says the head is carrying.

    Nothing changes except that the answer stops being one nobody has looked
    at. Refused when an unfinished or an aborted action has left the head
    possibly carrying more than the record says: state it with
    `device set-mounted-tips` instead.
    """
    client = get_client()
    client.device_confirm_mounted_tips(name, reason=reason)
    output.info(f"{name}: mounted tips confirmed")


def _parse_mounted_tip(entry: str) -> MountedTipDTO:
    channel, _, where = entry.partition("=")
    rack, _, position = where.partition(":")
    if not channel.strip().isdigit() or not rack or not position:
        raise typer.BadParameter(
            f"expected channel=rack:position, got {entry!r}"
        )
    return MountedTipDTO(
        channel=int(channel), tip_rack=rack, position=position,
    )


@app.command("compare-deck")
def compare_deck(
    name: str = typer.Argument(..., help="Liquid handler name."),
    mode: WorkflowRunMode | None = typer.Option(None, "--mode", help=MODE_HELP),
) -> None:
    """Ask whether the ledger and the liquid handler's own deck still agree.

    Reads only, and moves nothing, and files nothing: reconcile-deck is the
    call that acts and the one that files. Use it when something touched the
    robot outside orca -- a plate moved at the touchscreen, a gripper move that
    died partway -- because reconcile-deck writes the ledger's answer over the
    driver's and there is nothing to compare afterwards. A plate the ledger has
    in a gripper's jaws always disagrees: the jaws are not a deck site.
    """
    client = get_client()
    result = client.device_compare_deck(name, mode=mode)
    if result.driver_deck_empty:
        output.info(
            f"{name} reports an empty deck: its session was rebuilt and the "
            f"deck is waiting to be re-declared. Run reconcile-deck."
        )
        return
    if result.agrees:
        output.info(f"{name}: the deck and the ledger agree")
        return
    _report_disagreements(name, result.disagreements)


@app.command("take-control")
def take_control(
    name: str = typer.Argument(..., help="Device or transporter name."),
    reason: str | None = typer.Option(
        None, "--reason", help="What you are doing with it.",
    ),
) -> None:
    """Take a device out of the workflow's reach while you drive it by hand.

    A running thread that tries to use it, or to move labware into or out of
    it, fails until you release it. Without this a workflow can start a move
    into the device between two of your own commands.
    """
    client = get_client()
    client.device_take_control(name, reason=reason)
    output.info(f"device {name} held; the workflow will not touch it")


@app.command("release-control")
def release_control(
    name: str = typer.Argument(..., help="Device or transporter name."),
) -> None:
    """Give a device back to the workflow. Be clear of it first."""
    client = get_client()
    client.device_release_control(name)
    output.info(f"device {name} released")


@app.command("clear-fault")
def clear_fault(
    name: str = typer.Argument(..., help="Device or transporter name."),
) -> None:
    """Say a device has been looked at after a command left it part-way through.

    A command that fails, times out, or is cancelled after it reached the
    device faults it, and the workflow stops driving it. Look at the machine
    and put it right first: this clears the record, not the trouble.

    Usually not needed. If a thread is paused on this fault, recovering it with
    RETRY, RETRY_OP or CONTINUE says the machine has been looked at and clears
    the fault on the way, and a clean `initialize` or `home` clears it too.
    Reach for this when the machine is fit but neither of those applies.
    """
    client = get_client()
    result = client.device_clear_fault(name)
    if result.cleared is None:
        output.info(f"device {name} had no fault")
        return
    output.info(
        f"device {name} cleared; it was {result.cleared.command!r} "
        f"({result.cleared.error_type}: {result.cleared.error})"
    )


def _report_disagreements(
    name: str, disagreements: Sequence[DeckDisagreementDTO],
) -> None:
    if not disagreements:
        return
    output.info(f"{name}: {len(disagreements)} labware the deck and the ledger disagree about")
    for item in disagreements:
        driver = item.driver_site or "nowhere on the deck"
        output.info(
            f"  {item.labware_name}: ledger says {item.ledger_site}, "
            f"driver says {driver} ({item.reason})"
        )


# -- Unified device registry ------------------------------------------------
#
# These sub-verbs surface the same two-card view a cloud deployment exposes via
# `/api/devices/registry` (REST) and `device_registry_list` /
# `device_registry_show` (MCP). Surface parity rule: every operator-facing
# operation lives on REST + MCP + CLI.


def _link_mode(entry: DeviceRegistryEntryDTO) -> str:
    """Which of the device's drivers answered `device_connected`.

    Read it with the flag: a link open under DEVICE_SIM or PURE_SIM is a
    simulator's, and one open under LIVE on a bench running DEVICE_SIM belongs
    to an instrument the commands are not reaching. "-" means nobody could
    answer, which is an agent-held device whose agent has gone quiet.
    """
    return entry.device_link_mode or "-"


def _emit_registry_entry_kv(entry: DeviceRegistryEntryDTO) -> None:
    """Render a DeviceRegistryEntryDTO via output.emit_kv. Shared by list + show."""
    declared_kind = str((entry.topology_card or {}).get("declared_kind") or "")
    advertised_kind = str((entry.connection_card or {}).get("advertised_kind") or "")
    elig = entry.mode_eligibility
    output.emit_kv(f"device {entry.name}", [
        ("declared_kind", declared_kind or "-"),
        ("advertised_kind", advertised_kind or "-"),
        ("client_connected", str(entry.is_client_connected)),
        ("device_connected", str(entry.is_device_connected)),
        ("device_link_mode", _link_mode(entry)),
        ("is_initialized", str(entry.is_initialized)),
        ("mode_eligibility", f"pure_sim={elig.pure_sim} device_sim={elig.device_sim} live={elig.live}"),
        ("fault", entry.fault.message if entry.fault else "-"),
    ])


@registry_app.command("list")
def registry_list() -> None:
    """List the unified device registry.

    Same payload on both backends: each entry carries a topology card,
    connection card (each may be None), the two live connection flags and
    `is_initialized`, and the per-mode eligibility matrix.

    The `client` column is whether the on-prem client is reachable; `device` is
    whether the link is open on whichever driver is being driven. They differ: a
    released device sits under a client that is still heartbeating. Read `link`
    with `device`: it names the driver that answered, and on a deployment with
    no device bridge that is the dispatch driver the lifecycle verbs act on,
    which is the simulator (PURE_SIM) unless something seeded a run mode. `-`
    there means nobody could answer, so `device` is false for want of anywhere
    to put "unknown", not because a link is known closed.
    """
    entries = get_client().device_registry_list()
    if output.get_mode().value == "json":
        output.emit_json([e.model_dump(mode="json") for e in entries])
        return
    output.emit_table(
        "Device registry",
        ["name", "declared", "advertised", "client", "device", "link",
         "initialized", "modes", "fault"],
        [
            [
                e.name,
                str((e.topology_card or {}).get("declared_kind") or "-"),
                str((e.connection_card or {}).get("advertised_kind") or "-"),
                str(e.is_client_connected),
                str(e.is_device_connected),
                _link_mode(e),
                str(e.is_initialized),
                ",".join(
                    label for label, ok in (
                        ("pure_sim", e.mode_eligibility.pure_sim),
                        ("device_sim", e.mode_eligibility.device_sim),
                        ("live", e.mode_eligibility.live),
                    ) if ok
                ) or "-",
                _fault_column(e.fault),
            ]
            for e in entries
        ],
    )


@registry_app.command("show")
def registry_show(
    name: str = typer.Argument(..., help="Device name."),
) -> None:
    """Show one device's two-card registry entry plus live state."""
    entry = get_client().device_registry_show(name)
    if output.get_mode().value == "json":
        output.emit_json(entry.model_dump(mode="json"))
        return
    _emit_registry_entry_kv(entry)

"""`orca device ...` noun sub-app.

Read verbs: list, info (with --capabilities), capabilities, registry show.
Mutation verbs: send, which dispatches one command by name, and the
lifecycle steps connect, initialize, disconnect.

Every verb targets the resolved control-plane client, so it renders the
same against either server.
"""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import typer
from cheshire_drivers.device_setup import DeviceSetup
from pydantic import JsonValue, ValidationError

from orca.cli import output
from orca.cli.backend import get_client
from orca.cli.device_operator import register_device_verbs
from orca.operations.operator_device_models import BATCH_EXECUTE, BatchExecuteRequest, BatchExecuteResponse
from orca.operations.wire_models import (
    DeviceFaultModel,
    DeviceIntrospectionDTO,
    DeviceRegistryEntryResponse,
)
from orca.operations.device_models import DeckDisagreementDTO, MountedTipDTO
from orca.state.mounted import HEAD96_POSITIONS
from orca.modes.device_modes import DeviceMode

app = typer.Typer(help="Device inventory and direct invocation.", no_args_is_help=True)
registry_app = typer.Typer(
    help="Unified device registry: topology + connection cards.",
    no_args_is_help=True,
)
app.add_typer(registry_app, name="registry")


def _fault_column(fault: DeviceFaultModel | None) -> str:
    """What stopped this device, short enough to scan a table for.

    The workflow will not drive a faulted device, so a row that showed only
    busy / initialized reads healthy on a machine nothing may touch.
    """
    if fault is None:
        return ""
    return f"{fault.command} ({fault.outcome})"


_SCHEMA_TYPE_NAMES = {
    "integer": "int", "number": "float", "string": "str", "boolean": "bool", "array": "list", "object": "dict",
}


def _typed(name: str, declared: JsonValue, required: bool) -> str:
    typed = f"{name}:{declared}" if isinstance(declared, str) else name
    return typed if required else f"{typed}?"


def _request_fields(schema: Mapping[str, JsonValue]) -> list[str]:
    """The fields of a request model's JSON schema, which the operator sends flat."""
    properties, required = schema.get("properties"), schema.get("required")
    if not isinstance(properties, dict):
        return []
    needed = required if isinstance(required, list) else []
    rendered: list[str] = []
    for name, field in sorted(properties.items()):
        declared = field.get("type") if isinstance(field, dict) else None
        type_name = _SCHEMA_TYPE_NAMES.get(declared) if isinstance(declared, str) else None
        rendered.append(_typed(name, type_name, name in needed))
    return rendered


def _params_with_types(params: Mapping[str, JsonValue]) -> str:
    """Each parameter as `name:type`, with `?` after one that may be left out; one with no declared type is its name."""
    rendered: list[str] = []
    for name, spec in sorted(params.items()):
        if not isinstance(spec, dict):
            rendered.append(name)
        elif "properties" in spec:
            rendered.extend(_request_fields(spec))
        else:
            rendered.append(_typed(name, spec.get("type"), spec.get("required") is not False))
    return ", ".join(rendered)


def _emit_introspection(info: DeviceIntrospectionDTO) -> None:
    output.emit_kv(f"device {info.name}", [
        ("type", info.type or ""),
        ("interfaces", ", ".join(info.interfaces) or "-"),
        ("capabilities", ", ".join(info.capabilities) or "-"),
        ("provides_state", str(info.provides_state)),
    ])
    if info.methods:
        output.emit_table(
            f"Methods ({info.name})",
            ["name", "kind", "returns", "params"],
            [
                [method_name, meta.kind, meta.returns or "", _params_with_types(meta.params)]
                for method_name, meta in sorted(info.methods.items())
            ],
        )
    else:
        output.info("(no methods advertised)")
    if info.danger_levels:
        output.emit_table(
            f"Danger ({info.name})",
            ["command", "danger"],
            [[name, level] for name, level in sorted(info.danger_levels.items())],
        )


@app.command("capabilities")
def capabilities(
    device_id: str = typer.Argument(..., help="Device id / name."),
) -> None:
    """Show what a device can run: interfaces, capabilities, methods and each command's danger level.

    Both servers answer GET /api/devices/{device_id}/introspection.
    """
    info = get_client().device_introspection(device_id)
    if output.get_mode().value == "json":
        output.emit_json(info.model_dump(mode="json"))
        return
    _emit_introspection(info)


def device_entry_rows(entry: DeviceRegistryEntryResponse) -> list[tuple[str, str | None]]:
    """One device's state as `orca device info` and `orca describe device` print it."""
    engine = entry.engine
    if engine is None:
        return [("initialized", str(entry.is_initialized)), ("engine", "(only its orca-client reports it)")]
    return [
        ("type", engine.type_name),
        ("initialized", str(entry.is_initialized)),
        ("busy", str(engine.is_busy)),
        ("mode", engine.effective_mode.value),
        ("locations", ", ".join(engine.position_ids) or "-"),
        ("loaded", ", ".join(engine.loaded_labware_ids) or "-"),
        ("under_external_control", str(engine.under_external_control)),
    ]


@app.command("info")
def info(
    name: str = typer.Argument(...),
    capabilities: bool = typer.Option(
        False, "--capabilities",
        help="Also show what the device can run, as `orca device capabilities` does.",
    ),
) -> None:
    """Show one device's registry entry and (optionally) what it can run."""
    client = get_client()
    entry = client.device_registry_show(name)
    introspection = client.device_introspection(name) if capabilities else None
    if output.get_mode().value == "json":
        payload: dict[str, JsonValue] = entry.model_dump(mode="json")
        if introspection is not None:
            payload["capabilities"] = introspection.model_dump(mode="json")
        output.emit_json(payload)
        return
    output.emit_kv(f"device {entry.name}", device_entry_rows(entry))
    if introspection is not None:
        _emit_introspection(introspection)


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


def _emit_command_result(answer: BatchExecuteResponse, device: str) -> None:
    if output.get_mode().value == "json":
        output.emit_json(answer.model_dump(mode="json"))
        return
    ran = answer.results[0]
    output.emit_kv("device command", [
        ("device", device),
        ("command", ran.command),
        ("success", str(ran.success)),
        ("duration_s", f"{ran.duration_ms / 1000:.3f}"),
        ("value", "" if ran.result is None else json.dumps(ran.result)),
    ])


@app.command("send")
def send(
    name: str = typer.Argument(...),
    command: str = typer.Argument(
        ..., help="Command name as `orca device capabilities` lists it.",
    ),
    options: list[str] = typer.Argument(
        None, help="key=value pairs (alternative to --params).",
    ),
    params: str = typer.Option(
        "", "--params", help="JSON-encoded options dict, e.g. '{\"speed\": 1000}'.",
    ),
    confirm: bool = typer.Option(
        False, "--confirm",
        help="Acknowledge a command the driver flags requires_confirm: the "
             "raw vendor console, whose payload nothing between here and the "
             "controller can read. No other command needs it.",
    ),
    reason: str | None = typer.Option(
        None, "--reason", help="Why, for the audit trail.",
    ),
) -> None:
    """Send a command to a device by name.

    Reaches any command the device advertises, including the vendor commands
    a driver forwards to its hardware. An interface row carries an orca-side
    namespace (`shaker.shake`) which is dropped; the bare form works too. A
    vendor command's prefix names the object it lives on (`gripper.ungrip`)
    and is sent whole. Bypasses workflow coordination; if the device is busy
    with a workflow action this will contend on the device lock.
    """
    request = BatchExecuteRequest(
        device_id=name,
        commands=[{"command": command, "params": _parse_options(params, options), "confirm": confirm}],
        stop_on_error=True,
        reason=reason,
    )
    answer = BatchExecuteResponse.model_validate(
        get_client().device_operator_command(BATCH_EXECUTE, request).model_dump(),
    )
    _emit_command_result(answer, name)


@app.command("initialize")
def initialize(
    name: str = typer.Argument(...),
    reason: str | None = typer.Option(
        None, "--reason", help="Why, for the audit trail.",
    ),
) -> None:
    """Initialize or re-initialize a device.

    Brings the device up and resets driver state. Can drop the labware it was
    tracking, reset calibration, and interrupt an action currently holding the
    device lock. It asks for no motion; homing is its own command.

    A clean run clears any fault standing on the device.
    """
    client = get_client()
    client.device_initialize(name, reason=reason)
    output.info(f"device {name} initialized")


@app.command("connect")
def connect(
    name: str = typer.Argument(...),
    reason: str | None = typer.Option(
        None, "--reason", help="Why, for the audit trail.",
    ),
) -> None:
    """Open the link to a device. Moves nothing and readies nothing.

    Use this to take a device without initializing it. `initialize` is the
    step that makes it ready to command.
    """
    client = get_client()
    client.device_connect(name, reason=reason)
    output.info(f"device {name} connected")


@app.command("mode")
def mode(
    name: str = typer.Argument(...),
    setting: DeviceMode = typer.Argument(..., help="SIM, or LIVE for a device with a real driver."),
    confirm: bool = typer.Option(False, "--confirm", help="Switch without asking. The warning is still printed."),
) -> None:
    """Set a device to sim or live, and bring it up in that mode. Asks first. Refused during a run."""
    client = get_client()
    asked = client.device_set_mode(name, setting)
    if asked.warning is None:
        output.info(f"device {asked.device_name} is already {asked.mode.value}")
        return
    output.warn(asked.warning)
    if not confirm and not typer.confirm("Switch?", default=False):
        output.info("nothing switched")
        return
    switched = client.device_set_mode(name, setting, confirm=True)
    output.info(f"device {switched.device_name} set to {switched.mode.value}")
    if switched.bring_up_error is not None:
        output.warn(switched.bring_up_error)


@app.command("setup")
def setup(name: str = typer.Argument(...)) -> None:
    """Print devices/<name>.json: which orca-client runs the device, its driver, connection and sim settings."""
    output.emit_document(get_client().device_setup_get(name).setup.model_dump(mode="json", exclude_defaults=True))


@app.command("write-setup")
def write_setup(
    name: str = typer.Argument(...),
    file: Path = typer.Argument(..., exists=True, dir_okay=False, help="A JSON file holding the whole setup."),
    reason: str | None = typer.Option(None, "--reason", help="Why, for the audit trail."),
) -> None:
    """Replace devices/<name>.json with FILE. Takes effect on the next reload."""
    try:
        setup = DeviceSetup.model_validate_json(file.read_text(encoding="utf-8"))
    except ValidationError as refused:
        raise typer.BadParameter(str(refused), param_hint="FILE") from refused
    written = get_client().device_setup_put(name, setup, reason=reason)
    output.info(f"devices/{written.device_name}.json written")
    output.warn(written.reload_note)


@app.command("disconnect")
def disconnect(
    name: str = typer.Argument(...),
    reason: str | None = typer.Option(
        None, "--reason", help="Why, for the audit trail.",
    ),
) -> None:
    """Hand a device back. Moves nothing, but ends the session.

    Anything mid-action on this device fails, and hardware that holds motor
    power while connected drops it here.
    """
    client = get_client()
    client.device_disconnect(name, reason=reason)
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
    reason: str | None = typer.Option(
        None, "--reason", help="Why, for the audit trail.",
    ),
) -> None:
    """Re-seed a liquid handler from the world model. A state push, no motion.

    Re-dispatches the deck layout and re-declares every labware the ledger
    places on its deck. Use after anything that rebuilt the driver's session
    outside orca (a run cancelled on the touchscreen, a driver-internal
    recovery), so a RETRY can find its labware again. Initialize / connect
    through orca re-seed automatically.
    """
    client = get_client()
    result = client.device_reconcile_deck(name, reason=reason)
    output.emit_json(result.model_dump(mode="json"))
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
    output.emit_json(result.model_dump(mode="json"))
    if not result.mounted:
        output.info(f"{name}: {result.provenance}, no tips recorded on any channel")
        return
    output.info(f"{name}: {result.provenance}")
    for tip in result.mounted:
        guessed = " (channel number inferred)" if tip.channel_is_inferred else ""
        head = "96 head channel" if tip.head == "head96" else "channel"
        output.info(f"  {head} {tip.channel}: {tip.tip_rack} {tip.position}{guessed}")


@app.command("set-mounted-tips")
def set_mounted_tips(
    name: str = typer.Argument(..., help="Liquid handler name."),
    tip: list[str] = typer.Option(
        [], "--tip",
        help="channel=rack:position, repeated, for the channel head. Pass none to say it is empty.",
    ),
    head96: str | None = typer.Option(
        None, "--head96",
        help="The tip rack the 96 head took its tips from. Leave out to say the 96 head is empty.",
    ),
    reason: str | None = typer.Option(None, "--reason", help="Why you are stating it."),
) -> None:
    """State what each head is carrying.

    Absolute: any channel you do not name is recorded as carrying nothing, on
    both heads. This writes Orca's record only; nothing sends it to the
    driver. A vendor protocol runs only when the record says the head is empty.
    """
    mounted = [_parse_mounted_tip(entry) for entry in tip]
    if head96 is not None:
        mounted += [
            MountedTipDTO(channel=channel, tip_rack=head96, position=position, head="head96")
            for channel, position in enumerate(HEAD96_POSITIONS)
        ]
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
    at. Refused when a stop cut a command off at the device and left the head
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
    result = client.device_compare_deck(name)
    output.emit_json(result.model_dump(mode="json"))
    if result.driver_deck_empty and not result.agrees:
        output.info(
            f"{name} reports an empty deck and the ledger has labware on it: "
            f"its session was rebuilt and the deck is waiting to be "
            f"re-declared. Run reconcile-deck."
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
    reason: str | None = typer.Option(
        None, "--reason", help="Why, for the audit trail.",
    ),
) -> None:
    """Give a device back to the workflow. Be clear of it first."""
    client = get_client()
    client.device_release_control(name, reason=reason)
    output.info(f"device {name} released")


@app.command("clear-fault")
def clear_fault(
    name: str = typer.Argument(..., help="Device or transporter name."),
    reason: str | None = typer.Option(
        None, "--reason", help="Why, for the audit trail.",
    ),
) -> None:
    """Say a device has been looked at after a command left it part-way through.

    A command that fails, times out, or is cancelled after it reached the
    device faults it, and the workflow stops driving it. Look at the machine
    and put it right first: this clears the record, not the trouble.

    Usually not needed. If a thread is paused on this fault, recovering it with
    RETRY, RETRY_ACTION or CONTINUE says the machine has been looked at and clears
    the fault on the way, and a clean `initialize` or `home` clears it too.
    Reach for this when the machine is fit but neither of those applies.
    """
    client = get_client()
    result = client.device_clear_fault(name, reason=reason)
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
    output.info(f"{name}: {len(disagreements)} thing(s) the deck and the ledger disagree about")
    for item in disagreements:
        driver = item.driver_site or "nowhere on the deck"
        if item.labware_name is None:
            output.info(f"  no labware on record, {item.ledger_site} to {driver} ({item.reason}): {item.detail}")
            continue
        output.info(
            f"  {item.labware_name}: ledger says {item.ledger_site}, "
            f"driver says {driver} ({item.reason})"
        )


# -- Unified device registry ------------------------------------------------
#
# These sub-verbs show the same two-card view as the device registry list.


def _link_mode(entry: DeviceRegistryEntryResponse) -> str:
    """Which of the device's drivers answered `device_connected`.

    Read it with the flag: a link open under SIM is a
    simulator's, and one open under LIVE on a bench running SIM belongs
    to an instrument the commands are not reaching. "-" means nobody could
    answer, which is an agent-held device whose agent has gone quiet.
    """
    return entry.device_link_mode or "-"


def _emit_registry_entry_kv(entry: DeviceRegistryEntryResponse) -> None:
    """Render a DeviceRegistryEntryResponse via output.emit_kv."""
    declared_kind = entry.topology_card.declared_kind if entry.topology_card else ""
    advertised_kind = entry.connection_card.advertised_kind if entry.connection_card else ""
    elig = entry.mode_eligibility
    output.emit_kv(f"device {entry.name}", [
        ("declared_kind", declared_kind or "-"),
        ("advertised_kind", advertised_kind or "-"),
        ("client_connected", str(entry.is_client_connected)),
        ("device_connected", str(entry.is_device_connected)),
        ("device_link_mode", _link_mode(entry)),
        ("is_initialized", str(entry.is_initialized)),
        ("mode_eligibility", f"check_run={elig.check_run} sim={elig.sim} live={elig.live}"),
        ("fault", entry.fault.message if entry.fault else "-"),
    ])


def _kind_column(entry: DeviceRegistryEntryResponse) -> str:
    """The declared kind, with the client's own word beside it when the two disagree."""
    declared = entry.topology_card.declared_kind if entry.topology_card else ""
    advertised = entry.connection_card.advertised_kind if entry.connection_card else ""
    if declared and advertised and declared != advertised:
        return f"{declared} (client: {advertised})"
    return declared or advertised or "-"


def _holding_column(entry: DeviceRegistryEntryResponse) -> str:
    if entry.engine is None or entry.engine.held_labware_id is None:
        return "-"
    return entry.engine.held_labware_id


@app.command("list")
def list_devices() -> None:
    """List every device and transporter: what the topology declares, what the
    on-prem client reports, and what the engine holds.

    The `client` column is whether the on-prem client is reachable; `device` is
    whether the link is open on whichever driver is being driven. They differ: a
    released device sits under a client that is still heartbeating. `link` names
    the driver that answered; `-` means nobody could, so `device` is false for
    want of anywhere to put "unknown".
    """
    entries = get_client().device_registry_list()
    if output.get_mode().value == "json":
        output.emit_json([e.model_dump(mode="json") for e in entries])
        return
    output.emit_table(
        "Devices",
        ["name", "kind", "client", "device", "link", "initialized",
         "busy", "holding", "fault"],
        [
            [
                e.name,
                _kind_column(e),
                str(e.is_client_connected),
                str(e.is_device_connected),
                _link_mode(e),
                str(e.is_initialized),
                str(e.engine.is_busy) if e.engine else "-",
                _holding_column(e),
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


register_device_verbs(app)

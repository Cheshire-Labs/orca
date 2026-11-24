"""`orca describe <kind> [name]` -- unified detail view.

Replaces a stack of per-noun ``info`` verbs (``orca workflow info``,
``orca method info``, ``orca thread info``, ``orca location info``,
``orca device info``, ``orca system info``, ``orca system overview``)
with one verb that takes a kind + optional name and shows whatever the
daemon knows about that entity.

Rationale: the per-noun ``list`` verbs each have a distinct table shape
and stay separate, but the ``info``/``overview`` verbs were uniformly
"look up by name and dump". Consolidating saves six entries in
``--help`` and gives callers one mental model ("orca describe X").

``orca describe system`` takes no name and folds the old ``system info``
and ``system overview`` into one block.

Every ``describe`` verb works against either backend.

Each command builds its own client. That is intentional: every ``orca <verb>``
invocation is a separate OS process, so one client per verb is one client per
process. Sharing a client across commands would only dedupe the cheap pid-file
lookup.
"""

from pydantic import BaseModel
import typer

from orca.cli import output
from orca.cli.backend import get_client
from orca.cli.device import device_entry_rows
from orca.operations.system_models import GetSystemInfoResponse


app = typer.Typer(
    help="Show details for a system entity (workflow, method, thread, "
         "location, device, or system itself).",
    no_args_is_help=True,
)


class _SystemOverview(BaseModel):
    """Aggregate counts rendered by `orca describe system`."""
    devices: int
    locations: int
    workflows: int
    methods: int


class _SystemDescribePayload(BaseModel):
    """Combined info + counts payload for `orca describe system --json`."""
    info: GetSystemInfoResponse
    overview: _SystemOverview


@app.command("system")
def describe_system() -> None:
    """System topology summary + overview counts in one view."""
    client = get_client()
    info = client.system_info()
    overview = _SystemOverview(
        devices=len(client.device_registry_list()),
        locations=len(client.list_locations()),
        workflows=len(client.list_workflows()),
        methods=len(client.methods_list()),
    )
    payload = _SystemDescribePayload(info=info, overview=overview)
    output.emit_json(payload.model_dump(mode="json"))
    output.emit_kv("System", [
        ("name", info.name),
        ("description", info.description),
        ("version", info.version),
        ("devices", str(overview.devices)),
        ("locations", str(overview.locations)),
        ("workflows", str(overview.workflows)),
        ("methods", str(overview.methods)),
    ])


@app.command("workflow")
def describe_workflow(name: str = typer.Argument(..., help="Workflow name.")) -> None:
    """Show details for one workflow template."""
    client = get_client()
    items = client.list_workflows()
    match = next((w for w in items if w.name == name), None)
    if match is None:
        output.not_found("workflow", name)
    output.emit_json(match.model_dump(mode="json"))
    output.emit_kv(f"workflow {match.name}", [
        ("entry_threads", ", ".join(match.entry_thread_template_names) or "(none)"),
    ])


@app.command("method")
def describe_method(name: str = typer.Argument(..., help="Method name.")) -> None:
    """Show details for one method template. Works against both backends."""
    client = get_client()
    items = client.methods_list()
    match = next((m for m in items if m.name == name), None)
    if match is None:
        output.not_found("method", name)
    output.emit_json(match.model_dump(mode="json"))
    output.emit_kv(f"method {match.name}", [
        ("failure_policy", match.failure_policy.value),
    ])


@app.command("thread")
def describe_thread(name: str = typer.Argument(..., help="Thread template name.")) -> None:
    """Show details for one thread template. Works against both backends."""
    client = get_client()
    items = client.list_thread_templates()
    match = next((t for t in items if t.name == name), None)
    if match is None:
        output.not_found("thread", name)
    output.emit_json(match.model_dump(mode="json"))
    output.emit_kv(f"thread {match.name}", [
        ("labware", match.labware_template_name),
        ("start", match.start_position_id),
        ("end", ", ".join(match.end_position_ids)),
        ("put down before the run", "yes" if match.placed_before_run else "no"),
        ("starts holding", "not declared" if match.initial_state is None else match.initial_state.model_dump_json(exclude_defaults=True)),
    ])


@app.command("location")
def describe_location(name: str = typer.Argument(..., help="Location name.")) -> None:
    """Show details for one location. Works against both backends."""
    client = get_client()
    items = client.list_locations()
    match = next((loc for loc in items if loc.name == name), None)
    if match is None:
        output.not_found("location", name)
    output.emit_json(match.model_dump(mode="json"))
    output.emit_kv(f"location {match.name}", [
        ("resource", match.resource_name or ""),
        ("loaded_labware", ", ".join(match.loaded_labware_ids) or "(none)"),
    ])


@app.command("device")
def describe_device(name: str = typer.Argument(..., help="Device name.")) -> None:
    """Show one device's registry entry (type, busy/init flags, loaded labware)."""
    entry = get_client().device_registry_show(name)
    output.emit_json(entry.model_dump(mode="json"))
    output.emit_kv(f"device {entry.name}", device_entry_rows(entry))

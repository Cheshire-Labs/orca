"""`orca submission ...` noun sub-app.

Expose T6 LabwareGroup/Submission API over the daemon:
    submit  : submit one or more groups against a workflow.
    list    : list known submissions (optionally per-execution).
    detail  : show one submission by id.

To close an execution so further JOIN_EXISTING submissions are rejected,
use `orca execution close <execution_id>`. A submission is a unit of work
inside an execution, not a closable container.

Group JSON format (for `submit --groups-file` or `--groups-json`):

    {
      "id": "batch-1",
      "name": "optional-display-name",
      "members": [
        {
          "thread_template_name": "sample_plate",
          "acquisition": {"kind": "pool"}
        },
        {
          "thread_template_name": "tip_rack",
          "acquisition": {
            "kind": "location",
            "source_location": "stack_1",
            "verify_barcode": "ABC123"
          }
        }
      ]
    }

Multiple groups in one submit: supply a JSON list instead of an object.
"""

import json
from pathlib import Path

import typer
from pydantic import JsonValue

from orca.cli import output, resolve
from orca.cli.app import STATE
from orca.cli.backend import get_client
from orca.daemon.schemas import (
    LabwareGroupDTO, OptionValueJson, SubmissionSubmitRequest,
)


app = typer.Typer(
    help="Submit labware groups and manage submissions.",
    no_args_is_help=True,
)


def _load_groups_spec(path: str | None, inline: str | None) -> list[dict[str, JsonValue]]:
    """Resolve --groups-file / --groups-json into a list of group dicts."""
    if path and inline:
        output.fail(
            "--groups-file and --groups-json are mutually exclusive",
            code=output.EXIT_USAGE,
        )
    source: str | None = None
    if path:
        p = Path(path)
        if not p.is_file():
            output.fail(
                f"--groups-file path {path!r} does not exist or is not a file",
                code=output.EXIT_USAGE,
            )
        source = p.read_text(encoding="utf-8")
    elif inline:
        source = inline
    if source is None:
        return []
    try:
        parsed = json.loads(source)
    except json.JSONDecodeError as e:
        output.fail(
            f"invalid group JSON: {e}",
            code=output.EXIT_USAGE,
        )
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        output.fail(
            "group JSON must be an object or list of objects",
            code=output.EXIT_USAGE,
        )
    for i, g in enumerate(parsed):
        if not isinstance(g, dict):
            output.fail(
                f"group entry {i} is not a JSON object",
                code=output.EXIT_USAGE,
            )
    return parsed


@app.command("submit")
def submit(
    workflow: str = typer.Argument(..., help="Workflow name."),
    groups_file: str = typer.Option(
        "", "--groups-file", help="Path to a JSON file with one group or a list of groups.",
    ),
    groups_json: str = typer.Option(
        "", "--groups-json", help="Inline JSON string (object or list of objects).",
    ),
    batch_mode: str = typer.Option(
        "STANDALONE", "--batch-mode",
        help="STANDALONE | JOIN_EXISTING. JOIN_EXISTING merges into live batch receivers.",
    ),
    operator_id: str | None = typer.Option(None, "--operator-id"),
    profile: str = typer.Option("", "--profile", help="Deployment profile name (audit)."),
    vars_: str = typer.Option("", "--vars", help="KEY=VAL,KEY2=VAL2 submission-scope overrides."),
    run_mode: str = typer.Option(
        "", "--run-mode",
        help="PURE_SIM | DEVICE_SIM | LIVE. REQUIRED per submission: there "
             "is no deployment-level fallback.",
    ),
    confirm: bool = typer.Option(
        False, "--confirm",
        help="Acknowledge a LIVE submission against devices whose topology "
             "declares a sim-direction sim_override.",
    ),
) -> None:
    """Submit a workflow with zero or more labware groups.

    Zero groups: legacy one-shot submission path (same as `orca run`).
    One-plus groups: T6 multi-group batch. JOIN_EXISTING may merge into a
    live receiver when the workflow already has an active execution.

    `--run-mode`: required PURE_SIM / DEVICE_SIM / LIVE selector. The 12-row
    v3.4 resolver combines this with each device's topology sim_override at
    dispatch time.

    `--confirm`: required when submitting LIVE with any device whose topology
    declares a sim-direction sim_override.
    """
    if batch_mode not in ("STANDALONE", "JOIN_EXISTING"):
        output.fail(
            f"--batch-mode must be STANDALONE or JOIN_EXISTING, got {batch_mode!r}",
            code=output.EXIT_USAGE,
        )
    from orca.daemon.schemas import RunModeStr, is_run_mode_str
    run_mode_value: RunModeStr
    if not run_mode:
        output.fail(
            "--run-mode is required; pass one of "
            "PURE_SIM, DEVICE_SIM, or LIVE.",
            code=output.EXIT_USAGE,
        )
        return
    if is_run_mode_str(run_mode):
        run_mode_value = run_mode
    else:
        output.fail(
            f"--run-mode must be PURE_SIM, DEVICE_SIM, or LIVE; got {run_mode!r}",
            code=output.EXIT_USAGE,
        )
        return
    groups_spec = _load_groups_spec(groups_file or None, groups_json or None)
    variables: dict[str, OptionValueJson] | None = None
    if vars_.strip():
        variables = {}
        for pair in vars_.split(","):
            if "=" not in pair:
                output.fail(
                    f"invalid --vars entry {pair!r}; expected KEY=VALUE",
                    code=output.EXIT_USAGE,
                )
            k, _, v = pair.partition("=")
            k = k.strip()
            v = v.strip()
            if v.lower() == "true":
                variables[k] = True
            elif v.lower() == "false":
                variables[k] = False
            else:
                try:
                    variables[k] = int(v)
                except ValueError:
                    try:
                        variables[k] = float(v)
                    except ValueError:
                        variables[k] = v

    if not STATE.force:
        msg = (
            f"Submit workflow '{workflow}' with {len(groups_spec)} group(s), "
            f"batch_mode={batch_mode}?"
        )
        if not typer.confirm(msg, default=False):
            output.confirmation_denied()

    client = get_client()
    request = SubmissionSubmitRequest(
        workflow_name=workflow,
        groups=[LabwareGroupDTO.model_validate(g) for g in groups_spec],
        variables=variables or None,
        batch_mode=batch_mode,
        operator_id=operator_id,
        deployment_profile=profile or None,
        run_mode=run_mode_value,
        acknowledge_warnings=confirm,
    )
    snap = client.submission_submit(request)
    resolve.record_last_execution(snap.execution_id)
    if output.get_mode().value == "json":
        output.emit_json(snap.model_dump(mode="json"))
        return
    output.info(
        f"submission [cyan]{snap.id[:8]}[/cyan] accepted "
        f"(execution={snap.execution_id[:8]}, groups={snap.group_count})",
    )
    if snap.unsettled:
        # Said here because this is the moment somebody is looking. The same
        # list needs `orca state unsettled` and somebody remembering to ask.
        output.info(
            f"{len(snap.unsettled)} thing(s) nobody has settled. Nothing is "
            f"blocked; settle what this run depends on:",
        )
        for subject in snap.unsettled:
            output.info(f"  {subject.subject}: {subject.detail}")
            if subject.settle_with is not None:
                output.info(f"    settle with: {subject.settle_with}")


@app.command("list")
def list_submissions(
    execution: str = typer.Option(
        "", "--execution", help="Filter to one execution (id or prefix).",
    ),
) -> None:
    """List all submissions or filter by execution."""
    client = get_client()
    eid: str | None = None
    if execution:
        ids = [r.id for r in client.list_executions()]
        eid = resolve.resolve_execution_id(execution, ids)
    items = client.submissions_list(execution_id=eid)
    if output.get_mode().value == "json":
        output.emit_json([s.model_dump(mode="json") for s in items])
        return
    output.emit_table(
        "Submissions",
        ["id", "execution", "workflow", "groups", "status", "batch_mode", "submitted_at"],
        [
            [
                s.id[:8], s.execution_id[:8], s.workflow_name,
                str(s.group_count), s.status, s.batch_mode, s.submitted_at,
            ]
            for s in items
        ],
    )


@app.command("detail")
def detail(
    submission_id: str = typer.Argument(
        ..., help="Submission id (full or 4+ char prefix).",
    ),
) -> None:
    """Show one submission.

    Resolves prefix against `submissions_list`. `SubmissionFacade.get_submission`
    and `list_submissions` both walk `iter_executions()` -- single source --
    so a labware-style pass-through is not needed.
    """
    client = get_client()
    ids = [s.id for s in client.submissions_list()]
    resolved = resolve.resolve_id(submission_id, ids, what="submission")
    snap = client.submission_get(resolved)
    if output.get_mode().value == "json":
        output.emit_json(snap.model_dump(mode="json"))
        return
    output.emit_kv(
        f"submission {snap.id[:8]}",
        [
            ("execution", snap.execution_id[:8]),
            ("workflow", snap.workflow_name),
            ("status", snap.status),
            ("batch_mode", snap.batch_mode),
            ("groups", str(snap.group_count)),
            ("submitted_at", snap.submitted_at),
            ("operator_id", snap.operator_id),
            ("deployment_profile", snap.deployment_profile),
        ],
    )



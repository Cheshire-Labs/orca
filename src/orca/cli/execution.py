"""`orca execution ...` noun sub-app.

Instance-level commands for workflow executions and the threads running
inside them. Thread-template listing lives under `orca thread ...`
(see cli/registry.py). The split:
    orca thread list                           -> templates (authoring inventory)
    orca execution threads <id>                -> instances (per-execution list)
    orca execution thread <sub>                -> instance operations (detail, pause, ...)

Every verb works against either backend through `get_client()`.
"""

from orca.modes.sim_pacing import SIM_SPEEDUP_RANGE
from pathlib import Path
from typing import Protocol

import typer

from orca.cli import output, resolve
from orca.cli.app import STATE
from orca.cli.backend import get_client
from orca.cli.control_plane import IControlPlaneClient
from orca.operations.execution_models import GetExecutionDetailResponse
from orca.operations.wire_models import ThreadSnapshotResponse
from orca.operations.thread_models import InsertWhere, MethodSnapshotModel
from orca.workflow_models.status_enums import RecoveryDecision


def _parse_where(where: str) -> InsertWhere:
    """Validate the required --where option to the InsertWhere literal set.

    Typer hands us a bare str; narrow it here so a bad value fails fast with a
    clear CLI error instead of a downstream Pydantic 422 on the wire model.
    """
    match where:
        case "head" | "tail" | "before" | "after":
            return where
        case _:
            raise typer.BadParameter(
                "must be one of: head, tail, before, after", param_hint="--where",
            )


def _current_method_name(current: MethodSnapshotModel | None) -> str:
    return "" if current is None else current.name


def _group_cell(group_id: str | None) -> str:
    """A thread shared across groups, or of a submission sent without groups, has none."""
    if group_id is None:
        return "(none)"
    return group_id


def _one_line(text: str) -> str:
    """A docstring flattened onto the one line a key/value row has.

    A paragraph printed here runs flush left under the keys and breaks the
    block apart, so it is collapsed and capped like the other free-text rows.
    """
    return " ".join(text.split())[:120]


def _current_action_line(current: MethodSnapshotModel | None) -> str:
    """The action a thread is on, named and, where the author wrote one, described.

    An action's name is a function name, and a function name is not a sentence.
    """
    if current is None or current.current_action is None:
        return ""
    action = current.current_action
    if action.description is None:
        return action.command
    return f"{action.command}: {_one_line(action.description)}"


def error_paused_threads(
    detail: GetExecutionDetailResponse,
) -> list[ThreadSnapshotResponse]:
    """Threads in `detail` paused by an action error (not a manual pause).

    A default-PAUSE action failure parks the thread and leaves the
    execution non-terminal, so it never shows up as a failed/aborted
    status. The signal is per-thread: ``pause_reason == "error"``.
    Shared by the detail render and the ``run --wait`` poll so both
    surface the same condition.
    """
    return [t for t in detail.threads if t.pause_reason == "error"]


app = typer.Typer(help="Query and manage workflow executions.", no_args_is_help=True)
thread_app = typer.Typer(
    help="Per-thread instance operations inside an execution.",
    no_args_is_help=True,
)
app.add_typer(thread_app, name="thread")


# -- Execution-level verbs ---------------------------------------------------


class _Pausable(Protocol):
    paused: bool
    pause_reason: str | None
    abort_armed: bool


def _paused_cell(record: _Pausable) -> str:
    """The pause latch in words, for a surface that otherwise shows only the
    phase. A paused execution stays `accepting` or `draining`, so the phase
    column alone reads as if the stop never landed."""
    if not record.paused:
        return ""
    who = "system" if record.pause_reason == "system" else "operator"
    return f"paused ({who}), abort armed" if record.abort_armed else f"paused ({who})"


@app.command("list")
def list_executions() -> None:
    """List all executions known to the loaded system."""
    client = get_client()
    records = client.list_executions()
    if output.get_mode().value == "json":
        output.emit_json([r.model_dump(mode="json") for r in records])
        return
    output.emit_table(
        "Executions",
        ["id", "workflow", "status", "paused", "error"],
        [
            [r.id[:8], r.workflow_name, r.status, _paused_cell(r), r.error or ""]
            for r in records
        ],
    )


@app.command("detail")
def detail(
    execution_id: str = typer.Argument(...),
) -> None:
    """Show the full ExecutionDetail snapshot (threads, methods, actions).

    Table mode renders a concise summary (status + thread counts). Use
    ``--json`` to get the full structure including per-thread method lists.
    """
    client = get_client()
    ids = [r.id for r in client.list_executions()]
    eid = resolve.resolve_execution_id(execution_id, ids)
    detail_dto = client.get_execution(eid)
    if output.get_mode().value == "json":
        output.emit_json(detail_dto.model_dump(mode="json"))
        return
    output.emit_kv(f"execution {detail_dto.id[:8]}", [
        ("workflow", detail_dto.workflow_name),
        ("status", detail_dto.status),
        ("paused", _paused_cell(detail_dto)),
        ("error", detail_dto.error or ""),
        ("sim_speedup", output.sim_speedup_cell(detail_dto.simulated, detail_dto.sim_speedup)),
        ("threads_total", str(detail_dto.total_thread_count)),
        ("threads_active", str(detail_dto.active_thread_count)),
        ("threads_completed", str(detail_dto.completed_thread_count)),
    ])
    for t in error_paused_threads(detail_dto):
        output.emit_kv(
            f"thread {t.id[:8]} ({t.name}) PAUSED (error)",
            [
                ("last_error", t.last_error or "(no detail)"),
                ("pause_message", t.pause_message or "(no detail)"),
                ("action", _current_action_line(t.current_method) or "(no action bound)"),
                ("pause_site", t.pause_site or "(not error-paused)"),
                # The only decisions the recover hint below will be accepted
                # with. Anything else is refused and the thread stays paused.
                (
                    "honoured_decisions",
                    ", ".join(t.honoured_decisions) or "(not error-paused)",
                ),
                # retry on the hint below re-runs this one call.
                ("paused_device_command", t.paused_device_command or "(not in a device call)"),
                (
                    "recover",
                    f"orca execution thread recover {eid} {t.id} "
                    f"<retry|retry-action|continue|abort-action|abort-method|abort-thread>",
                ),
            ],
        )


@app.command("sim-speedup")
def sim_speedup_command(
    execution_id: str = typer.Argument(...),
    sim_speedup: float = typer.Argument(
        ..., min=SIM_SPEEDUP_RANGE[0], max=SIM_SPEEDUP_RANGE[1],
        help="How many times faster than real time, 0.1 to 1000: 1 is real time, 100 a hundred times faster.",
    ),
) -> None:
    """Set a sim run's speed-up. Applies at once, to the step in progress too.

    A LIVE run is paced by its hardware and a check run never waits, so both refuse.
    """
    client = get_client()
    ids = [r.id for r in client.list_executions()]
    eid = resolve.resolve_execution_id(execution_id, ids)
    result = client.set_execution_sim_speedup(eid, sim_speedup)
    if output.get_mode().value == "json":
        output.emit_json(result.model_dump(mode="json"))
        return
    output.info(
        f"execution {eid[:8]} sim_speedup={result.sim_speedup:g} ({result.message})",
    )


@app.command("stop")
def stop(
    execution_id: str = typer.Argument(...),
    confirm: bool = typer.Option(
        False, "--confirm",
        help="Confirm the abort. Without it, stop only pauses and arms the "
             "execution; a second call with --confirm aborts it.",
    ),
    reason: str | None = typer.Option(
        None, "--reason", help="Why, for the audit trail.",
    ),
) -> None:
    """Stop a running execution: pause immediately, then confirm to abort.

    The first call (no --confirm) pauses the execution immediately and arms the
    abort; it is recoverable via `orca execution resume`. Call again with
    --confirm to abort (cancels threads, releases reservations). A single stray
    --confirm never aborts a not-yet-armed execution -- it pauses and arms.
    """
    client = get_client()
    ids = [r.id for r in client.list_executions()]
    eid = resolve.resolve_execution_id(execution_id, ids)
    if confirm and not STATE.force:
        if not typer.confirm(
            f"Abort execution {eid[:8]}? This cancels threads and cannot "
            f"be undone.",
            default=False,
        ):
            output.confirmation_denied()
    result = client.stop_execution(eid, confirm=confirm, reason=reason)
    if result.status == "aborted":
        output.info(f"execution {eid[:8]} aborted (phase={result.phase})")
    elif result.status == "already_terminal":
        output.info(f"execution {eid[:8]} already {result.phase}; nothing to stop")
    else:
        output.info(
            f"execution {eid[:8]} paused and abort armed; run "
            f"`orca execution stop {eid[:8]} --confirm` to abort, or "
            f"`orca execution resume {eid[:8]}` to disarm"
        )


@app.command("remove")
def remove(
    execution_id: str = typer.Argument(...),
    reason: str | None = typer.Option(
        None, "--reason", help="Why, for the audit trail.",
    ),
) -> None:
    """Remove a terminal execution from the runtime's registry."""
    client = get_client()
    ids = [r.id for r in client.list_executions()]
    eid = resolve.resolve_execution_id(execution_id, ids)
    if not STATE.force:
        if not typer.confirm(f"Remove execution {eid[:8]}?", default=False):
            output.confirmation_denied()
    client.execution_remove(eid, reason=reason)
    output.info(f"removed execution {eid[:8]} from the running registry; `orca execution list` still shows its stored record")


@app.command("close")
def close(
    execution_id: str = typer.Argument(...),
    reason: str | None = typer.Option(
        None, "--reason", help="Why, for the audit trail.",
    ),
) -> None:
    """Close an execution (ACCEPTING -> DRAINING).

    After close, JOIN_EXISTING submissions against this execution are
    rejected. STANDALONE submissions for the same workflow boot a fresh
    execution. Live threads in this execution continue to completion.

    Use this when operators have submitted work throughout a run and want
    to signal "no more coming" so the execution drains and terminates.
    Works against both backends.
    """
    lister = get_client()
    ids = [r.id for r in lister.list_executions()]
    eid = resolve.resolve_execution_id(execution_id, ids)
    if not STATE.force:
        if not typer.confirm(
            f"Close execution {eid[:8]}?", default=False,
        ):
            output.confirmation_denied()
    resp = get_client().execution_close(eid, reason=reason)
    if output.get_mode().value == "json":
        output.emit_json(resp.model_dump(mode="json"))
        return
    output.info(
        f"execution [cyan]{resp.execution_id[:8]}[/cyan] -> phase={resp.phase}",
    )


@app.command("threads")
def list_threads_cmd(
    execution_id: str = typer.Argument(...),
) -> None:
    """List thread instances inside one execution. Works against both backends."""
    client = get_client()
    ids = [r.id for r in client.list_executions()]
    eid = resolve.resolve_execution_id(execution_id, ids)
    threads = client.get_execution(eid).threads
    if output.get_mode().value == "json":
        output.emit_json([t.model_dump(mode="json") for t in threads])
        return
    output.emit_table(
        f"Threads ({eid[:8]})",
        ["id", "name", "group", "status", "completed", "current"],
        [
            [
                t.id[:8],
                t.name,
                _group_cell(t.group_id),
                t.status,
                str(t.completed_method_count),
                _current_method_name(t.current_method),
            ]
            for t in threads
        ],
    )


@app.command("pause")
def pause_all(
    execution_id: str = typer.Argument(...),
    reason: str = typer.Option(
        None, "--reason", "-r",
        help="Optional human-readable reason captured on the audit trail.",
    ),
) -> None:
    """Pause every non-terminal thread in an execution. Works against both backends."""
    client = get_client()
    ids = [r.id for r in client.list_executions()]
    eid = resolve.resolve_execution_id(execution_id, ids)
    if not STATE.force:
        if not typer.confirm(
            f"Pause all threads in {eid[:8]}?", default=False,
        ):
            output.confirmation_denied()
    client.pause_all_threads(eid, reason=reason)
    output.info(f"paused all threads in {eid[:8]}")


@app.command("resume")
def resume_all(
    execution_id: str = typer.Argument(...),
    reason: str = typer.Option(
        None, "--reason", "-r",
        help="Optional human-readable reason captured on the audit trail.",
    ),
) -> None:
    """Resume every manually-paused thread. Error-paused threads remain paused.

    Works against both backends.
    """
    client = get_client()
    ids = [r.id for r in client.list_executions()]
    eid = resolve.resolve_execution_id(execution_id, ids)
    result = client.resume_all_threads(eid, reason=reason)
    if output.get_mode().value == "json":
        output.emit_json(result.model_dump(mode="json", exclude={"kind"}))
        return
    output.emit_kv(
        f"resume-all ({eid[:8]})",
        [
            ("resumed", str(result.resumed)),
            ("pause_cancelled", str(result.pause_cancelled)),
            ("error_skipped", str(result.error_skipped)),
            ("completed_skipped", str(result.completed_skipped)),
        ],
    )


# -- `orca execution thread ...` sub-app -------------------------------------


def _resolve_thread_in_execution(
    client: IControlPlaneClient, execution: str, thread_id: str,
) -> tuple[str, str]:
    """Resolve execution id + thread id by prefix, reading threads from the execution detail."""
    ids = [r.id for r in client.list_executions()]
    eid = resolve.resolve_execution_id(execution, ids)
    thread_ids = [t.id for t in client.get_execution(eid).threads]
    tid = resolve.resolve_thread_id(thread_id, thread_ids)
    return eid, tid


@thread_app.command("detail")
def thread_detail(
    execution_id: str = typer.Argument(...),
    thread_id: str = typer.Argument(...),
) -> None:
    """Show one thread instance's full snapshot (completed methods, current)."""
    client = get_client()
    eid, tid = _resolve_thread_in_execution(client, execution_id, thread_id)
    snap = client.get_thread_detail(eid, tid)
    if output.get_mode().value == "json":
        output.emit_json(snap.model_dump(mode="json"))
        return
    output.emit_kv(
        f"thread {tid[:8]}",
        [
            ("name", snap.name),
            ("group", _group_cell(snap.group_id)),
            ("status", snap.status),
            ("current_location", snap.current_location or "unknown"),
            ("current_method", snap.current_method.name if snap.current_method else ""),
            ("current_action", _current_action_line(snap.current_method)),
            ("completed", ", ".join(snap.completed_methods) or "(none)"),
            ("pause_reason", snap.pause_reason or ""),
            ("last_error", (snap.last_error or "")[:80]),
            ("pause_message", (snap.pause_message or "")[:120]),
            ("pause_site", snap.pause_site or ""),
            ("honoured_decisions", ", ".join(snap.honoured_decisions)),
            ("paused_device_command", snap.paused_device_command or ""),
        ],
    )


@thread_app.command("pause")
def thread_pause(
    execution_id: str = typer.Argument(...),
    thread_id: str = typer.Argument(...),
    reason: str = typer.Option(
        None, "--reason", "-r",
        help="Optional human-readable reason captured on the audit trail.",
    ),
) -> None:
    """Request cooperative pause on one thread instance."""
    client = get_client()
    eid, tid = _resolve_thread_in_execution(client, execution_id, thread_id)
    if not STATE.force:
        if not typer.confirm(f"Pause thread {tid[:8]}?", default=False):
            output.confirmation_denied()
    client.pause_thread(eid, tid, reason=reason)
    output.info(f"paused thread {tid[:8]}")


@thread_app.command("resume")
def thread_resume(
    execution_id: str = typer.Argument(...),
    thread_id: str = typer.Argument(...),
    reason: str = typer.Option(
        None, "--reason", "-r",
        help="Optional human-readable reason captured on the audit trail.",
    ),
) -> None:
    """Resume a manually-paused thread (not for error recovery; use `recover`)."""
    client = get_client()
    eid, tid = _resolve_thread_in_execution(client, execution_id, thread_id)
    client.resume_thread(eid, tid, reason=reason)
    output.info(f"resumed thread {tid[:8]}")


@thread_app.command("spawn")
def thread_spawn(
    execution_id: str = typer.Argument(...),
    template_name: str = typer.Argument(..., help="Thread-template name to spawn."),
    labware_id: str | None = typer.Option(
        None, "--labware-id", help="Attach this existing labware instance (optional).",
    ),
    reason: str | None = typer.Option(
        None, "--reason", help="Why, for the audit trail.",
    ),
) -> None:
    """Manually create and start a thread inside an execution."""
    client = get_client()
    ids = [r.id for r in client.list_executions()]
    eid = resolve.resolve_execution_id(execution_id, ids)
    if not STATE.force:
        if not typer.confirm(
            f"Spawn thread '{template_name}' in execution {eid[:8]}?",
            default=False,
        ):
            output.confirmation_denied()
    snap = client.spawn_thread(
        eid, template_name,
        labware_id=labware_id, reason=reason,
    )
    if output.get_mode().value == "json":
        output.emit_json(snap.model_dump(mode="json"))
        return
    output.info(
        f"spawned thread [cyan]{snap.id[:8]}[/cyan] ({snap.name}) "
        f"status={snap.status}",
    )


@thread_app.command("recover")
def thread_recover(
    execution_id: str = typer.Argument(...),
    thread_id: str = typer.Argument(...),
    decision: str = typer.Argument(
        ...,
        help="retry | retry-action | continue | abort-action | abort-method | abort-thread",
    ),
    reason: str | None = typer.Option(
        None, "--reason",
        help="Why, for the audit trail. Required for abort-method on a manually paused thread.",
    ),
) -> None:
    """Apply a recovery decision to an error-paused thread.

    `abort-method` also takes a manually paused thread: it aborts the method in
    progress there. Use skip-method for a method that has not started.

    `continue` says the work is done and the run may carry on. After an action
    errored, use it when the call actually succeeded or you fixed the situation
    by hand. After a move failed, it means you carried the labware to the move's
    target yourself: run `orca labware edit-location` first to say where it is,
    or the runtime refuses. Either way it makes no claim that the failed step's
    work happened, and the ledger records it as operator-confirmed, not executed.
    """
    decision_map = {d.cli_word: d.value for d in RecoveryDecision}
    if decision not in decision_map:
        output.fail(
            f"decision must be one of {sorted(decision_map)}, got {decision!r}",
            code=output.EXIT_USAGE,
        )
    client = get_client()
    eid, tid = _resolve_thread_in_execution(client, execution_id, thread_id)
    if not STATE.force:
        if not typer.confirm(
            f"Recover thread {tid[:8]} with {decision}?", default=False,
        ):
            output.confirmation_denied()
    client.recover_thread(eid, tid, decision_map[decision], reason=reason)
    output.info(f"{decision} applied to thread {tid[:8]}")


# -- Thread mutation verbs (runtime-mutation-surface plan) ------------------


@thread_app.command("skip-method")
def thread_skip_method(
    execution_id: str = typer.Argument(...),
    thread_id: str = typer.Argument(...),
    method_name: str = typer.Option(
        ..., "--method-name",
        help="Name of a pending method on the thread's lane. Methods are "
             "not unique by name -- skip targets the FIRST match; re-issue "
             "to skip a later occurrence.",
    ),
    reason: str = typer.Option(
        ..., "--reason", help="Audit-trail reason for the skip.",
    ),
) -> None:
    """Skip a pending method on a paused thread.

    Use `recover <execution> <thread> abort-method` for a method already IN_PROGRESS. Skip is
    one-shot first-match by name; re-run for later occurrences.
    """
    client = get_client()
    eid, tid = _resolve_thread_in_execution(client, execution_id, thread_id)
    if not STATE.force:
        if not typer.confirm(
            f"Skip method '{method_name}' on thread {tid[:8]}? "
            f"[CRITICAL: downstream methods may fail if this method's "
            f"side effects are required]",
            default=False,
        ):
            output.confirmation_denied()
    client.thread_skip_method(
        eid, tid, method_name=method_name, reason=reason,
    )
    output.info(f"skipped method '{method_name}' on thread {tid[:8]}")


@thread_app.command("insert-method")
def thread_insert_method(
    execution_id: str = typer.Argument(...),
    thread_id: str = typer.Argument(...),
    template_name: str = typer.Option(
        "", "--template", help="Name of an already-registered MethodTemplate.",
    ),
    method_code_path: str = typer.Option(
        "", "--from-file",
        help="Path to a .py file containing a single @orca.method definition.",
    ),
    where: str = typer.Option(
        ..., "--where",
        help="Placement: head | tail | before | after. Required -- recovery "
             "inserts are position-sensitive, so choose explicitly.",
    ),
    anchor: str = typer.Option(
        "", "--anchor",
        help="Required for --where before/after: anchor method name.",
    ),
    reason: str = typer.Option(
        ..., "--reason", help="Audit-trail reason for the insertion.",
    ),
) -> None:
    """Insert a method into a paused thread's lane.

    Provide exactly one of --template or --from-file. --template looks up
    a method already registered at system-build time; --from-file reads a
    Python source file with a single @orca.method definition and validates
    it server-side before execution.
    """
    parsed_where = _parse_where(where)
    if (template_name == "") == (method_code_path == ""):
        output.fail(
            "provide exactly one of --template or --from-file",
            code=output.EXIT_USAGE,
        )

    method_code: str | None = None
    if method_code_path:
        try:
            method_code = Path(method_code_path).read_text(encoding="utf-8")
        except OSError as exc:
            output.fail(
                f"could not read --from-file {method_code_path!r}: {exc}",
                code=output.EXIT_USAGE,
            )

    client = get_client()
    eid, tid = _resolve_thread_in_execution(client, execution_id, thread_id)
    label = template_name or method_code_path
    if not STATE.force:
        if not typer.confirm(
            f"Insert method '{label}' into thread {tid[:8]} (where={where})?",
            default=False,
        ):
            output.confirmation_denied()
    client.thread_insert_method(
        eid, tid,
        template_name=template_name or None,
        method_code=method_code,
        where=parsed_where,
        anchor=anchor or None,
        reason=reason,
    )
    output.info(f"inserted method '{label}' into thread {tid[:8]}")


@thread_app.command("skip-action")
def thread_skip_action(
    execution_id: str = typer.Argument(...),
    thread_id: str = typer.Argument(...),
    action_command: str | None = typer.Option(
        None, "--action-command", help="Action command to skip.",
    ),
    action_id: str | None = typer.Option(
        None, "--action-id", help="Action id to skip.",
    ),
    reason: str = typer.Option(
        ..., "--reason", help="Audit-trail reason for the skip.",
    ),
) -> None:
    """Skip a pending action on a paused thread's IN_PROGRESS method."""
    if (action_command is None) == (action_id is None):
        output.fail(
            "provide exactly one of --action-command or --action-id",
            code=output.EXIT_USAGE,
        )
    client = get_client()
    eid, tid = _resolve_thread_in_execution(client, execution_id, thread_id)
    label = action_command or action_id
    assert label is not None
    if not STATE.force:
        if not typer.confirm(
            f"Skip action '{label}' on thread {tid[:8]}?", default=False,
        ):
            output.confirmation_denied()
    client.thread_skip_action(
        eid, tid,
        action_id=action_id,
        action_command=action_command,
        reason=reason,
    )
    output.info(f"skipped action '{label}' on thread {tid[:8]}")


@thread_app.command("insert-action")
def thread_insert_action(
    execution_id: str = typer.Argument(...),
    thread_id: str = typer.Argument(...),
    action_code_path: str = typer.Argument(
        ..., help="Path to a .py file containing a single @orca.action definition.",
    ),
    where: str = typer.Option(
        ..., "--where",
        help="Placement: head | tail | before | after. Required -- recovery "
             "inserts are position-sensitive, so choose explicitly.",
    ),
    anchor: str = typer.Option(
        "", "--anchor", help="Required for --where before/after: anchor tag.",
    ),
    reason: str = typer.Option(
        ..., "--reason", help="Audit-trail reason for the insertion.",
    ),
) -> None:
    """Insert an action into a paused thread's IN_PROGRESS method.

    Source file must contain a single @orca.action definition. ActionTemplates
    have no global registry, so insertion is always by source code.
    """
    parsed_where = _parse_where(where)
    try:
        action_code = Path(action_code_path).read_text(encoding="utf-8")
    except OSError as exc:
        output.fail(
            f"could not read action source {action_code_path!r}: {exc}",
            code=output.EXIT_USAGE,
        )
    client = get_client()
    eid, tid = _resolve_thread_in_execution(client, execution_id, thread_id)
    if not STATE.force:
        if not typer.confirm(
            f"Insert action from {action_code_path} into thread {tid[:8]} "
            f"(where={where})?", default=False,
        ):
            output.confirmation_denied()
    client.thread_insert_action(
        eid, tid,
        action_code=action_code,
        where=parsed_where,
        anchor=anchor or None,
        reason=reason,
    )
    output.info(f"inserted action from {action_code_path} into thread {tid[:8]}")


@thread_app.command("replace-method")
def thread_replace_method(
    execution_id: str = typer.Argument(...),
    thread_id: str = typer.Argument(...),
    target: str = typer.Argument(
        ...,
        help="Method name to replace: a pending method (spliced), or the "
             "method this thread is error-paused on (staged for recovery).",
    ),
    template_name: str = typer.Option(
        "", "--template", help="Name of an already-registered MethodTemplate.",
    ),
    method_code_path: str = typer.Option(
        "", "--from-file",
        help="Path to a .py file containing a single @orca.method definition.",
    ),
    reason: str = typer.Option(
        ..., "--reason", help="Audit-trail reason for the replacement.",
    ),
) -> None:
    """Replace a method with a substitute.

    A PENDING target is spliced in place (skip + insert). If the target is the
    method this thread is error-paused ON, the replacement is STAGED to run
    next and you must then run `orca execution thread recover <execution> <thread>
    abort-method` (after making the cell physically safe) to drop the failed
    method. Provide exactly one of --template or --from-file.
    """
    if (template_name == "") == (method_code_path == ""):
        output.fail(
            "provide exactly one of --template or --from-file",
            code=output.EXIT_USAGE,
        )

    method_code: str | None = None
    if method_code_path:
        try:
            method_code = Path(method_code_path).read_text(encoding="utf-8")
        except OSError as exc:
            output.fail(
                f"could not read --from-file {method_code_path!r}: {exc}",
                code=output.EXIT_USAGE,
            )

    client = get_client()
    eid, tid = _resolve_thread_in_execution(client, execution_id, thread_id)
    label = template_name or method_code_path
    if not STATE.force:
        if not typer.confirm(
            f"Replace method '{target}' with '{label}' on thread {tid[:8]}?",
            default=False,
        ):
            output.confirmation_denied()
    result = client.thread_replace_method(
        eid, tid,
        target_name=target,
        template_name=template_name or None,
        method_code=method_code,
        reason=reason,
    )
    if result.staged_for_recovery:
        output.info(f"staged '{label}' to replace in-progress method '{target}' on thread {tid[:8]}")
        if result.next_step:
            output.info(result.next_step)
    else:
        output.info(
            f"queued replacement of method '{target}' with '{label}' on thread "
            f"{tid[:8]} (applies when '{target}' is reached; no-op if it never runs)"
        )


@thread_app.command("replace-action")
def thread_replace_action(
    execution_id: str = typer.Argument(...),
    thread_id: str = typer.Argument(...),
    target_command: str = typer.Argument(
        ...,
        help="Action command to replace: a pending action (spliced), or the "
             "action this thread is error-paused on (staged for recovery).",
    ),
    action_code_path: str = typer.Argument(
        ..., help="Path to a .py file containing a single @orca.action definition.",
    ),
    reason: str = typer.Option(
        ..., "--reason", help="Audit-trail reason for the replacement.",
    ),
) -> None:
    """Replace an action with a substitute on the IN_PROGRESS method.

    The replacement runs next. A PENDING target is also skipped. If the target
    is the action this thread is error-paused ON, the replacement is STAGED and
    you must then run `orca execution thread recover <execution> <thread>
    abort-action` (after making the cell safe) to drop the failed action.
    """
    try:
        action_code = Path(action_code_path).read_text(encoding="utf-8")
    except OSError as exc:
        output.fail(
            f"could not read action source {action_code_path!r}: {exc}",
            code=output.EXIT_USAGE,
        )
    client = get_client()
    eid, tid = _resolve_thread_in_execution(client, execution_id, thread_id)
    if not STATE.force:
        if not typer.confirm(
            f"Replace action '{target_command}' with {action_code_path} "
            f"on thread {tid[:8]}?", default=False,
        ):
            output.confirmation_denied()
    result = client.thread_replace_action(
        eid, tid,
        target_command=target_command,
        action_code=action_code,
        reason=reason,
    )
    if result.staged_for_recovery:
        output.info(
            f"staged {action_code_path} to replace in-progress action "
            f"'{target_command}' on thread {tid[:8]}"
        )
        if result.next_step:
            output.info(result.next_step)
    else:
        output.info(
            f"queued replacement of action '{target_command}' with {action_code_path} "
            f"on thread {tid[:8]} (replacement runs next; '{target_command}' "
            f"skipped when reached)"
        )

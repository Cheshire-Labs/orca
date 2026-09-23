"""`orca execution ...` noun sub-app.

Instance-level commands for workflow executions and the threads running
inside them. Thread-template listing lives under `orca thread ...`
(see cli/registry.py). The split:
    orca thread list                           -> templates (authoring inventory)
    orca execution threads <id>                -> instances (per-execution list)
    orca execution thread <sub>                -> instance operations (detail, pause, ...)

Most verbs work against either backend via the resolved control-plane
client (`get_client()`): the execution-level `list`/`detail`/`stop`/`remove`
and every `thread` sub-verb (`detail`, `pause`, `resume`, `spawn`, `recover`,
and the mutation verbs `skip-method`/`abort-method`/`insert-method`/
`skip-action`/`insert-action`/`replace-method`/`replace-action`) -- all on
the shared `IControlPlaneClient` protocol, which a cloud backend mirrors on its
REST/MCP surface. Thread resolution reads the execution-detail snapshot
(`get_execution().threads`), so it too works against either backend.

The execution-level `close`/`threads`/`pause`/`resume` instead dispatch by
branching on `active_backend()` because their client methods are not on the
shared protocol.
"""

from pathlib import Path
from typing import Protocol

import typer
from pydantic import JsonValue

from orca.cli import output, resolve
from orca.cli.app import STATE
from orca.cli.backend import active_backend, cloud_client, get_client, local_client
from orca.cli.control_plane import (
    ExecutionDetailDTO,
    IControlPlaneClient,
    ThreadSnapshotDTO,
)
from orca.daemon.schemas import ActionSnapshotDTO, MethodSnapshotDTO
from orca.operations.thread_models import InsertWhere


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


def _current_method_name(
    current: MethodSnapshotDTO | dict[str, JsonValue] | None,
) -> str:
    """Method name from a thread's `current_method`, across backend shapes.

    Daemon `list_threads` returns a typed `MethodSnapshotDTO`; the cloud
    backend reads the execution-detail thread snapshots whose
    `current_method` is the raw `dict` projection. Both carry `name`.
    """
    if current is None:
        return ""
    if isinstance(current, MethodSnapshotDTO):
        return current.name
    name = current.get("name", "")
    return str(name) if name is not None else ""


def _one_line(text: str) -> str:
    """A docstring flattened onto the one line a key/value row has.

    A paragraph printed here runs flush left under the keys and breaks the
    block apart, so it is collapsed and capped like the other free-text rows.
    """
    return " ".join(text.split())[:120]


def _current_action_line(
    current: MethodSnapshotDTO | dict[str, JsonValue] | None,
) -> str:
    """The action a thread is on, named and, where the author wrote one, described.

    An action's name is a function name, and a function name is not a sentence.
    Same two backend shapes as `_current_method_name`.
    """
    if current is None:
        return ""
    action: ActionSnapshotDTO | dict[str, JsonValue] | None
    if isinstance(current, MethodSnapshotDTO):
        action = current.current_action
    else:
        raw_action = current.get("current_action")
        action = raw_action if isinstance(raw_action, dict) else None
    if action is None:
        return ""
    if isinstance(action, ActionSnapshotDTO):
        command, description = action.command, action.description
    else:
        command = str(action.get("command", ""))
        raw_description = action.get("description")
        description = raw_description if isinstance(raw_description, str) else None
    if description is None:
        return command
    return f"{command}: {_one_line(description)}"


def error_paused_threads(
    detail: ExecutionDetailDTO,
) -> list[ThreadSnapshotDTO]:
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
                # retry-op is on the hint below, and it re-runs this one call.
                ("paused_device_command", t.paused_device_command or "(not in a device call)"),
                (
                    "recover",
                    f"orca execution thread recover {eid} {t.id} "
                    f"<retry|retry-op|continue|abort-action|abort-method|abort-thread>",
                ),
            ],
        )


@app.command("stop")
def stop(
    execution_id: str = typer.Argument(...),
    confirm: bool = typer.Option(
        False, "--confirm",
        help="Confirm the abort. Without it, stop only pauses and arms the "
             "execution; a second call with --confirm aborts it.",
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
    result = client.stop_execution(eid, confirm=confirm)
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
) -> None:
    """Remove a terminal execution from the runtime's registry."""
    client = get_client()
    ids = [r.id for r in client.list_executions()]
    eid = resolve.resolve_execution_id(execution_id, ids)
    if not STATE.force:
        if not typer.confirm(f"Remove execution {eid[:8]}?", default=False):
            output.confirmation_denied()
    client.execution_remove(eid)
    output.info(f"removed execution {eid[:8]}")


@app.command("close")
def close(
    execution_id: str = typer.Argument(...),
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
    if active_backend() == "cloud":
        resp = cloud_client().execution_close(eid)
    else:
        resp = local_client().execution_close(eid)
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
    """List thread instances inside one execution. Works against both backends.

    The cloud backend has no standalone threads endpoint; it reads the
    per-thread snapshots embedded in the execution-detail response.
    """
    if active_backend() == "cloud":
        client = cloud_client()
        ids = [r.id for r in client.list_executions()]
        eid = resolve.resolve_execution_id(execution_id, ids)
        threads = list(client.get_execution(eid).threads)
    else:
        client = local_client()
        ids = [r.id for r in client.list_executions()]
        eid = resolve.resolve_execution_id(execution_id, ids)
        threads = client.list_threads(eid)
    if output.get_mode().value == "json":
        output.emit_json([t.model_dump(mode="json") for t in threads])
        return
    output.emit_table(
        f"Threads ({eid[:8]})",
        ["id", "name", "status", "completed", "current"],
        [
            [
                t.id[:8],
                t.name,
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
    if active_backend() == "cloud":
        client = cloud_client()
    else:
        client = local_client()
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
    if active_backend() == "cloud":
        client = cloud_client()
    else:
        client = local_client()
    ids = [r.id for r in client.list_executions()]
    eid = resolve.resolve_execution_id(execution_id, ids)
    result = client.resume_all_threads(eid, reason=reason)
    if output.get_mode().value == "json":
        output.emit_json(result.model_dump(mode="json"))
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
    """Resolve execution id + thread id via their prefix matching.

    Reads threads from the execution-detail snapshot (`get_execution`)
    rather than the daemon-only `list_threads` route so this resolves
    against either backend.
    """
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
            ("status", snap.status),
            ("current_location", snap.current_location),
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
) -> None:
    """Manually create and start a thread inside an execution.

    Use case: AUTO_SPAWN_FAILED incidents where the engine could not find
    a matching thread template. Once the operator has identified the right
    template, `thread spawn` wakes it and lets the waiting contributor
    proceed.
    """
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
        labware_id=labware_id,
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
        help="retry | retry-op | continue | abort-action | abort-method | abort-thread",
    ),
) -> None:
    """Apply a recovery decision to an error-paused thread.

    `continue` says the work is done and the run may carry on. After an action
    errored, use it when the call actually succeeded or you fixed the situation
    by hand. After a move failed, it means you carried the labware to the move's
    target yourself: run `orca labware edit-location` first to say where it is,
    or the runtime refuses. Either way it makes no claim that the failed step's
    work happened, and the ledger records it as operator-confirmed, not executed.
    """
    decision_map = {
        "retry": "RETRY",
        "retry-op": "RETRY_OP",
        "continue": "CONTINUE",
        "abort-action": "ABORT_ACTION",
        "abort-method": "ABORT_METHOD",
        "abort-thread": "ABORT_THREAD",
    }
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
    client.recover_thread(eid, tid, decision_map[decision])
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

    Use abort-method for methods that are already IN_PROGRESS. Skip is
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


@thread_app.command("abort-method")
def thread_abort_method(
    execution_id: str = typer.Argument(...),
    thread_id: str = typer.Argument(...),
    method_name: str = typer.Option(
        ..., "--method-name",
        help="Name of the IN_PROGRESS method on the thread.",
    ),
    reason: str = typer.Option(
        ..., "--reason", help="Audit-trail reason for the abort.",
    ),
) -> None:
    """Abort the IN_PROGRESS method on a paused thread.

    Aborts the running action and skips the rest of the method. For
    methods that have not yet started, use skip-method.
    """
    client = get_client()
    eid, tid = _resolve_thread_in_execution(client, execution_id, thread_id)
    if not STATE.force:
        if not typer.confirm(
            f"Abort method '{method_name}' on thread {tid[:8]}? "
            f"[CRITICAL: aborts the running action]",
            default=False,
        ):
            output.confirmation_denied()
    client.thread_abort_method(
        eid, tid, method_name=method_name, reason=reason,
    )
    output.info(f"aborted method '{method_name}' on thread {tid[:8]}")


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
    next and you must then run `orca execution thread recover ... --decision
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
    you must then run `orca execution thread recover ... --decision
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

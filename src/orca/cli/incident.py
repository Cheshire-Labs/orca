"""`orca incident ...` noun sub-app.

Backend-dispatched: ``list`` / ``get`` / ``ack`` / ``ack-all`` route to the
cloud control-plane client when ``active_backend()`` is "cloud", else to the
local daemon. SystemIncidents are non-Action runtime errors (variable
resolution failures, co-labware timeouts, plugin exceptions, etc.). See
orca.runtime.incident_store for the category list.

``get`` / ``ack`` accept id prefixes (>=4 chars) resolved against
``incidents_list``. ``list`` and ``get`` read the same backing store, so
no pass-through is needed.

The ``recoverable-timeout`` sub-app (extend / abort / mark-complete) works
against both backends: the engine holds the timed-out command, and the local
daemon exposes the same routes as a cloud deployment. There is no listing
endpoint for these on the CLI, so they require full UUIDs (same precedent as
``labware journey``).
"""

import typer

from orca.cli import output, resolve
from orca.cli.app import STATE
from orca.cli.backend import active_backend, cloud_client, get_client, local_client


app = typer.Typer(help="Non-Action error records: query + acknowledge.", no_args_is_help=True)

recoverable_timeout_app = typer.Typer(
    help="Operator decisions for RECOVERABLE_TIMEOUT incidents.",
    no_args_is_help=True,
)
app.add_typer(recoverable_timeout_app, name="recoverable-timeout")


def _resolve_incident_id(incident_id: str) -> str:
    """Resolve `incident_id` against the active backend's full incident list.

    `get` and `list` read the same backing store, so a labware-style
    pass-through to the daemon would not find anything local enumeration
    missed.
    """
    if active_backend() == "cloud":
        ids = [inc.id for inc in cloud_client().incidents_list()]
    else:
        ids = [inc.id for inc in local_client().incidents_list()]
    return resolve.resolve_id(incident_id, ids, what="incident")


@app.command("list")
def list_incidents(
    unacknowledged: bool = typer.Option(False, "--unacknowledged"),
    category: str = typer.Option("", "--category"),
    execution: str = typer.Option("", "--execution"),
) -> None:
    """List incidents, optionally filtered by state / category / execution."""
    if active_backend() == "cloud":
        items = cloud_client().incidents_list(
            unacknowledged_only=unacknowledged,
            category=category or None,
            execution_id=execution or None,
        )
    else:
        items = local_client().incidents_list(
            unacknowledged_only=unacknowledged,
            category=category or None,
            execution_id=execution or None,
        )
    if output.get_mode().value == "json":
        output.emit_json([i.model_dump(mode="json") for i in items])
        return
    output.emit_table(
        "Incidents",
        ["id", "category", "severity", "message", "ack"],
        [
            [
                i.id[:8], i.category, i.severity,
                i.message[:60], str(i.acknowledged),
            ]
            for i in items
        ],
    )


@app.command("get")
def get(
    incident_id: str = typer.Argument(
        ..., help="Incident id (full or 4+ char prefix).",
    ),
) -> None:
    """Show one incident's full detail."""
    resolved = _resolve_incident_id(incident_id)
    if active_backend() == "cloud":
        inc = cloud_client().incidents_get(resolved)
    else:
        inc = local_client().incidents_get(resolved)
    output.emit_json(inc.model_dump(mode="json"))
    output.emit_kv(f"incident {inc.id[:8]}", [
        ("category", inc.category),
        ("severity", inc.severity),
        ("message", inc.message),
        ("acknowledged", str(inc.acknowledged)),
        ("timestamp", str(inc.timestamp)),
        ("execution_id", inc.execution_id),
        ("thread_id", inc.thread_id),
        ("recovery_action", inc.recovery_action),
    ])


@app.command("ack")
def ack(
    incident_id: str = typer.Argument(
        ..., help="Incident id (full or 4+ char prefix).",
    ),
) -> None:
    """Acknowledge one incident."""
    resolved = _resolve_incident_id(incident_id)
    if not STATE.force:
        if not typer.confirm(
            f"Acknowledge incident {resolved[:8]}?", default=False,
        ):
            output.confirmation_denied()
    if active_backend() == "cloud":
        cloud_client().incidents_ack(resolved)
    else:
        local_client().incidents_ack(resolved)
    output.info(f"acknowledged incident {resolved[:8]}")


@app.command("ack-all")
def ack_all(
    category: str = typer.Option("", "--category"),
) -> None:
    """Acknowledge every unacknowledged incident (optionally by category)."""
    if not STATE.force:
        target = category or "all"
        if not typer.confirm(
            f"Acknowledge all incidents ({target})?", default=False,
        ):
            output.confirmation_denied()
    if active_backend() == "cloud":
        result = cloud_client().incidents_ack_all(category=category or None)
    else:
        result = local_client().incidents_ack_all(category=category or None)
    output.info(f"acknowledged {result.acknowledged_count} incidents")


@recoverable_timeout_app.command("extend")
def recoverable_timeout_extend(
    incident_id: str = typer.Argument(
        ..., help="RECOVERABLE_TIMEOUT incident id (full UUID).",
    ),
    additional_seconds: float = typer.Option(
        ...,
        "--additional-seconds",
        help="How much extra time to grant before the next RECOVERABLE_TIMEOUT.",
    ),
) -> None:
    """Operator decision: give the held command more time.

    Re-arms the dispatcher's command timer for ``--additional-seconds`` and
    acknowledges the original incident. If the new timer expires before a
    real response, a fresh RECOVERABLE_TIMEOUT incident is declared.
    """
    if additional_seconds <= 0:
        output.fail(
            "--additional-seconds must be > 0",
            code=output.EXIT_USAGE,
        )
    client = get_client()
    result = client.recoverable_timeout_extend(
        incident_id, additional_seconds,
    )
    output.info(
        f"extended incident {result.incident_id[:8]} by "
        f"{additional_seconds}s",
    )


@recoverable_timeout_app.command("abort")
def recoverable_timeout_abort(
    incident_id: str = typer.Argument(
        ..., help="RECOVERABLE_TIMEOUT incident id (full UUID).",
    ),
    operator: str = typer.Option(
        ..., "--operator", help="Operator name for the audit trail.",
    ),
    reason: str = typer.Option(
        ..., "--reason", help="Why the command should be aborted.",
    ),
) -> None:
    """Operator decision: fail the held command with CommandTimeoutError.

    Workflow propagates the failure normally. Resumes paused threads so
    the execution can transition to its error state.
    """
    if not operator.strip() or not reason.strip():
        output.fail(
            "--operator and --reason are both required and non-empty",
            code=output.EXIT_USAGE,
        )
    if not STATE.force:
        if not typer.confirm(
            f"Abort recoverable-timeout incident {incident_id[:8]}? "
            f"This fails the held workflow command.",
            default=False,
        ):
            output.confirmation_denied()
    client = get_client()
    result = client.recoverable_timeout_abort(incident_id, operator, reason)
    output.info(f"aborted incident {result.incident_id[:8]}")


@recoverable_timeout_app.command("mark-complete")
def recoverable_timeout_mark_complete(
    incident_id: str = typer.Argument(
        ..., help="RECOVERABLE_TIMEOUT incident id (full UUID).",
    ),
    operator: str = typer.Option(
        ..., "--operator", help="Operator name for the audit trail.",
    ),
    reason: str = typer.Option(
        ..., "--reason", help="Why the command should be marked complete.",
    ),
) -> None:
    """Operator decision: synthesize a success response.

    DANGEROUS. The operator asserts the device completed the action even
    though no response was received -- usually because they have manually
    intervened on the instrument. Downstream consumers expecting a typed
    response shape may fail at the workflow layer. Audit metadata is
    required and recorded.
    """
    if not operator.strip() or not reason.strip():
        output.fail(
            "--operator and --reason are both required and non-empty",
            code=output.EXIT_USAGE,
        )
    if not STATE.force:
        if not typer.confirm(
            f"Mark recoverable-timeout incident {incident_id[:8]} complete? "
            f"This synthesizes a success response without device "
            f"acknowledgement.",
            default=False,
        ):
            output.confirmation_denied()
    client = get_client()
    result = client.recoverable_timeout_mark_complete(
        incident_id, operator, reason,
    )
    output.info(
        f"marked incident {result.incident_id[:8]} complete "
        f"(operator-asserted)",
    )

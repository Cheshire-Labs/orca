"""Typer root app for the `orca` CLI.

Grammar:
  orca <lifecycle-verb> [args...]         -- start, shutdown, unload, run, version
  orca <noun> <verb>   [args...]          -- topology mount, workflow load,
                                             execution, thread, var, device, ...

Global flags (parsed in the root callback and stashed on a singleton context):
  --json                  emit JSON / NDJSON instead of tables
  --quiet, -q             suppress stdout (only exit code matters)
  --force, --yes, -y      skip confirmation prompts
  --no-color              disable ANSI (honors NO_COLOR env)
  --system-module SPEC    legacy loader; the daemon owns system loading now (ignored)
  --verbose               show full tracebacks on errors

Lifecycle verbs in this module:
  orca version
  orca run <workflow> [--wait] [--vars K=V,...]

Noun sub-apps are registered at import time at the bottom of the file.
"""

import os
import sys
import time

import typer

from orca.cli import output, resolve
from orca.cli.backend import cloud_help
from orca.cli.output import OutputMode
from orca.variables.errors import OptionValue


def _backend_signal_from_argv() -> str | None:
    """Find an explicit `--backend` value in sys.argv.

    Handles both forms Typer/Click accept:
      `--backend cloud`        -> ("--backend", "cloud") as two argv tokens
      `--backend=cloud`        -> one argv token
    Returns the value string or None if not present.
    """
    for i, token in enumerate(sys.argv):
        if token == "--backend" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if token.startswith("--backend="):
            return token.split("=", 1)[1]
    return None


def _hide_cloud_verbs_in_help() -> bool:
    """True when `--help` should suppress cloud-only sub-apps.

    Cloud verbs stay invokable in every case; this only filters help
    output for operators running against the local daemon who don't want
    noise about commands they can't use. Mirrors `resolve_backend(None)`
    silently:

    - no cloud backend installed -> hide
    - explicit `--backend cloud` (or `--backend=cloud`) -> show
    - explicit `--backend local` (or `--backend=local`) -> hide
    - `ORCA_BACKEND` env -> show or hide per value
    - cloud creds set without local -> show
    - daemon reachable AND no cloud signals -> hide
    - nothing resolves -> show (operator hasn't picked sides yet)

    The daemon probe is the expensive branch -- only call it when the
    current invocation is actually rendering help. Other commands pay
    nothing for this check.
    """
    if not _argv_is_help_invocation():
        # `hidden=` only changes --help output, so a command that prints none skips every probe.
        return False
    from orca.cli.backend import _daemon_reachable, cloud_backend_installed  # avoid import cycle at app load
    if not cloud_backend_installed():
        return True
    flag_value = _backend_signal_from_argv()
    if flag_value == "cloud":
        return False
    if flag_value == "local":
        return True
    env_backend = os.environ.get("ORCA_BACKEND")
    if env_backend == "cloud":
        return False
    if env_backend == "local":
        return True
    if os.environ.get("ORCA_CLOUD_URL") and os.environ.get("ORCA_CLOUD_API_KEY"):
        return False
    return _daemon_reachable()


def _hide_local_verbs_in_help() -> bool:
    """True when `--help` should suppress local-only sub-apps.

    Symmetric to `_hide_cloud_verbs_in_help`: hides sub-apps that have no
    cloud-capable verb when the backend is definitively cloud, so a cloud
    operator's `--help` does not advertise verbs that immediately
    fail-clean. Local-only verbs stay invokable; this only filters help.

    Hidden ONLY when cloud is the resolved backend:
    - explicit `--backend cloud` / `ORCA_BACKEND=cloud` -> hide
    - cloud creds set without a reachable daemon -> hide
    - explicit local, daemon reachable, or nothing resolved -> show
      (operator hasn't committed to cloud).
    """
    flag_value = _backend_signal_from_argv()
    if flag_value == "cloud":
        return True
    if flag_value == "local":
        return False
    env_backend = os.environ.get("ORCA_BACKEND")
    if env_backend == "cloud":
        return True
    if env_backend == "local":
        return False
    if not _argv_is_help_invocation():
        return False
    if not (os.environ.get("ORCA_CLOUD_URL") and os.environ.get("ORCA_CLOUD_API_KEY")):
        return False
    from orca.cli.backend import _daemon_reachable  # avoid import cycle at app load
    return not _daemon_reachable()


def _argv_is_help_invocation() -> bool:
    """True when sys.argv suggests we're rendering --help.

    Conservative: any presence of `--help`/`-h` anywhere in argv counts;
    an empty argv tail (`orca` alone) also triggers help because Typer's
    `no_args_is_help=True` on the root app.
    """
    if len(sys.argv) <= 1:
        return True
    return any(arg in ("--help", "-h") for arg in sys.argv[1:])


app = typer.Typer(
    name="orca",
    help="Orca lab automation CLI.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    pretty_exceptions_show_locals=False,
)


# -- Global context (set by root callback, read by verbs) ---------------------


class _GlobalState:
    force: bool = False
    verbose: bool = False
    system_module: str | None = None
    backend: str | None = None  # None = use precedence (env/config/auto); else "local" or "cloud"


STATE = _GlobalState()


@app.callback()
def _main(
    json_out: bool = typer.Option(False, "--json", help="Emit JSON / NDJSON instead of tables."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress stdout; only exit code."),
    force: bool = typer.Option(False, "--force", "--yes", "-y", help="Skip confirmation prompts."),
    no_color: bool = typer.Option(False, "--no-color", help="Disable ANSI color."),
    system_module: str = typer.Option(
        "",
        "--system-module",
        envvar="ORCA_SYSTEM_MODULE",
        help="module:factory of a builder returning .system/.workflow/.event_bus.",
    ),
    verbose: bool = typer.Option(False, "--verbose", help="Show full tracebacks."),
    backend: str = typer.Option(
        "",
        "--backend",
        help=(
            "Control-plane backend: 'local' (the loopback daemon)"
            + cloud_help(" or 'cloud' (a cloud deployment's REST API)")
            + ". Defaults follow ORCA_BACKEND -> ~/.orca/config.json -> "
            "auto-detect (loopback daemon if reachable)."
        ),
    ),
) -> None:
    """Global flag handler. Runs before every verb."""
    if no_color:
        os.environ["NO_COLOR"] = "1"
    if quiet:
        output.set_mode(OutputMode.QUIET)
    elif json_out:
        output.set_mode(OutputMode.JSON)
    else:
        output.set_mode(OutputMode.TABLE)
    STATE.force = force
    STATE.verbose = verbose
    STATE.system_module = system_module or None
    STATE.backend = backend or None


# -- Lifecycle verbs ----------------------------------------------------------


# Register the lifecycle commands at the top level.
from orca.cli import lifecycle as _lifecycle  # noqa: E402

app.command("start")(_lifecycle.start)
app.command("shutdown")(_lifecycle.shutdown)
app.command("unload")(_lifecycle.unload)

# `orca status` -- one-shot daemon + runtime health view.
from orca.cli import status as _status  # noqa: E402

app.command(
    "status",
    help="Show backend health: daemon snapshot"
    + cloud_help(" (local) or runtime status (cloud)") + ".",
)(_status.status)


@app.command()
def version() -> None:
    """Print the installed orca version."""
    try:
        from importlib.metadata import version as _pkg_version
        v = _pkg_version("cheshire-orca")
    except Exception:
        v = "unknown"
    if output.get_mode() == OutputMode.JSON:
        output.emit_json({"version": v})
    elif output.get_mode() != OutputMode.QUIET:
        typer.echo(f"orca {v}")


def _parse_vars(vars_str: str) -> dict[str, OptionValue]:
    """Parse `--vars KEY=VAL,KEY2=VAL2` into a dict. Best-effort value typing."""
    if not vars_str.strip():
        return {}
    result: dict[str, OptionValue] = {}
    for pair in vars_str.split(","):
        if "=" not in pair:
            output.fail(
                f"invalid --vars entry {pair!r}; expected KEY=VALUE",
                code=output.EXIT_USAGE,
            )
        k, _, v = pair.partition("=")
        k = k.strip()
        v = v.strip()
        if v.lower() == "true":
            result[k] = True
        elif v.lower() == "false":
            result[k] = False
        else:
            try:
                result[k] = int(v)
            except ValueError:
                try:
                    result[k] = float(v)
                except ValueError:
                    result[k] = v
    return result


_TERMINAL_STATES = {"completed", "failed", "aborted"}


@app.command()
def run(
    workflow: str = typer.Argument(..., help="Name of the workflow to submit."),
    wait: bool = typer.Option(False, "--wait", help="Block until the execution completes."),
    vars_: str = typer.Option("", "--vars", help="KEY=VAL,KEY2=VAL2 variable overrides."),
    profile: str = typer.Option(
        "",
        "--profile",
        help="Path to a JSON deployment profile; loaded into the new execution "
             "before any thread runs its first tick.",
    ),
    timeout: float = typer.Option(300.0, "--timeout", help="Seconds to wait (with --wait)."),
    poll: float = typer.Option(0.5, "--poll", help="Polling interval for --wait, seconds."),
    run_mode: str = typer.Option(
        "", "--run-mode",
        help="PURE_SIM | DEVICE_SIM | LIVE. REQUIRED per submission: there "
             "is no deployment-level fallback.",
    ),
    confirm: bool = typer.Option(
        False, "--confirm",
        help="Acknowledge a LIVE submission against devices whose topology "
             "declares a sim-direction sim_override (the "
             "LIVE_SUBMISSION_WITH_SIM_OVERRIDES_UNACKNOWLEDGED gate).",
    ),
) -> None:
    """Submit a workflow for execution. Fire-and-forget by default; use --wait to block.

    Submits against the currently-loaded system in the running daemon.

    `--profile <path>`: the CLI validates the file exists locally, then passes
    the path to the daemon. Because the daemon binds 127.0.0.1 only, it reads
    from the same filesystem the CLI sees. `--profile` is local-only -- the
    cloud REST surface does not accept a profile path; passing it with
    `--backend cloud` exits non-zero with a clean redirect.

    `--run-mode`: required PURE_SIM / DEVICE_SIM / LIVE selector. The 12-row
    v3.4 resolver combines this with each device's topology sim_override at
    dispatch time.

    `--confirm`: required when submitting LIVE with any device whose topology
    declares a sim-direction sim_override. The runtime refuses the submission;
    pass `--confirm` after reviewing the override list.
    """
    from orca.daemon.schemas import RunModeStr, is_run_mode_str
    run_mode_value: RunModeStr
    if not run_mode:
        output.fail(
            "--run-mode is required; pass one of "
            "PURE_SIM, DEVICE_SIM, or LIVE.",
            code=output.EXIT_USAGE,
        )
        return  # unreachable: output.fail exits, but satisfies pyright
    if is_run_mode_str(run_mode):
        run_mode_value = run_mode
    else:
        output.fail(
            f"--run-mode must be PURE_SIM, DEVICE_SIM, or LIVE; got {run_mode!r}",
            code=output.EXIT_USAGE,
        )
        return  # unreachable: output.fail exits, but satisfies pyright
    from orca.cli.backend import active_backend, get_client
    from orca.cli.client import LocalDaemonClient

    profile_path: str | None = None
    if profile:
        if active_backend() != "local":
            output.fail(
                "--profile is local-only; rerun with --backend local or drop the flag",
                code=output.EXIT_USAGE,
            )
        import os
        abs_path = os.path.abspath(profile)
        if not os.path.isfile(abs_path):
            output.fail(
                f"--profile path {abs_path!r} does not exist or is not a file",
                code=output.EXIT_USAGE,
            )
        profile_path = abs_path

    variables: dict[str, OptionValue] | None = _parse_vars(vars_) or None
    if profile_path is not None:
        # Daemon-only path: profile_path requires the LocalDaemonClient impl.
        local_client = LocalDaemonClient()
        record = local_client.submit_workflow(
            workflow, variables, run_mode=run_mode_value,
            profile_path=profile_path,
            acknowledge_warnings=confirm,
        )
        client = local_client
    else:
        client = get_client()
        record = client.submit_workflow(
            workflow, variables, run_mode=run_mode_value,
            acknowledge_warnings=confirm,
        )
    resolve.record_last_execution(record.id)
    output.info(
        f"submitted execution [cyan]{record.id[:8]}[/cyan] ({record.workflow_name})",
    )

    if not wait:
        if output.get_mode() == OutputMode.JSON:
            output.emit_json(record.model_dump(mode="json"))
        return

    # Poll until terminal or timeout.
    from orca.cli.execution import error_paused_threads
    output.info(f"waiting up to {timeout}s for completion...")
    deadline = time.time() + timeout
    final = record
    while time.time() < deadline:
        final = client.get_execution(record.id)
        # A default-PAUSE action error parks the thread and leaves the
        # execution non-terminal forever (it waits for an operator). Detect
        # it so --wait fails fast with a recover hint instead of burning the
        # whole timeout on a poll that can never reach a terminal state.
        paused = error_paused_threads(final)
        if paused:
            t = paused[0]
            in_call = (
                f" while running device call '{t.paused_device_command}'"
                if t.paused_device_command is not None
                else ""
            )
            output.fail(
                f"execution {record.id[:8]} has an error-paused thread "
                f"{t.id[:8]} ({t.name}){in_call}: {t.last_error or '(no detail)'}. "
                f"Recover with `orca execution thread recover {record.id} "
                f"{t.id} <retry|retry-op|continue|abort-action|abort-method|abort-thread>`.",
                code=output.EXIT_INVALID_STATE,
            )
        # ExecutionDetailDTO.status is a string (dataclass source is str).
        if final.status in _TERMINAL_STATES:
            break
        time.sleep(poll)
    else:
        output.fail(
            f"timed out after {timeout}s waiting for {record.id[:8]} "
            f"(last status: {final.status}). Execution is still running; "
            f"poll with `orca execution detail {record.id}` or rerun "
            f"with `--timeout <seconds>` for a longer wait.",
            code=output.EXIT_TIMEOUT,
        )

    if output.get_mode() == OutputMode.JSON:
        output.emit_json(final.model_dump(mode="json"))
    else:
        output.emit_kv(
            f"execution {final.id[:8]}",
            [
                ("workflow", final.workflow_name),
                ("status", final.status),
                ("error", final.error or ""),
            ],
        )
    if final.status != "completed":
        raise typer.Exit(code=output.EXIT_GENERIC)


# -- Sub-app registration -----------------------------------------------------
# Every sub-app below is first-party and shipped in this repo. Imports are
# direct -- if a sub-app module is broken, the CLI fails to start rather
# than silently dropping the verb from --help.

from orca.cli import access_config as _access_config  # noqa: E402
from orca.cli import grip_profiles as _grip_profiles  # noqa: E402
from orca.cli import move_defaults as _move_defaults  # noqa: E402
from orca.cli import audit as _audit  # noqa: E402
from orca.cli import deck_layout as _deck_layout  # noqa: E402
from orca.cli import describe as _describe  # noqa: E402
from orca.cli import device as _device  # noqa: E402
from orca.cli import execution as _execution  # noqa: E402
from orca.cli import incident as _incident
from orca.cli import state as _state  # noqa: E402
from orca.cli import labware as _labware  # noqa: E402
from orca.cli import manual_step as _manual_step  # noqa: E402
from orca.cli import ops_history as _ops_history  # noqa: E402
from orca.cli import registry as _registry  # noqa: E402
from orca.cli import reservation as _reservation  # noqa: E402
from orca.cli import runtime as _runtime  # noqa: E402
from orca.cli import submission as _submission  # noqa: E402
from orca.cli import teachpoint as _teachpoint  # noqa: E402
from orca.cli import topology as _topology  # noqa: E402
from orca.cli import var as _var  # noqa: E402

# Sub-apps whose every verb is local-only: hidden from cloud `--help` so a
# cloud operator is not shown verbs that immediately fail-clean. Mixed
# sub-apps (execution, labware, device, incident, workflow, method, audit)
# stay visible because they carry cloud-capable verbs.
_HIDE_LOCAL = _hide_local_verbs_in_help()

app.add_typer(_execution.app, name="execution")
app.add_typer(_audit.app, name="audit")
app.add_typer(_var.app, name="var", hidden=_HIDE_LOCAL)
app.add_typer(_labware.app, name="labware")
app.add_typer(_device.app, name="device")
app.add_typer(_reservation.app, name="reservation", hidden=_HIDE_LOCAL)
app.add_typer(_incident.app, name="incident")
app.add_typer(_state.app, name="state")
app.add_typer(_manual_step.app, name="manual-step")
app.add_typer(_submission.app, name="submission", hidden=_HIDE_LOCAL)
app.add_typer(_teachpoint.app, name="teachpoints", hidden=_HIDE_LOCAL)
app.add_typer(_access_config.app, name="access-configs", hidden=_HIDE_LOCAL)
app.add_typer(_grip_profiles.app, name="grip-profiles")
app.add_typer(_move_defaults.app, name="move-defaults")
app.add_typer(_deck_layout.app, name="deck-layouts", hidden=_HIDE_LOCAL)
app.add_typer(_registry.workflow_app, name="workflow")
app.add_typer(_registry.method_app, name="method")
app.add_typer(_registry.thread_app, name="thread", hidden=_HIDE_LOCAL)
app.add_typer(_registry.location_app, name="location", hidden=_HIDE_LOCAL)
app.add_typer(_describe.app, name="describe", hidden=_HIDE_LOCAL)
# ops-history reads run against both backends (the daemon binds the
# ops-history Operations), so the sub-app stays visible everywhere.
app.add_typer(_ops_history.app, name="ops-history")
app.add_typer(_topology.app, name="topology")
app.add_typer(_runtime.app, name="runtime")


if __name__ == "__main__":
    app()

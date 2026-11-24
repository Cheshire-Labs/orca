"""`orca status` -- one-shot health view across either backend.

Local backend: surfaces what the operator otherwise assembles by
`cat`-ing ``~/.orca/daemon.json`` + hand-pinging ``/health``: pid/port,
uptime, whether a system is loaded, the loaded spec name, and the runtime
state.

Cloud backend: renders the cloud runtime-lifecycle snapshot (same shape
as ``orca runtime status``) so a cloud operator gets a real answer
instead of the local "no daemon running" advice.
"""

import time

from pydantic import BaseModel

from orca.cli import output
from orca.cli.backend import get_client, resolve_backend
from orca.cli.client import OrcaClient
from orca.cli.control_plane import BackendNotResolvedError, ControlPlaneError
from orca.daemon.lifecycle import DaemonInfo, detect_live_daemon
from orca.operations.blocker_models import RemedyDTO
from orca.operations.wire_models import HealthResponse, RuntimeStatusResponse


_HEALTH_TIMEOUT_S = 2.0


class _StatusPayload(BaseModel):
    """JSON shape emitted by ``orca status --json``.

    ``health_reachable`` distinguishes "the daemon answered /health" from
    "/health failed or timed out". When the probe fails, the loaded /
    package_dir / runtime_state fields are left null because we genuinely
    don't know -- the daemon process is recorded but unresponsive.
    """
    pid: int
    port: int
    started_at: float
    uptime_seconds: float
    health_reachable: bool
    system_loaded: bool | None
    package_dir: str | None
    runtime_state: str | None
    mounting: bool | None = None


def _fetch_health(info: DaemonInfo) -> HealthResponse | None:
    """GET /health on the recorded daemon port. None on HTTP error."""
    return OrcaClient(f"http://127.0.0.1:{info.port}", timeout=_HEALTH_TIMEOUT_S).health_report()


def _remedy_line(remedy: RemedyDTO) -> str:
    """The commands to run, in order, as an operator would type them."""
    commands = [step.cli for step in remedy.steps if step.cli]
    return " && ".join(commands) if commands else remedy.label


def _render_blockers(payload: RuntimeStatusResponse) -> None:
    """What is stopping the run, worst first, with what to do about each.

    Printed whether or not the runtime built: a build failure is itself the
    first row, so there is one place to look either way.
    """
    if not payload.blockers_known:
        output.emit_kv(
            "blockers",
            [("known", "no"),
             ("note", "the list could not be read; this is not an all-clear")],
        )
        return
    if not payload.blockers:
        output.emit_kv("blockers", [("count", "0"), ("note", "nothing is in the way")])
        return
    rows: list[tuple[str, str, str, str]] = []
    for blocker in payload.blockers:
        best = next(
            (r for r in blocker.remedies if r.recommended),
            blocker.remedies[0] if blocker.remedies else None,
        )
        rows.append((
            blocker.kind,
            "!" if blocker.may_still_be_moving else blocker.severity,
            blocker.headline,
            "" if best is None else _remedy_line(best),
        ))
    output.emit_table(
        "what is stopping the run",
        ["kind", "severity", "what is wrong", "what to do"],
        [list(row) for row in rows],
    )


def _cloud_status() -> None:
    """Render the runtime-lifecycle snapshot for a cloud backend."""
    payload = get_client().runtime_status()
    if output.get_mode().value == "json":
        output.emit_json(payload.model_dump(mode="json"))
        return
    rows: list[tuple[str, str | None]] = [("built", str(payload.built))]
    if payload.last_build_error is not None:
        err = payload.last_build_error
        rows.extend([
            ("last_error_type", err.get("type", "(unknown)")),
            ("message", err.get("message", "")),
            ("hint", err.get("hint", "")),
        ])
    output.emit_kv("cloud runtime", rows)
    _render_blockers(payload)


def status() -> None:
    """Show backend health: daemon snapshot (local) or runtime status (cloud)."""
    # Imported here, not at module scope: `app` registers this very function,
    # so a top-level import is a cycle that only resolves in one order.
    from orca.cli.app import STATE

    try:
        backend = resolve_backend(STATE.backend)
    except BackendNotResolvedError:
        backend = "local"
    except ControlPlaneError as exc:
        output.fail(str(exc), code=output.EXIT_USAGE)
    if backend == "cloud":
        _cloud_status()
        return
    info = detect_live_daemon()
    if info is None:
        output.fail(
            "no daemon running (run `orca start` first)",
            code=output.EXIT_NOT_CONNECTED,
        )
    health = _fetch_health(info)
    uptime = max(0.0, time.time() - info.started_at)
    if health is not None:
        payload = _StatusPayload(
            pid=info.pid,
            port=info.port,
            started_at=info.started_at,
            uptime_seconds=uptime,
            health_reachable=True,
            system_loaded=health.system_loaded,
            package_dir=health.package_dir,
            runtime_state=(
                health.runtime_state.name if health.runtime_state is not None else None
            ),
            mounting=health.mounting,
        )
    else:
        payload = _StatusPayload(
            pid=info.pid,
            port=info.port,
            started_at=info.started_at,
            uptime_seconds=uptime,
            health_reachable=False,
            system_loaded=None,
            package_dir=None,
            runtime_state=None,
        )
    output.emit_json(payload.model_dump(mode="json"))
    if payload.health_reachable:
        output.emit_kv("daemon", [
            ("pid", str(payload.pid)),
            ("port", str(payload.port)),
            ("uptime", f"{payload.uptime_seconds:.0f}s"),
            ("system_loaded", str(payload.system_loaded)),
            ("package_dir", payload.package_dir or "(none)"),
            ("runtime_state", payload.runtime_state or "(unknown)"),
            ("mounting", str(payload.mounting)),
        ])
    else:
        output.emit_kv("daemon", [
            ("pid", str(payload.pid)),
            ("port", str(payload.port)),
            ("uptime", f"{payload.uptime_seconds:.0f}s"),
            ("health", "(unreachable: pid recorded but /health did not answer)"),
        ])

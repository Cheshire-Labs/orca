"""Entry point for `python -m orca.daemon`.

Spawned by `orca start` as a detached subprocess. Contract:
- Required arg: `--port N`  (127.0.0.1:N; no external binding).
- Writes ~/.orca/daemon.json with {pid, port, started_at} at startup.
- Serves FastAPI via uvicorn.
- POST /shutdown sets uvicorn's `should_exit`, which triggers a graceful
  shutdown and returns control to this module for PID-file cleanup.
- Cross-platform SIGINT/SIGTERM handling comes from uvicorn itself.

Uses uvicorn's programmatic API so we can inject the "please exit" hook
into FastAPI app state without relying on signal-handler semantics that
differ between Windows and POSIX.
"""

import argparse
import asyncio
import logging
import sys
import time

import uvicorn

from orca.daemon.app import create_app
from orca.daemon.lifecycle import (
    DaemonInfo,
    LOG_FILE_PATH,
    PID_FILE_PATH,
    delete_pid_file,
    write_pid_file,
)


logger = logging.getLogger("orca.daemon")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m orca.daemon",
        description="Orca daemon process (spawned by `orca start`).",
    )
    p.add_argument(
        "--port", type=int, required=True,
        help="TCP port to bind on 127.0.0.1. Picked by `orca start`.",
    )
    return p.parse_args(argv)


async def _serve(port: int) -> None:
    """Build app + run uvicorn.Server programmatically.

    `on_exit` is wired to set `server.should_exit`. POST /shutdown schedules
    it as a BackgroundTask so the response is flushed first; uvicorn then
    gracefully drains remaining connections and returns from serve().
    """
    server_holder: dict[str, uvicorn.Server] = {}

    async def on_exit() -> None:
        # Small grace window so the /shutdown response finishes writing.
        await asyncio.sleep(0.05)
        srv = server_holder.get("server")
        if srv is not None:
            srv.should_exit = True

    app = create_app(on_exit=on_exit)
    config = uvicorn.Config(
        app,
        host="127.0.0.1",  # localhost only; never externally routable
        port=port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)
    server_holder["server"] = server
    await server.serve()


def _setup_logging() -> None:
    """Route orca.* logs to ~/.orca/daemon.log for post-mortem inspection."""
    LOG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(LOG_FILE_PATH, mode="a", encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"),
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def main(argv: list[str] | None = None) -> int:
    import os

    args = _parse_args(argv)

    # On POSIX, detach from the immediate parent so a long-lived spawner
    # (e.g., a test runner) doesn't accumulate us as a zombie after we
    # exit. Single-fork: the spawner's direct child exits immediately and
    # we (the grandchild) are orphan-adopted by init, which reaps us on
    # exit. psutil.pid_exists() then drops the pid from /proc promptly.
    # Production `orca start` exits after spawning anyway, so the zombie
    # window there is microscopic; the test is the case this matters for.
    # Windows handles detachment via CREATE_NO_WINDOW + CREATE_NEW_PROCESS_GROUP
    # flags applied at Popen time.
    if sys.platform != "win32":
        if os.fork() > 0:
            os._exit(0)

    _setup_logging()
    logger.info("daemon starting on 127.0.0.1:%d (pid=%d)", args.port, os.getpid())

    info = DaemonInfo(
        pid=os.getpid(), port=args.port, started_at=time.time(),
    )
    write_pid_file(info, PID_FILE_PATH)

    try:
        asyncio.run(_serve(args.port))
    finally:
        delete_pid_file(PID_FILE_PATH)
        logger.info("daemon stopped (pid=%d); pid file removed", os.getpid())

    return 0


if __name__ == "__main__":
    sys.exit(main())

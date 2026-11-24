"""A test's daemon is proven to be its own, so a port another test's daemon took is never shared or shut down."""

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from orca.operations.wire_models import HealthResponse
from tests.isolated_daemon import START_LIMIT_S, pick_free_port, start_isolated_daemon, stop_isolated_daemon


def _ports(*ports: int) -> Iterator[int]:
    yield from ports
    while True:
        yield pick_free_port()


def _package_dir_at(port: int) -> Path:
    response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=5.0)
    return Path(HealthResponse.model_validate_json(response.content).package_dir).resolve()


# Three starts: the other daemon, the attempt on its port, and the attempt that succeeds.
@pytest.mark.timeout(START_LIMIT_S * 3 + 120.0)
def test_a_port_another_daemon_holds_is_given_up_and_that_daemon_is_left_running(tmp_path: Path) -> None:
    other_package = tmp_path / "other_package"
    other = start_isolated_daemon(tmp_path / "other_home", other_package)
    try:
        ports = _ports(other.info.port)
        own_package = tmp_path / "own_package"
        own = start_isolated_daemon(tmp_path / "own_home", own_package, pick_port=lambda: next(ports))
        try:
            assert own.info.port != other.info.port
            assert _package_dir_at(own.info.port) == own_package.resolve()
        finally:
            stop_isolated_daemon(own)

        assert _package_dir_at(other.info.port) == other_package.resolve(), "the other test's daemon still runs"
    finally:
        stop_isolated_daemon(other)

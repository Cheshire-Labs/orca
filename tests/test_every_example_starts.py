"""Every shipped example topology mounts on the daemon, from any directory.

`test_examples.py` builds the examples and stops there, so nothing covered
the validation that runs inside `SystemRuntime.start()`. It also runs from the
repo root, where a path an example spells relative to the shell resolves by
accident. Both gaps shipped real breakage: `Venus` reached start with no
`KIND`, and the Venus example read its teachpoints from a path that only
existed relative to the repo root.

Each example goes through `POST /mount-topology`, the route `orca topology
mount` calls, so the test and the operator take the same path.

The example list is discovered, not written down, so a new example is covered
without anyone remembering this file exists.
"""

import pathlib

import pytest
from httpx import ASGITransport, AsyncClient

from orca.daemon.app import create_app

pytest.importorskip("pylabrobot", reason="pylabrobot not installed")

_EXAMPLES = pathlib.Path(__file__).resolve().parents[1] / "examples"


def _example_packages() -> list[str]:
    return sorted(
        path.parent.name
        for path in _EXAMPLES.glob("*/topology.py")
    )


def test_the_discovery_finds_the_examples() -> None:
    """Without this, an empty list would make every case below vacuous."""
    found = _example_packages()
    assert len(found) >= 5, found
    assert "smc_assay" in found


@pytest.mark.parametrize("package", _example_packages())
async def test_example_topology_mounts_on_the_daemon(
    package: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Not the repo root: an example that spells a path relative to the shell
    # has to fail here rather than pass by where pytest happened to start.
    monkeypatch.chdir(tmp_path)

    app = create_app(initial_system_runtime=None)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://daemon.test",
    ) as client:
        resp = await client.post(
            "/mount-topology",
            json={"spec": f"examples.{package}.topology:build_topology", "sim": True},
        )
        assert resp.status_code == 200, resp.text
        await client.post("/unload")

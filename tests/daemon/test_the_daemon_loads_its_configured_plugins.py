"""A webhook in `ORCA_PLUGINS` is registered on the runtime the daemon mounts, as Orca AI registers its own."""

import pytest

from orca.daemon.app import create_app
from tests.daemon.fixture_package import write_package
from tests.daemon.started_daemon import started_daemon

pytestmark = pytest.mark.positions_bound_by_the_build


async def test_a_configured_webhook_is_registered_on_the_mounted_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ORCA_PLUGINS", '[{"type": "webhook", "url": "https://lims.example/hook"}]')
    write_package()
    app = create_app()
    async with started_daemon(app) as client:
        mounted = await client.post("/api/runtime/mount-package", json={})
        assert mounted.status_code == 200, mounted.text

        plugins = (await client.get("/api/plugins")).json()

        await client.post("/api/runtime/unload")
    assert [p["type_name"] for p in plugins] == ["WebhookPlugin"]
    assert app.state.daemon.plugins.plugins[0]._drain_task is None, "the drain stops with the daemon"


async def test_a_daemon_with_no_plugins_configured_registers_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ORCA_PLUGINS", raising=False)
    write_package()
    async with started_daemon(create_app()) as client:
        await client.post("/api/runtime/mount-package", json={})

        plugins = (await client.get("/api/plugins")).json()

        await client.post("/api/runtime/unload")
    assert plugins == []
